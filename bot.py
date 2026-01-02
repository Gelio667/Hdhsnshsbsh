# bot.py
# PTB 21+ multi-channel "Подслушано" bot:
# - /start -> сначала политика/гарантия анонимности (из privacy_anon.md) + кнопки Принять/Отказаться
# - после принятия -> меню (Отправить / Контролировать / 📜 Правила и анонимность)
# - привязка канала (только creator) + бот должен быть админом
# - deeplink /start <код> выбирает канал (код <= 20)
# - отправка текста/медиа анонимно
# - модерация (очередь + тикеты) с режимами: owner | admins | selected
# - очередь pending в меню контроля
# - логирование: технические + event_log (без имён)
#
# ВАЖНО про Markdown:
# Telegram "MarkdownV2" строгий. Файл privacy_anon.md должен быть написан в MarkdownV2 (или очень аккуратном markdown).
# Иначе Telegram может ругаться на "can't parse entities".
#
# Termux / Python 3.12 fix: создаём event loop вручную перед run_polling()

import os
import re
import base64
import hashlib
import asyncio
import logging
import datetime as dt
from typing import Optional, Tuple, List, Dict, Any

import aiosqlite
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

# ----------------- ENV -----------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0").strip() or "0")  # владелец бота (можно слать события/ошибки)
DEEPLINK_SALT = os.getenv("DEEPLINK_SALT", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден. Проверь .env и load_dotenv().")
if not DEEPLINK_SALT:
    raise RuntimeError("DEEPLINK_SALT не задан. Нужен для коротких кодов /start.")

DB_PATH = "bot.db"
POLICY_FILE = "privacy_anon.md"

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_REJECTED = "rejected"

# ----------------- LOGGING -----------------
def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)

logger = logging.getLogger("podslushano")

async def event_log(context: ContextTypes.DEFAULT_TYPE, text: str):
    """Событийный лог без username/имён (можно слать владельцу бота)."""
    logger.info("[EVENT] %s", text)
    if BOT_OWNER_ID:
        try:
            await context.bot.send_message(BOT_OWNER_ID, f"🧾 {text}")
        except Exception:
            pass

# ----------------- POLICY LOADER -----------------
def load_policy_text_and_hash() -> Tuple[str, str]:
    """
    Загружает privacy_anon.md и возвращает (text, sha256_hex).
    Пишем как MarkdownV2 (Telegram).
    """
    if not os.path.exists(POLICY_FILE):
        # аварийный текст, чтобы бот не падал
        txt = (
            "*Политика конфиденциальности и гарантия анонимности*\n\n"
            "Файл `privacy_anon.md` не найден рядом с bot.py.\n"
            "Создай его и перезапусти бота."
        )
        h = hashlib.sha256(txt.encode("utf-8")).hexdigest()
        return txt, h

    with open(POLICY_FILE, "r", encoding="utf-8") as f:
        txt = f.read()

    h = hashlib.sha256(txt.encode("utf-8")).hexdigest()
    return txt, h

POLICY_TEXT, POLICY_HASH = load_policy_text_and_hash()

# ----------------- DB + MIGRATIONS -----------------
async def table_exists(db: aiosqlite.Connection, name: str) -> bool:
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ) as cur:
        row = await cur.fetchone()
        return row is not None

async def column_exists(db: aiosqlite.Connection, table: str, col: str) -> bool:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return any(r[1] == col for r in rows)

async def db_init_and_migrate():
    async with aiosqlite.connect(DB_PATH) as db:
        # --- users consent table ---
        await db.execute("""
        CREATE TABLE IF NOT EXISTS user_consents (
            user_id INTEGER PRIMARY KEY,
            accepted INTEGER NOT NULL,
            policy_hash TEXT NOT NULL,
            accepted_at TEXT NOT NULL
        )
        """)

        # --- base tables ---
        await db.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            owner_user_id INTEGER NOT NULL,
            moderation INTEGER NOT NULL DEFAULT 1,
            reviewers_mode TEXT NOT NULL DEFAULT 'owner',  -- owner | admins | selected
            created_at TEXT NOT NULL
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS deeplinks (
            code TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS channel_reviewers (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(chat_id, user_id)
        )
        """)

        # --- migrations for channels (old DBs) ---
        if await table_exists(db, "channels"):
            if not await column_exists(db, "channels", "reviewers_mode"):
                await db.execute("ALTER TABLE channels ADD COLUMN reviewers_mode TEXT NOT NULL DEFAULT 'owner'")
            if not await column_exists(db, "channels", "moderation"):
                await db.execute("ALTER TABLE channels ADD COLUMN moderation INTEGER NOT NULL DEFAULT 1")
            if not await column_exists(db, "channels", "username"):
                await db.execute("ALTER TABLE channels ADD COLUMN username TEXT")

        # --- submissions: ensure NEW schema ---
        submissions_exists = await table_exists(db, "submissions")
        if not submissions_exists:
            await db.execute("""
            CREATE TABLE submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                sender_user_id INTEGER NOT NULL,
                content_type TEXT NOT NULL,
                text TEXT,
                file_id TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """)
        else:
            has_chat_id = await column_exists(db, "submissions", "chat_id")
            has_sender = await column_exists(db, "submissions", "sender_user_id")
            has_content_type = await column_exists(db, "submissions", "content_type")
            has_file_id = await column_exists(db, "submissions", "file_id")
            has_status = await column_exists(db, "submissions", "status")
            has_created = await column_exists(db, "submissions", "created_at")

            if not (has_chat_id and has_sender and has_content_type and has_file_id and has_status and has_created):
                ts = int(dt.datetime.now(dt.UTC).timestamp())
                legacy_name = f"submissions_legacy_{ts}"
                await db.execute(f"ALTER TABLE submissions RENAME TO {legacy_name}")

                await db.execute("""
                CREATE TABLE submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    sender_user_id INTEGER NOT NULL,
                    content_type TEXT NOT NULL,
                    text TEXT,
                    file_id TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """)
                # Старые записи остаются в legacy (их нельзя корректно перенести без chat_id).

        await db.commit()

# ---- consent helpers ----
async def get_user_consent(user_id: int) -> Optional[Tuple[int, str]]:
    """returns (accepted, policy_hash) or None"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT accepted, policy_hash FROM user_consents WHERE user_id=?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return (int(row[0]), str(row[1])) if row else None

async def set_user_consent(user_id: int, accepted: int, policy_hash: str):
    now = dt.datetime.now(dt.UTC).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO user_consents(user_id, accepted, policy_hash, accepted_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            accepted=excluded.accepted,
            policy_hash=excluded.policy_hash,
            accepted_at=excluded.accepted_at
        """, (user_id, accepted, policy_hash, now))
        await db.commit()

async def user_is_allowed(user_id: int) -> bool:
    row = await get_user_consent(user_id)
    if not row:
        return False
    accepted, ph = row
    # если политика обновилась — потребуется принять заново
    return accepted == 1 and ph == POLICY_HASH

# ---- channels/submissions helpers ----
async def upsert_channel(chat_id: int, username: Optional[str], owner_user_id: int, moderation: int = 1):
    now = dt.datetime.now(dt.UTC).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO channels(chat_id, username, owner_user_id, moderation, reviewers_mode, created_at)
        VALUES(?, ?, ?, ?, 'owner', ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            username=excluded.username,
            owner_user_id=excluded.owner_user_id
        """, (chat_id, username, owner_user_id, moderation, now))
        await db.commit()

async def get_channel_by_chat_id(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT chat_id, username, owner_user_id, moderation, reviewers_mode FROM channels WHERE chat_id=?",
            (chat_id,),
        ) as cur:
            return await cur.fetchone()

async def get_channels_by_owner(owner_user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT chat_id, username, owner_user_id, moderation, reviewers_mode FROM channels WHERE owner_user_id=? ORDER BY chat_id",
            (owner_user_id,),
        ) as cur:
            return await cur.fetchall()

async def set_channel_moderation(chat_id: int, moderation: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE channels SET moderation=? WHERE chat_id=?", (moderation, chat_id))
        await db.commit()

async def set_reviewers_mode(chat_id: int, mode: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE channels SET reviewers_mode=? WHERE chat_id=?", (mode, chat_id))
        await db.commit()

async def add_reviewer(chat_id: int, user_id: int):
    now = dt.datetime.now(dt.UTC).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO channel_reviewers(chat_id, user_id, created_at) VALUES(?, ?, ?)",
            (chat_id, user_id, now),
        )
        await db.commit()

async def remove_reviewer(chat_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM channel_reviewers WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        await db.commit()

async def list_reviewers(chat_id: int) -> List[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id FROM channel_reviewers WHERE chat_id=? ORDER BY user_id",
            (chat_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [int(r[0]) for r in rows]

async def create_deeplink(code: str, chat_id: int):
    now = dt.datetime.now(dt.UTC).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO deeplinks(code, chat_id, created_at) VALUES(?, ?, ?)",
            (code, chat_id, now),
        )
        await db.commit()

async def resolve_deeplink(code: str) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT chat_id FROM deeplinks WHERE code=?", (code,)) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else None

async def create_submission(
    chat_id: int,
    sender_user_id: int,
    content_type: str,
    text: Optional[str],
    file_id: Optional[str],
    status: str,
) -> int:
    now = dt.datetime.now(dt.UTC).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
        INSERT INTO submissions(chat_id, sender_user_id, content_type, text, file_id, status, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, sender_user_id, content_type, text, file_id, status, now))
        await db.commit()
        return cur.lastrowid

async def get_submission(sub_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
        SELECT id, chat_id, sender_user_id, content_type, text, file_id, status
        FROM submissions WHERE id=?
        """, (sub_id,)) as cur:
            return await cur.fetchone()

async def set_submission_status(sub_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE submissions SET status=? WHERE id=?", (status, sub_id))
        await db.commit()

async def list_pending_submissions(chat_id: int, limit: int = 10, offset: int = 0):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT id, content_type, COALESCE(text,'') as text
            FROM submissions
            WHERE chat_id=? AND status=?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, (chat_id, STATUS_PENDING, limit, offset)) as cur:
            return await cur.fetchall()

async def count_pending_submissions(chat_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT COUNT(*) FROM submissions WHERE chat_id=? AND status=?
        """, (chat_id, STATUS_PENDING)) as cur:
            row = await cur.fetchone()
            return int(row[0] or 0)

# ----------------- HELPERS -----------------
CHANNEL_INPUT_RE = re.compile(r"^@?[A-Za-z0-9_]{5,}$|^-100\d{5,}$")

def normalize_channel_input(s: str) -> str:
    s = s.strip()
    if s.startswith("@"):
        return s
    if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
        return s
    if re.fullmatch(r"[A-Za-z0-9_]{5,}", s):
        return "@" + s
    return s

def make_code_for_chat(chat_id: int) -> str:
    raw = f"{chat_id}:{DEEPLINK_SALT}".encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    b32 = base64.b32encode(digest).decode("ascii").rstrip("=")
    return b32[:20]

# ----------------- UI -----------------
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📨 Отправить", callback_data="menu_send")],
        [InlineKeyboardButton("🛠 Контролировать", callback_data="menu_control")],
        [InlineKeyboardButton("📜 Правила и анонимность", callback_data="menu_policy")],
    ])

def back_to_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="menu_back")]])

def send_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Ввести канал", callback_data="send_pick_channel")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu_back")],
    ])

def control_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Привязать канал", callback_data="ctl_bind")],
        [InlineKeyboardButton("📋 Мои каналы", callback_data="ctl_list")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu_back")],
    ])

def reviewers_manage_kb(chat_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить user_id", callback_data=f"rv_add:{chat_id}")],
        [InlineKeyboardButton("➖ Удалить user_id", callback_data=f"rv_del:{chat_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"ch_open:{chat_id}")],
    ])

def channel_controls(chat_id: int, moderation: int, reviewers_mode: str):
    mode_title = {"owner": "Только владелец", "admins": "Все админы", "selected": "Выбранные"}.get(reviewers_mode, reviewers_mode)
    kb = [
        [InlineKeyboardButton(
            f"Модерация: {'ВКЛ ✅' if moderation == 1 else 'ВЫКЛ ❎'}",
            callback_data=f"ch_toggle:{chat_id}"
        )],
        [InlineKeyboardButton(
            f"Проверяют: {mode_title}",
            callback_data=f"ch_reviewers_mode:{chat_id}"
        )],
        [InlineKeyboardButton("🗂 Очередь на проверку", callback_data=f"ch_queue:{chat_id}")],
        [InlineKeyboardButton("🔗 Ссылка для отправки", callback_data=f"ch_link:{chat_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="ctl_list")],
    ]
    if reviewers_mode == "selected":
        kb.insert(3, [InlineKeyboardButton("👥 Управлять проверяющими", callback_data=f"ch_reviewers_manage:{chat_id}")])
    return InlineKeyboardMarkup(kb)

def confirm_send_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Отправить", callback_data="send_confirm")],
        [InlineKeyboardButton("❌ Отмена", callback_data="send_cancel")],
    ])

def ticket_kb(sub_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Одобрить", callback_data=f"mod_ok:{sub_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"mod_no:{sub_id}"),
        ]
    ])

def queue_kb(chat_id: int, items, total: int, offset: int, limit: int = 10):
    kb = []
    for sid, ctype, txt in items:
        preview = (txt[:30] + "…") if len(txt) > 30 else txt
        label = f"#{sid} | {ctype}" + (f" | {preview}" if preview else "")
        kb.append([InlineKeyboardButton(label, callback_data=f"q_open:{chat_id}:{sid}")])

    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"q_page:{chat_id}:{max(0, offset-limit)}"))
    if offset + limit < total:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"q_page:{chat_id}:{offset+limit}"))
    if nav:
        kb.append(nav)

    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"ch_open:{chat_id}")])
    return InlineKeyboardMarkup(kb)

def policy_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Принять и продолжить", callback_data="policy_accept")],
        [InlineKeyboardButton("❌ Отказаться и выйти", callback_data="policy_decline")],
    ])

def policy_back_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu_back")]
    ])

# ----------------- Permissions -----------------
async def verify_bind(
    context: ContextTypes.DEFAULT_TYPE,
    channel_input: str,
    user_id: int
) -> Tuple[bool, str, Optional[int], Optional[str]]:
    """
    bind allowed only if:
    - bot is admin of the channel
    - user is creator/owner of the channel
    """
    try:
        chat = await context.bot.get_chat(channel_input)
        chat_id = chat.id
        username = chat.username
    except (BadRequest, Forbidden) as e:
        return False, f"Не могу получить канал. Проверь @username/chat_id и доступ. ({e})", None, None

    bot_id = context.bot.id
    try:
        bot_member = await context.bot.get_chat_member(chat_id, bot_id)
    except (BadRequest, Forbidden) as e:
        return False, f"Не могу проверить права бота. ({e})", None, None

    if bot_member.status != ChatMemberStatus.ADMINISTRATOR:
        return False, "Бот не администратор канала. Выдай боту админку (права на постинг).", None, None

    try:
        user_member = await context.bot.get_chat_member(chat_id, user_id)
    except (BadRequest, Forbidden) as e:
        return False, f"Не могу проверить владельца. ({e})", None, None

    if user_member.status != ChatMemberStatus.OWNER:
        return False, "Привязать канал может только владелец (creator), не админ.", None, None

    return True, "OK", chat_id, username

async def ensure_registered_channel(context: ContextTypes.DEFAULT_TYPE, channel_input: str) -> Optional[int]:
    """Send allowed only into registered channels."""
    try:
        chat = await context.bot.get_chat(channel_input)
        row = await get_channel_by_chat_id(chat.id)
        return chat.id if row else None
    except Exception:
        return None

async def can_moderate(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    row = await get_channel_by_chat_id(chat_id)
    if not row:
        return False
    _, _, owner_user_id, _, reviewers_mode = row
    owner_user_id = int(owner_user_id)

    if user_id == owner_user_id:
        return True

    if reviewers_mode == "owner":
        return False

    if reviewers_mode == "selected":
        reviewers = await list_reviewers(chat_id)
        return user_id in reviewers

    if reviewers_mode == "admins":
        try:
            m = await context.bot.get_chat_member(chat_id, user_id)
            return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
        except Exception:
            return False

    return False

# ----------------- STATE (in-memory) -----------------
USER_STATE: Dict[int, Dict[str, Any]] = {}

def st(uid: int) -> Dict[str, Any]:
    return USER_STATE.setdefault(uid, {
        "mode": None,               # send_pick_channel | send_wait_content | ctl_bind_wait | rv_add_wait | rv_del_wait
        "selected_chat_id": None,   # int
        "pending": None,            # dict(content_type,text,file_id)
        "rv_chat_id": None,         # int
    })

def reset_send(uid: int):
    s = st(uid)
    s["mode"] = None
    s["selected_chat_id"] = None
    s["pending"] = None

# ----------------- Consent gate -----------------
async def ensure_consent_or_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Returns True if user already accepted current policy.
    Otherwise shows policy prompt and returns False.
    """
    uid = update.effective_user.id if update.effective_user else None
    if not uid:
        return False

    if await user_is_allowed(uid):
        return True

    # показать политику
    target = None
    if update.message:
        target = update.message
    elif update.callback_query:
        target = update.callback_query.message

    if target:
        # Если политика обновилась — это тоже сюда попадёт
        try:
            await target.reply_text(
                POLICY_TEXT,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=policy_kb(),
                disable_web_page_preview=True,
            )
        except Exception as e:
            # fallback без markdown если файл сломан
            await target.reply_text(
                "Не могу отобразить политику (ошибка форматирования MarkdownV2). "
                "Проверь privacy_anon.md.\n\n"
                f"Тех. ошибка: {e}"
            )
    return False

# ----------------- Update log (minimal) -----------------
async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug("Update received")

# ----------------- Core actions -----------------
async def post_to_channel(context: ContextTypes.DEFAULT_TYPE, chat_id: int, pending: Dict[str, Any]):
    ctype = pending["content_type"]
    text = (pending.get("text") or "").strip()
    fid = pending.get("file_id")

    if ctype == "text":
        await context.bot.send_message(chat_id, text)
    elif ctype == "photo":
        await context.bot.send_photo(chat_id, fid, caption=text if text else None)
    elif ctype == "video":
        await context.bot.send_video(chat_id, fid, caption=text if text else None)
    elif ctype == "document":
        await context.bot.send_document(chat_id, fid, caption=text if text else None)
    elif ctype == "audio":
        await context.bot.send_audio(chat_id, fid, caption=text if text else None)
    elif ctype == "voice":
        await context.bot.send_voice(chat_id, fid, caption=text if text else None)
    else:
        await context.bot.send_message(chat_id, text)

async def send_ticket_to_owner(context: ContextTypes.DEFAULT_TYPE, owner_user_id: int, sub_id: int, pending: Dict[str, Any]):
    text_preview = (pending.get("text") or "").strip()
    ctype = pending["content_type"]

    header = f"🆕 На проверку #{sub_id}\nТип: {ctype}"
    if text_preview:
        header += f"\n\nТекст/подпись:\n{text_preview}"

    try:
        if ctype == "text":
            await context.bot.send_message(owner_user_id, header, reply_markup=ticket_kb(sub_id))
        elif ctype == "photo":
            await context.bot.send_photo(owner_user_id, pending["file_id"], caption=header, reply_markup=ticket_kb(sub_id))
        elif ctype == "video":
            await context.bot.send_video(owner_user_id, pending["file_id"], caption=header, reply_markup=ticket_kb(sub_id))
        elif ctype == "document":
            await context.bot.send_document(owner_user_id, pending["file_id"], caption=header, reply_markup=ticket_kb(sub_id))
        elif ctype == "audio":
            await context.bot.send_audio(owner_user_id, pending["file_id"], caption=header, reply_markup=ticket_kb(sub_id))
        elif ctype == "voice":
            await context.bot.send_voice(owner_user_id, pending["file_id"], caption=header, reply_markup=ticket_kb(sub_id))
        else:
            await context.bot.send_message(owner_user_id, header, reply_markup=ticket_kb(sub_id))
    except Exception:
        pass

# ----------------- ERROR HANDLER -----------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled exception", exc_info=context.error)
    if BOT_OWNER_ID:
        try:
            await context.bot.send_message(
                BOT_OWNER_ID,
                f"⚠️ Ошибка: {type(context.error).__name__}: {context.error}"
            )
        except Exception:
            pass

# ----------------- Handlers -----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # gate
    if not await ensure_consent_or_show(update, context):
        return

    uid = update.effective_user.id
    code = context.args[0].strip() if context.args else ""

    if code:
        chat_id = await resolve_deeplink(code)
        if chat_id:
            s = st(uid)
            s["selected_chat_id"] = chat_id
            await update.message.reply_text(
                "Канал выбран по ссылке. Нажми «Отправить».",
                reply_markup=main_menu()
            )
            return

    await update.message.reply_text("Меню:", reply_markup=main_menu())

async def on_policy_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "policy_accept":
        await set_user_consent(uid, 1, POLICY_HASH)
        await event_log(context, "Пользователь принял политику (без идентификации).")
        try:
            await q.edit_message_text("✅ Принято. Продолжаем.", reply_markup=main_menu())
        except Exception:
            await q.message.reply_text("✅ Принято. Продолжаем.", reply_markup=main_menu())
        return

    if data == "policy_decline":
        await set_user_consent(uid, 0, POLICY_HASH)
        await event_log(context, "Пользователь отказался от политики (без идентификации).")
        try:
            await q.edit_message_text("❌ Ок. Для использования бота нужно принять политику.")
        except Exception:
            await q.message.reply_text("❌ Ок. Для использования бота нужно принять политику.")
        return

async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # gate (кроме кнопок политики)
    if not await ensure_consent_or_show(update, context):
        return

    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "menu_back":
        reset_send(uid)
        await q.edit_message_text("Меню:", reply_markup=main_menu())
        return

    if data == "menu_policy":
        # показать политику с кнопкой назад
        try:
            await q.edit_message_text(
                POLICY_TEXT,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=policy_back_kb(),
                disable_web_page_preview=True,
            )
        except Exception as e:
            await q.edit_message_text(
                "Не могу отобразить политику (ошибка форматирования MarkdownV2). "
                "Проверь privacy_anon.md.\n\n"
                f"Тех. ошибка: {e}",
                reply_markup=policy_back_kb(),
            )
        return

    if data == "menu_send":
        await q.edit_message_text(
            "Отправка анонимного сообщения.\n"
            "Если ты зашёл по ссылке канала — он уже выбран.\n"
            "Иначе нажми «Ввести канал».",
            reply_markup=send_menu()
        )
        return

    if data == "send_pick_channel":
        s = st(uid)
        s["mode"] = "send_pick_channel"
        await q.edit_message_text(
            "Введи @username канала или chat_id (-100...).\n"
            "Канал должен быть предварительно привязан владельцем через «Контролировать».",
            reply_markup=back_to_menu()
        )
        return

    if data == "menu_control":
        await q.edit_message_text("Контроль:", reply_markup=control_menu())
        return

    if data == "ctl_bind":
        s = st(uid)
        s["mode"] = "ctl_bind_wait"
        await q.edit_message_text(
            "Привязка канала.\n"
            "Введи @username канала или chat_id (-100...).\n\n"
            "Требования:\n"
            "• бот админ канала\n"
            "• привязать может только creator (владелец)\n",
            reply_markup=back_to_menu()
        )
        return

    if data == "ctl_list":
        channels = await get_channels_by_owner(uid)
        if not channels:
            await q.edit_message_text("У тебя нет привязанных каналов.", reply_markup=control_menu())
            return

        kb = []
        for chat_id, username, _, moderation, reviewers_mode in channels:
            title = f"@{username}" if username else str(chat_id)
            mode_title = {"owner": "владелец", "admins": "админы", "selected": "выбранные"}.get(reviewers_mode, reviewers_mode)
            kb.append([InlineKeyboardButton(
                f"{title} | мод:{'ON' if moderation else 'OFF'} | {mode_title}",
                callback_data=f"ch_open:{chat_id}"
            )])
        kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu_control")])
        await q.edit_message_text("Мои каналы:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("ch_open:"):
        chat_id = int(data.split(":", 1)[1])
        row = await get_channel_by_chat_id(chat_id)
        if not row or int(row[2]) != uid:
            await q.edit_message_text("Нет доступа.", reply_markup=control_menu())
            return

        _, username, _, moderation, reviewers_mode = row
        title = f"@{username}" if username else str(chat_id)
        await q.edit_message_text(
            f"Канал: {title}\nchat_id: {chat_id}",
            reply_markup=channel_controls(chat_id, moderation, reviewers_mode)
        )
        return

    if data.startswith("ch_toggle:"):
        chat_id = int(data.split(":", 1)[1])
        row = await get_channel_by_chat_id(chat_id)
        if not row or int(row[2]) != uid:
            await q.answer("Нет доступа", show_alert=True)
            return
        _, _, _, moderation, reviewers_mode = row
        new_val = 0 if int(moderation) == 1 else 1
        await set_channel_moderation(chat_id, new_val)
        await q.edit_message_reply_markup(reply_markup=channel_controls(chat_id, new_val, reviewers_mode))
        await q.answer("Готово")
        await event_log(context, f"Модерация переключена: channel={chat_id}, moderation={new_val}")
        return

    if data.startswith("ch_reviewers_mode:"):
        chat_id = int(data.split(":", 1)[1])
        row = await get_channel_by_chat_id(chat_id)
        if not row or int(row[2]) != uid:
            await q.answer("Только владелец может менять это", show_alert=True)
            return

        _, _, _, moderation, reviewers_mode = row
        order = ["owner", "admins", "selected"]
        new_mode = order[(order.index(reviewers_mode) + 1) % len(order)]
        await set_reviewers_mode(chat_id, new_mode)

        await q.edit_message_reply_markup(reply_markup=channel_controls(chat_id, int(moderation), new_mode))
        await q.answer("Готово")
        await event_log(context, f"Режим проверяющих изменён: channel={chat_id}, mode={new_mode}")
        return

    if data.startswith("ch_reviewers_manage:"):
        chat_id = int(data.split(":", 1)[1])
        row = await get_channel_by_chat_id(chat_id)
        if not row or int(row[2]) != uid:
            await q.answer("Нет доступа", show_alert=True)
            return

        reviewers = await list_reviewers(chat_id)
        txt = "Проверяющие (user_id):\n" + ("\n".join(map(str, reviewers)) if reviewers else "— пусто —")
        await q.edit_message_text(txt, reply_markup=reviewers_manage_kb(chat_id))
        return

    if data.startswith("rv_add:"):
        chat_id = int(data.split(":", 1)[1])
        row = await get_channel_by_chat_id(chat_id)
        if not row or int(row[2]) != uid:
            await q.answer("Нет доступа", show_alert=True)
            return
        s = st(uid)
        s["mode"] = "rv_add_wait"
        s["rv_chat_id"] = chat_id
        await q.edit_message_text("Пришли user_id, которого добавить в проверяющие.", reply_markup=reviewers_manage_kb(chat_id))
        return

    if data.startswith("rv_del:"):
        chat_id = int(data.split(":", 1)[1])
        row = await get_channel_by_chat_id(chat_id)
        if not row or int(row[2]) != uid:
            await q.answer("Нет доступа", show_alert=True)
            return
        s = st(uid)
        s["mode"] = "rv_del_wait"
        s["rv_chat_id"] = chat_id
        await q.edit_message_text("Пришли user_id, которого удалить из проверяющих.", reply_markup=reviewers_manage_kb(chat_id))
        return

    if data.startswith("ch_queue:"):
        chat_id = int(data.split(":", 1)[1])
        if not await can_moderate(context, chat_id, uid):
            await q.answer("Нет доступа", show_alert=True)
            return

        total = await count_pending_submissions(chat_id)
        items = await list_pending_submissions(chat_id, limit=10, offset=0)
        await q.edit_message_text(
            f"Очередь на проверку (pending): {total}",
            reply_markup=queue_kb(chat_id, items, total, offset=0)
        )
        return

    if data.startswith("q_page:"):
        _, chat_id_s, offset_s = data.split(":")
        chat_id = int(chat_id_s)
        offset = int(offset_s)
        if not await can_moderate(context, chat_id, uid):
            await q.answer("Нет доступа", show_alert=True)
            return
        total = await count_pending_submissions(chat_id)
        items = await list_pending_submissions(chat_id, limit=10, offset=offset)
        await q.edit_message_text(
            f"Очередь на проверку (pending): {total}",
            reply_markup=queue_kb(chat_id, items, total, offset=offset)
        )
        return

    if data.startswith("q_open:"):
        _, chat_id_s, sid_s = data.split(":")
        chat_id = int(chat_id_s)
        sid = int(sid_s)
        if not await can_moderate(context, chat_id, uid):
            await q.answer("Нет доступа", show_alert=True)
            return

        row = await get_submission(sid)
        if not row or row[6] != STATUS_PENDING:
            await q.answer("Уже обработано", show_alert=True)
            return

        _id, _chat_id_db, _sender_user_id, content_type, text, file_id, _status = row
        header = f"🧾 Заявка #{sid}\nТип: {content_type}"
        if text:
            header += f"\n\nТекст/подпись:\n{text}"

        try:
            if content_type == "text":
                await q.edit_message_text(header, reply_markup=ticket_kb(sid))
            else:
                try:
                    await q.message.delete()
                except Exception:
                    pass
                pending = {"content_type": content_type, "text": text, "file_id": file_id}
                await send_ticket_to_owner(context, uid, sid, pending)
        except Exception:
            await q.edit_message_text(header, reply_markup=ticket_kb(sid))
        return

    if data.startswith("ch_link:"):
        chat_id = int(data.split(":", 1)[1])
        row = await get_channel_by_chat_id(chat_id)
        if not row or int(row[2]) != uid:
            await q.answer("Нет доступа", show_alert=True)
            return

        code = make_code_for_chat(chat_id)
        await create_deeplink(code, chat_id)

        me = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start={code}"

        await q.edit_message_text(
            "Ссылка для отправки в этот канал:\n"
            f"{link}\n\n"
            "Пользователь перейдёт по ссылке → бот запомнит канал → «Отправить».",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"ch_open:{chat_id}")]])
        )
        await event_log(context, f"Сгенерирована ссылка: channel={chat_id}")
        return

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # gate
    if not await ensure_consent_or_show(update, context):
        return

    uid = update.effective_user.id
    s = st(uid)
    text = (update.message.text or "").strip()

    # manage selected reviewers add/del
    if s.get("mode") in ("rv_add_wait", "rv_del_wait"):
        chat_id = int(s.get("rv_chat_id") or 0)
        row = await get_channel_by_chat_id(chat_id)
        if not row or int(row[2]) != uid:
            await update.message.reply_text("Нет доступа.", reply_markup=main_menu())
            s["mode"] = None
            s["rv_chat_id"] = None
            return

        if not text.isdigit():
            await update.message.reply_text("Нужен числовой user_id.")
            return

        target = int(text)
        if s["mode"] == "rv_add_wait":
            await add_reviewer(chat_id, target)
            await update.message.reply_text(f"Добавлен: {target}", reply_markup=main_menu())
            await event_log(context, f"Добавлен проверяющий: channel={chat_id}")
        else:
            await remove_reviewer(chat_id, target)
            await update.message.reply_text(f"Удалён: {target}", reply_markup=main_menu())
            await event_log(context, f"Удалён проверяющий: channel={chat_id}")

        s["mode"] = None
        s["rv_chat_id"] = None
        return

    # bind flow
    if s.get("mode") == "ctl_bind_wait":
        channel_in = normalize_channel_input(text)
        if not CHANNEL_INPUT_RE.match(channel_in):
            await update.message.reply_text("Неверный формат. Введи @username или -100....")
            return

        ok, reason, chat_id, username = await verify_bind(context, channel_in, uid)
        if not ok:
            await update.message.reply_text(f"❌ Не удалось привязать: {reason}")
            return

        await upsert_channel(chat_id, username, uid, moderation=1)
        s["mode"] = None

        await update.message.reply_text(
            f"✅ Канал привязан.\nchat_id: {chat_id}\nusername: {('@'+username) if username else 'нет'}\nМодерация: ВКЛ",
            reply_markup=main_menu()
        )
        await event_log(context, f"Канал привязан: channel={chat_id}")
        return

    # send: pick channel
    if s.get("mode") == "send_pick_channel":
        channel_in = normalize_channel_input(text)
        if not CHANNEL_INPUT_RE.match(channel_in):
            await update.message.reply_text("Неверный формат. Введи @username или -100....")
            return

        registered_chat_id = await ensure_registered_channel(context, channel_in)
        if not registered_chat_id:
            await update.message.reply_text(
                "❌ Этот канал не зарегистрирован.\n"
                "Его должен сначала привязать владелец через «Контролировать»."
            )
            return

        s["selected_chat_id"] = registered_chat_id
        s["mode"] = "send_wait_content"
        await update.message.reply_text(
            "Канал выбран.\nТеперь пришли текст или медиа (фото/видео/файл/голос) и, если нужно, подпись.\n"
            "После этого появится кнопка «Отправить».",
            reply_markup=back_to_menu()
        )
        return

    # if channel selected, treat text as content
    if s.get("selected_chat_id") and (s.get("mode") in (None, "send_wait_content")):
        if len(text) < 1:
            await update.message.reply_text("Пустое сообщение.")
            return
        s["pending"] = {"content_type": "text", "text": text, "file_id": None}
        s["mode"] = "send_wait_content"
        await update.message.reply_text("Готово. Подтверди отправку:", reply_markup=confirm_send_kb())
        return

    await update.message.reply_text("Нажми /start и выбери действие.", reply_markup=main_menu())

async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # gate
    if not await ensure_consent_or_show(update, context):
        return

    uid = update.effective_user.id
    s = st(uid)

    if not s.get("selected_chat_id"):
        await update.message.reply_text("Сначала выбери канал: /start → «Отправить» → «Ввести канал».")
        return

    msg: Message = update.message
    content_type = None
    file_id = None
    text = None

    if msg.photo:
        content_type = "photo"
        file_id = msg.photo[-1].file_id
        text = msg.caption or ""
    elif msg.video:
        content_type = "video"
        file_id = msg.video.file_id
        text = msg.caption or ""
    elif msg.document:
        content_type = "document"
        file_id = msg.document.file_id
        text = msg.caption or ""
    elif msg.audio:
        content_type = "audio"
        file_id = msg.audio.file_id
        text = msg.caption or ""
    elif msg.voice:
        content_type = "voice"
        file_id = msg.voice.file_id
        text = msg.caption or ""
    else:
        return

    s["pending"] = {"content_type": content_type, "text": text, "file_id": file_id}
    s["mode"] = "send_wait_content"
    await update.message.reply_text("Файл получен. Подтверди отправку:", reply_markup=confirm_send_kb())

async def on_send_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # gate
    if not await ensure_consent_or_show(update, context):
        return

    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    s = st(uid)

    if q.data == "send_cancel":
        reset_send(uid)
        await q.edit_message_text("Отменено.", reply_markup=main_menu())
        return

    if q.data != "send_confirm":
        return

    chat_id = s.get("selected_chat_id")
    pending = s.get("pending")

    if not chat_id or not pending:
        await q.edit_message_text("Нечего отправлять.", reply_markup=main_menu())
        reset_send(uid)
        return

    row = await get_channel_by_chat_id(int(chat_id))
    if not row:
        await q.edit_message_text("Канал не зарегистрирован владельцем.", reply_markup=main_menu())
        reset_send(uid)
        return

    _, _, owner_user_id, moderation, _reviewers_mode = row
    owner_user_id = int(owner_user_id)

    if int(moderation) == 1:
        sub_id = await create_submission(
            chat_id=int(chat_id),
            sender_user_id=uid,
            content_type=pending["content_type"],
            text=pending.get("text"),
            file_id=pending.get("file_id"),
            status=STATUS_PENDING
        )
        await q.edit_message_text("Сообщение отправлено на проверку 🕵️‍♂️", reply_markup=main_menu())
        await send_ticket_to_owner(context, owner_user_id, sub_id, pending)

        await event_log(context, f"Новое сообщение на проверку: channel={chat_id}, sid={sub_id}")
        reset_send(uid)
        return

    # direct post
    try:
        await post_to_channel(context, int(chat_id), pending)
        await create_submission(int(chat_id), uid, pending["content_type"], pending.get("text"), pending.get("file_id"), STATUS_SENT)
        await q.edit_message_text("Сообщение отправлено ✅", reply_markup=main_menu())
        await event_log(context, f"Сообщение отправлено напрямую: channel={chat_id}")
    except Exception as e:
        await q.edit_message_text(f"Не смог отправить в канал. Ошибка: {e}", reply_markup=main_menu())
        await event_log(context, f"Ошибка отправки в канал: channel={chat_id}")
    finally:
        reset_send(uid)

async def on_moderation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # gate
    if not await ensure_consent_or_show(update, context):
        return

    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if not (data.startswith("mod_ok:") or data.startswith("mod_no:")):
        return

    sub_id = int(data.split(":", 1)[1])
    row = await get_submission(sub_id)
    if not row:
        await q.edit_message_text("Заявка не найдена.")
        return

    _id, chat_id, sender_user_id, content_type, text, file_id, status = row
    if status != STATUS_PENDING:
        await q.edit_message_text("Уже обработано.")
        return

    if not await can_moderate(context, int(chat_id), uid):
        await q.edit_message_text("Нет доступа.")
        return

    pending = {"content_type": content_type, "text": text, "file_id": file_id}

    if data.startswith("mod_ok:"):
        try:
            await context.bot.send_message(sender_user_id, "Отправка сообщения была одобрена проверкой ✅")
        except Exception:
            pass

        try:
            await post_to_channel(context, int(chat_id), pending)
            await set_submission_status(sub_id, STATUS_SENT)
            try:
                await context.bot.send_message(sender_user_id, "Сообщение отправлено ✅")
            except Exception:
                pass
            await q.edit_message_text("✅ Одобрено и опубликовано")
            await event_log(context, f"Сообщение одобрено и отправлено: channel={chat_id}, sid={sub_id}")
        except Exception as e:
            await q.edit_message_text(f"Ошибка отправки в канал: {e}")
            await event_log(context, f"Ошибка при публикации после одобрения: channel={chat_id}, sid={sub_id}")
        return

    # reject
    await set_submission_status(sub_id, STATUS_REJECTED)
    try:
        await context.bot.send_message(sender_user_id, "Сообщение отклонено ❌")
    except Exception:
        pass
    await q.edit_message_text("❌ Отклонено")
    await event_log(context, f"Сообщение отклонено: channel={chat_id}, sid={sub_id}")

# ----------------- MAIN -----------------
def main():
    setup_logging()

    # Termux / Python 3.12: ensure event loop exists
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(db_init_and_migrate())

    logger.info("Application starting")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_error_handler(on_error)

    # minimal raw update logging (debug)
    app.add_handler(TypeHandler(Update, log_update), group=-100)

    app.add_handler(CommandHandler("start", start_cmd))

    # policy accept/decline must work even if user not accepted yet
    app.add_handler(CallbackQueryHandler(on_policy_callbacks, pattern=r"^(policy_accept|policy_decline)$"))

    # order matters: moderation callbacks first, then send buttons, then menu
    app.add_handler(CallbackQueryHandler(on_moderation, pattern=r"^(mod_ok:|mod_no:)"))
    app.add_handler(CallbackQueryHandler(on_send_buttons, pattern=r"^(send_confirm|send_cancel)$"))
    app.add_handler(CallbackQueryHandler(on_menu))  # everything else (menus, queue, settings)

    # media first
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.AUDIO | filters.VOICE,
        on_media
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling(drop_pending_updates=True)

    logger.info("Application stopped")

if __name__ == "__main__":
    main()
