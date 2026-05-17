import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from html import escape
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

DB_PATH = os.getenv("DB_PATH", "posts_chat.db")
BOT_TOKEN_FILE = os.getenv("BOT_TOKEN_FILE", "bot_token.txt")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "7704968798")
ADMIN_IDS = {int(part.strip()) for part in ADMIN_IDS_RAW.split(",") if part.strip().isdigit()}

BTN_CREATE_POST = "✏️ Пост"
BTN_VIEW_POSTS = "🔍 Смотреть"
BTN_END_CHAT = "🚪 Выйти"
BTN_BACK = "◀️ Назад"
BTN_ACCEPT_RULES = "✅ 18+ Принимаю"
BTN_PREMIUM = "💎 Премиум"

# Старые подписи кнопок (если клавиатура не обновилась у пользователя)
LEGACY_MENU = frozenset({
    "✏️ Сделать свой пост",
    "🔍 Смотреть посты",
    "🚪 Прекратить диалог",
    "🏠 В главное меню",
})

MAX_POST_LENGTH = 500
ONLINE_THRESHOLD = timedelta(minutes=5)
PREMIUM_HOURS_PER_REFERRAL = 1

RULES_TEXT = (
    "📜 <b>Правила RandomCircle</b>\n\n"
    "🔞 <b>Только 18+.</b> Если вам нет 18 лет — покиньте бота.\n\n"
    "📋 <b>Запрещено:</b>\n"
    "• контент для несовершеннолетних, насилие, угрозы;\n"
    "• спам, реклама, мошенничество;\n"
    "• оскорбления, домогательства, разжигание ненависти;\n"
    "• фото и файлы в постах — <b>только текст и смайлики</b>;\n"
    "• нарушение <a href=\"https://telegram.org/tos\">правил Telegram</a>.\n\n"
    "⚖️ За нарушения — блокировка без предупреждения.\n"
    "🤝 Будьте вежливы. Не передавайте личные данные незнакомцам.\n\n"
    "Нажимая кнопку ниже, вы подтверждаете возраст 18+ и согласие с правилами."
)

MENU_BUTTONS = frozenset({
    BTN_CREATE_POST,
    BTN_VIEW_POSTS,
    BTN_PREMIUM,
    BTN_END_CHAT,
    BTN_BACK,
    BTN_ACCEPT_RULES,
}) | LEGACY_MENU

PREMIUM_TEXT = (
    "💎 <b>Премиум RandomCircle</b>\n\n"
    "С премиумом ваш пост <b>всегда выше остальных</b> — его видят первым "
    "при просмотре анкет. Вы получите <b>намного больше просмотров</b>, "
    "сообщений и чатов!\n\n"
    "🎁 <b>Как получить бесплатно:</b>\n"
    "Пригласите друга по своей ссылке. Когда он примет правила и зайдёт в бота — "
    f"ваш пост будет в <b>ТОПе {PREMIUM_HOURS_PER_REFERRAL} час</b> за каждого друга.\n\n"
    "⏱ Часы премиума <b>суммируются</b> — пригласили 5 друзей = 5 часов в топе.\n\n"
    "👇 Ваша реферальная ссылка:"
)


def read_secret(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as file:
        return file.read().strip()


BOT_TOKEN = os.getenv("BOT_TOKEN", read_secret(BOT_TOKEN_FILE))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Кэш для принятых правил (user_id -> bool)
RULES_ACCEPTED_CACHE: dict[int, bool] = {}

# Следующий пост при нажатии «Смотреть посты» (по user_id).
VIEWER_OFFSET: dict[int, int] = {}


class CreatePost(StatesGroup):
    waiting_content = State()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                photo_file_id TEXT,
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                initiator_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                initiator_post_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                ended_at TEXT
            );

            CREATE TABLE IF NOT EXISTS post_ratings (
                post_id INTEGER NOT NULL,
                rater_id INTEGER NOT NULL,
                rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                chat_id INTEGER,
                created_at TEXT NOT NULL,
                PRIMARY KEY (post_id, rater_id)
            );

            CREATE TABLE IF NOT EXISTS bans (
                user_id INTEGER PRIMARY KEY,
                banned_until TEXT,
                reason TEXT,
                banned_by INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                reporter_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (post_id, reporter_id)
            );

            CREATE TABLE IF NOT EXISTS referrals (
                referred_id INTEGER PRIMARY KEY,
                referrer_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "last_active_at" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN last_active_at TEXT")
        if "last_chat_at" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN last_chat_at TEXT")
        if "rules_accepted" not in user_cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN rules_accepted INTEGER NOT NULL DEFAULT 0"
            )
        if "premium_until" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN premium_until TEXT")
        if "pending_referrer_id" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN pending_referrer_id INTEGER")
        conn.commit()


def ensure_user(user_id: int) -> None:
    ts = now_iso()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO users (user_id, created_at, last_active_at, rules_accepted)
            VALUES (?, ?, ?, 0)
            """,
            (user_id, ts, ts),
        )
        conn.commit()


def touch_activity(user_id: int) -> None:
    ensure_user(user_id)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE users SET last_active_at = ? WHERE user_id = ?",
            (now_iso(), user_id),
        )
        conn.commit()


def touch_chat_activity(user_id: int) -> None:
    ts = now_iso()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            UPDATE users SET last_chat_at = ?, last_active_at = ? WHERE user_id = ?
            """,
            (ts, ts, user_id),
        )
        conn.commit()


def user_accepted_rules(user_id: int) -> bool:
    if user_id in RULES_ACCEPTED_CACHE:
        return RULES_ACCEPTED_CACHE[user_id]
    
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT rules_accepted FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        accepted = bool(row and row[0])
        if accepted:
            RULES_ACCEPTED_CACHE[user_id] = True
        return accepted


def accept_rules(user_id: int) -> int | None:
    ensure_user(user_id)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE users SET rules_accepted = 1, last_active_at = ? WHERE user_id = ?",
            (now_iso(), user_id),
        )
        conn.commit()
    RULES_ACCEPTED_CACHE[user_id] = True
    return process_pending_referral(user_id)


async def notify_referrer_premium(bot: Bot, referrer_id: int) -> None:
    try:
        await bot.send_message(
            referrer_id,
            f"🎉 <b>Новый друг по вашей ссылке!</b>\n\n"
            f"⏱ +{PREMIUM_HOURS_PER_REFERRAL} ч премиума — ваш пост в <b>ТОПе</b>!\n"
            f"Приглашайте ещё — раздел «💎 Премиум».",
        )
    except Exception:
        pass


def save_pending_referral(user_id: int, referrer_id: int) -> None:
    if user_id == referrer_id or referrer_id <= 0:
        return
    ensure_user(user_id)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT referred_id FROM referrals WHERE referred_id = ?",
            (user_id,),
        ).fetchone()
        if row:
            return
        conn.execute(
            """
            UPDATE users SET pending_referrer_id = ?
            WHERE user_id = ? AND pending_referrer_id IS NULL
            """,
            (referrer_id, user_id),
        )
        conn.commit()


def process_pending_referral(user_id: int) -> int | None:
    """Возвращает referrer_id, если реферал засчитан."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT pending_referrer_id FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row or not row["pending_referrer_id"]:
            return None
        referrer_id = int(row["pending_referrer_id"])
        if referrer_id == user_id:
            conn.execute(
                "UPDATE users SET pending_referrer_id = NULL WHERE user_id = ?",
                (user_id,),
            )
            conn.commit()
            return None
        exists = conn.execute(
            "SELECT 1 FROM referrals WHERE referred_id = ?",
            (user_id,),
        ).fetchone()
        if exists:
            conn.execute(
                "UPDATE users SET pending_referrer_id = NULL WHERE user_id = ?",
                (user_id,),
            )
            conn.commit()
            return None
        conn.execute(
            "INSERT INTO referrals (referred_id, referrer_id, created_at) VALUES (?, ?, ?)",
            (user_id, referrer_id, now_iso()),
        )
        conn.execute(
            "UPDATE users SET pending_referrer_id = NULL WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
    add_premium_hours(referrer_id, PREMIUM_HOURS_PER_REFERRAL)
    return referrer_id


def add_premium_hours(user_id: int, hours: int) -> None:
    ensure_user(user_id)
    now = datetime.now(timezone.utc)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT premium_until FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        base = now
        if row and row[0]:
            current = parse_iso(row[0])
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            if current > now:
                base = current
        new_until = (base + timedelta(hours=hours)).isoformat()
        conn.execute(
            "UPDATE users SET premium_until = ? WHERE user_id = ?",
            (new_until, user_id),
        )
        conn.commit()


def has_active_premium(user_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT premium_until FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row or not row[0]:
            return False
        until = parse_iso(row[0])
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= until:
            conn.execute(
                "UPDATE users SET premium_until = NULL WHERE user_id = ?",
                (user_id,),
            )
            conn.commit()
            return False
        return True


def get_premium_until(user_id: int) -> str | None:
    if not has_active_premium(user_id):
        return None
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT premium_until FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row[0] if row else None


def count_referrals(user_id: int) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?",
            (user_id,),
        ).fetchone()
        return int(row[0])


def _now_sql() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_user_activity(user_id: int) -> dict:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT last_active_at, last_chat_at FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return {"last_active_at": None, "last_chat_at": None}
        return dict(row)


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_time_remaining(iso_value: str) -> str:
    dt = parse_iso(iso_value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = int((dt - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        return "скоро"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин."
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч."
    days = hours // 24
    return f"{days} дн."


def format_time_ago(iso_value: str | None) -> str | None:
    if not iso_value:
        return None
    dt = parse_iso(iso_value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "только что"
    if seconds < 60:
        return "только что"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин. назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч. назад"
    days = hours // 24
    if days == 1:
        return "вчера"
    if days < 7:
        return f"{days} дн. назад"
    if days < 30:
        weeks = days // 7
        return f"{weeks} нед. назад"
    months = days // 30
    return f"{months} мес. назад"


def get_active_post(user_id: int) -> dict | None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, user_id, text, photo_file_id
            FROM posts
            WHERE user_id = ? AND is_active = 1
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def create_post(user_id: int, text: str) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE posts SET is_active = 0 WHERE user_id = ? AND is_active = 1",
            (user_id,),
        )
        cur = conn.execute(
            """
            INSERT INTO posts (user_id, text, photo_file_id, created_at, is_active)
            VALUES (?, ?, NULL, ?, 1)
            """,
            (user_id, text, now_iso()),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_post(post_id: int) -> dict | None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, user_id, text, photo_file_id FROM posts WHERE id = ? AND is_active = 1",
            (post_id,),
        ).fetchone()
        return dict(row) if row else None


def get_post_rating(post_id: int) -> tuple[float | None, int]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT AVG(rating), COUNT(*) FROM post_ratings WHERE post_id = ?",
            (post_id,),
        ).fetchone()
        avg, count = row[0], row[1]
        if not count:
            return None, 0
        return round(float(avg), 1), int(count)


def list_posts_for_viewer(viewer_id: int, offset: int = 0) -> list[dict]:
    ts = _now_sql()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT p.id, p.user_id, p.text, p.photo_file_id
            FROM posts p
            JOIN users u ON u.user_id = p.user_id
            WHERE p.is_active = 1 AND p.user_id != ?
            ORDER BY
                CASE
                    WHEN u.premium_until IS NOT NULL AND u.premium_until > ? THEN 0
                    ELSE 1
                END,
                p.id DESC
            LIMIT 1 OFFSET ?
            """,
            (viewer_id, ts, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def count_posts_for_viewer(viewer_id: int) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE is_active = 1 AND user_id != ?",
            (viewer_id,),
        ).fetchone()
        return int(row[0])


def get_active_chat(user_id: int) -> dict | None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, post_id, initiator_id, owner_id, initiator_post_id, status
            FROM chats
            WHERE status = 'active'
              AND (initiator_id = ? OR owner_id = ?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def get_chat(chat_id: int) -> dict | None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, post_id, initiator_id, owner_id, initiator_post_id, status
            FROM chats WHERE id = ?
            """,
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None


def start_chat(post_id: int, initiator_id: int) -> tuple[int | None, str | None]:
    post = get_post(post_id)
    if not post:
        return None, "Пост не найден."
    owner_id = post["user_id"]
    if owner_id == initiator_id:
        return None, "Нельзя начать чат со своим постом."

    initiator_post = get_active_post(initiator_id)
    if not initiator_post:
        return None, "Сначала создайте свой пост — без него нельзя начать чат."

    if get_active_chat(initiator_id):
        return None, "Вы уже в диалоге. Завершите его кнопкой «Выйти»."
    if get_active_chat(owner_id):
        return None, "Автор поста сейчас занят в другом диалоге."

    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            """
            INSERT INTO chats (post_id, initiator_id, owner_id, initiator_post_id, status, created_at)
            VALUES (?, ?, ?, ?, 'active', ?)
            """,
            (post_id, initiator_id, owner_id, initiator_post["id"], now_iso()),
        )
        conn.commit()
        touch_chat_activity(initiator_id)
        touch_chat_activity(owner_id)
        return int(cur.lastrowid), None


def end_chat(chat_id: int) -> dict | None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE chats SET status = 'ended', ended_at = ? WHERE id = ? AND status = 'active'",
            (now_iso(), chat_id),
        )
        conn.commit()
    chat = get_chat(chat_id)
    if chat:
        touch_chat_activity(chat["initiator_id"])
        touch_chat_activity(chat["owner_id"])
    return chat


def save_post_rating(post_id: int, rater_id: int, rating: int, chat_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT INTO post_ratings (post_id, rater_id, rating, chat_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(post_id, rater_id) DO UPDATE SET
                rating = excluded.rating,
                chat_id = excluded.chat_id,
                created_at = excluded.created_at
            """,
            (post_id, rater_id, rating, chat_id, now_iso()),
        )
        conn.commit()


def partner_id(chat: dict, user_id: int) -> int:
    return chat["owner_id"] if user_id == chat["initiator_id"] else chat["initiator_id"]


def rated_post_for_user(chat: dict, user_id: int) -> int:
    """Пост собеседника, который оцениваем после диалога."""
    if user_id == chat["initiator_id"]:
        return chat["post_id"]
    return chat["initiator_post_id"]


def get_total_stats() -> dict:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        users_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        posts_count = conn.execute("SELECT COUNT(*) FROM posts WHERE is_active = 1").fetchone()[0]
        chats_count = conn.execute("SELECT COUNT(*) FROM chats WHERE status = 'active'").fetchone()[0]
        total_chats = conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
        return {
            "users": users_count,
            "active_posts": posts_count,
            "active_chats": chats_count,
            "total_chats": total_chats
        }


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def delete_post(post_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "UPDATE posts SET is_active = 0 WHERE id = ? AND is_active = 1",
            (post_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def end_user_active_chats(user_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            UPDATE chats SET status = 'ended', ended_at = ?
            WHERE status = 'active' AND (initiator_id = ? OR owner_id = ?)
            """,
            (now_iso(), user_id, user_id),
        )
        conn.commit()


def ban_user(user_id: int, hours: int, reason: str, admin_id: int) -> None:
    if hours <= 0:
        banned_until = None
    else:
        banned_until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT INTO bans (user_id, banned_until, reason, banned_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                banned_until = excluded.banned_until,
                reason = excluded.reason,
                banned_by = excluded.banned_by,
                created_at = excluded.created_at
            """,
            (user_id, banned_until, reason, admin_id, now_iso()),
        )
        conn.commit()
    end_user_active_chats(user_id)


def unban_user(user_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM bans WHERE user_id = ?", (user_id,))
        conn.commit()


def get_ban_info(user_id: int) -> dict | None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT banned_until, reason FROM bans WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        banned_until = row["banned_until"]
        if banned_until is None:
            return {"permanent": True, "reason": row["reason"]}
        until_dt = parse_iso(banned_until)
        if until_dt.tzinfo is None:
            until_dt = until_dt.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= until_dt:
            unban_user(user_id)
            return None
        return {
            "permanent": False,
            "until": banned_until,
            "reason": row["reason"],
        }


def is_user_banned(user_id: int) -> bool:
    return get_ban_info(user_id) is not None


def ban_message_text(user_id: int) -> str:
    info = get_ban_info(user_id)
    if not info:
        return ""
    reason = escape(info.get("reason") or "нарушение правил")
    if info.get("permanent"):
        return f"🚫 <b>Вы заблокированы навсегда.</b>\n\nПричина: {reason}"
    remaining = format_time_remaining(info["until"])
    return (
        f"🚫 <b>Вы временно заблокированы.</b>\n\n"
        f"Причина: {reason}\n"
        f"⏱ Осталось: ~{remaining}"
    )


def save_report(post_id: int, reporter_id: int) -> bool:
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            conn.execute(
                "INSERT INTO reports (post_id, reporter_id, created_at) VALUES (?, ?, ?)",
                (post_id, reporter_id, now_iso()),
            )
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def admin_moderation_kb(post_id: int, author_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить пост", callback_data=f"adm:del:{post_id}")],
            [
                InlineKeyboardButton(text="⏱ Бан 1 д", callback_data=f"adm:ban:{author_id}:24"),
                InlineKeyboardButton(text="⏱ Бан 7 д", callback_data=f"adm:ban:{author_id}:168"),
            ],
            [InlineKeyboardButton(text="🔒 Бан навсегда", callback_data=f"adm:ban:{author_id}:0")],
            [InlineKeyboardButton(text="✅ Пропустить", callback_data=f"adm:skip:{post_id}")],
        ]
    )


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=BTN_CREATE_POST),
                KeyboardButton(text=BTN_VIEW_POSTS),
                KeyboardButton(text=BTN_PREMIUM),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def back_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_BACK)]],
        resize_keyboard=True,
    )


def chat_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_END_CHAT)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def rating_kb(chat_id: int) -> InlineKeyboardMarkup:
    stars = ["⭐", "⭐⭐", "⭐⭐⭐", "⭐⭐⭐⭐", "⭐⭐⭐⭐⭐"]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{n} {stars[n - 1]}",
                    callback_data=f"rate:{chat_id}:{n}",
                )
                for n in range(1, 6)
            ]
        ]
    )


def post_actions_kb(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💬 Чат", callback_data=f"chat:{post_id}"),
                InlineKeyboardButton(text="🚩 Жалоба", callback_data=f"report:{post_id}"),
            ],
        ]
    )


def next_viewer_offset(user_id: int, total: int) -> int:
    if total <= 0:
        return 0
    offset = VIEWER_OFFSET.get(user_id, 0) % total
    VIEWER_OFFSET[user_id] = (offset + 1) % total
    return offset


def format_rating(post_id: int) -> str:
    avg, count = get_post_rating(post_id)
    if avg is None:
        return "⭐ —"
    return f"⭐ {avg}/5 ({count})"


def format_author_status(user_id: int) -> str:
    activity = get_user_activity(user_id)
    bits = []
    if get_active_chat(user_id):
        bits.append("🔴 в диалоге")
    active_at = activity.get("last_active_at")
    if active_at:
        dt = parse_iso(active_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - dt <= ONLINE_THRESHOLD:
            bits.append("🟢 онлайн")
        else:
            bits.append(f"🕐 {format_time_ago(active_at)}")
    chat_at = activity.get("last_chat_at")
    if chat_at:
        bits.append(f"💬 {format_time_ago(chat_at)}")
    return " · ".join(bits) if bits else ""


def format_post_caption(post: dict, header: str = "") -> str:
    parts = []
    if header:
        parts.append(f"<b>{escape(header)}</b>")
    if has_active_premium(post["user_id"]):
        parts.append("👑 ТОП")
    parts.append(escape(post["text"].strip()))
    parts.append(format_rating(post["id"]))
    status = format_author_status(post["user_id"])
    if status:
        parts.append(status)
    return "\n".join(parts)


async def refresh_main_keyboard(message: Message, text: str) -> None:
    await message.answer(text, reply_markup=ReplyKeyboardRemove())
    await message.answer("\u2063", reply_markup=main_menu_kb())


async def send_post(bot: Bot, chat_id: int, post: dict, header: str = "") -> None:
    await bot.send_message(chat_id, format_post_caption(post, header=header))


def rules_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_ACCEPT_RULES)]],
        resize_keyboard=True,
    )


async def referral_link(bot: Bot, user_id: int) -> str:
    me = await bot.get_me()
    return f"https://t.me/{me.username}?start=ref_{user_id}"


async def send_premium_info(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    refs = count_referrals(user_id)
    link = await referral_link(bot, user_id)
    premium_line = "❌ Премиум не активен"
    until = get_premium_until(user_id)
    if until:
        premium_line = f"✅ <b>Премиум активен</b> ещё ~{format_time_remaining(until)}"
    text = (
        f"{PREMIUM_TEXT}\n\n"
        f"<code>{escape(link)}</code>\n\n"
        f"{premium_line}\n"
        f"👥 Приглашено друзей: <b>{refs}</b>"
    )
    await message.answer(text, reply_markup=main_menu_kb())


async def send_welcome(message: Message) -> None:
    await refresh_main_keyboard(
        message,
        "👋 <b>RandomCircle</b>\n"
        "✏️ Пост · 🔍 Смотреть · 💎 Премиум\n"
        "Создайте пост → смотрите чужие → жмите «Чат» под постом",
    )


storage = MemoryStorage()
dp = Dispatcher(storage=storage)

CHAT_SKIP_TEXT = MENU_BUTTONS | {BTN_END_CHAT, "🚪 Прекратить диалог"}


class ActiveChatFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if not message.from_user:
            return False
        return get_active_chat(message.from_user.id) is not None


async def clear_user_fsm(bot: Bot, user_id: int) -> None:
    key = StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id)
    await storage.set_state(key, None)
    await storage.set_data(key, {})


async def relay_text_to_partner(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    chat = get_active_chat(user_id)
    if not chat or not message.text:
        return
    other = partner_id(chat, user_id)
    try:
        await bot.send_message(other, "💬 Собеседник:\n" + message.text)
    except Exception:
        logger.exception("Failed to relay message to %s", other)
        await message.answer("Не удалось доставить сообщение.")


async def relay_media_to_partner(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    chat = get_active_chat(user_id)
    if not chat:
        logging.info(f"No active chat for user {user_id}, cannot relay media.")
        return
    other = partner_id(chat, user_id)
    logging.info(f"Relaying media from {user_id} to {other}")
    try:
        # Определяем тип медиа для уведомления
        if message.photo: prefix = "🖼 Фото"
        elif message.video: prefix = "📹 Видео"
        elif message.video_note: prefix = "🎥 Кружок"
        elif message.voice: prefix = "🎤 Голос"
        elif message.sticker: prefix = "🧧 Стикер"
        elif message.animation: prefix = "👾 GIF"
        elif message.audio: prefix = "🎵 Аудио"
        elif message.document: prefix = "📄 Файл"
        else: prefix = "📦 Медиа"
        
        # Отправляем уведомление
        await bot.send_message(other, f"💬 Собеседник отправил {prefix}:")
        
        # Пересылаем само сообщение
        # copy_message - лучший способ переслать любой контент
        await bot.copy_message(
            chat_id=other,
            from_chat_id=message.chat.id,
            message_id=message.message_id
        )
        logging.info(f"Successfully relayed media from {user_id} to {other}")
    except Exception as e:
        logging.exception(f"Failed to relay media from {user_id} to {other}: {e}")
        await message.answer("❌ Не удалось доставить файл собеседнику.")


@dp.message(ActiveChatFilter(), ~F.text.in_(CHAT_SKIP_TEXT))
async def relay_chat_all_in_dialog(message: Message, state: FSMContext, bot: Bot) -> None:
    user_id = message.from_user.id
    # Очищаем стейт на всякий случай, если пользователь застрял в создании поста
    current_state = await state.get_state()
    if current_state:
        logging.info(f"Clearing state {current_state} for user {user_id} in active chat")
        await state.clear()
    
    # Если это текст - пересылаем как текст
    if message.text:
        await relay_text_to_partner(message, bot)
    # Если это медиа (фото, видео, кружок и т.д.) - пересылаем как медиа
    else:
        await relay_media_to_partner(message, bot)


@dp.update.outer_middleware()
async def track_activity(handler, event, data):
    user = None
    bot = data.get("bot")
    if hasattr(event, "from_user") and event.from_user:
        user = event.from_user
    elif hasattr(event, "message") and event.message and event.message.from_user:
        user = event.message.from_user

    if user and not is_admin(user.id) and is_user_banned(user.id):
        text = ban_message_text(user.id)
        if isinstance(event, Message) and bot:
            await event.answer(text, parse_mode="HTML")
        elif hasattr(event, "answer"):
            await event.answer("Вы заблокированы", show_alert=True)
        return None

    if user:
        touch_activity(user.id)
    return await handler(event, data)


@dp.message(CommandStart())
async def cmd_start(
    message: Message, state: FSMContext, command: CommandObject, bot: Bot
) -> None:
    await state.clear()
    user_id = message.from_user.id
    ensure_user(user_id)
    if command.args:
        arg = command.args.strip()
        if arg.startswith("ref_"):
            try:
                save_pending_referral(user_id, int(arg[4:]))
            except ValueError:
                pass
    if user_accepted_rules(user_id):
        referrer_id = process_pending_referral(user_id)
        if referrer_id:
            await notify_referrer_premium(bot, referrer_id)
        await send_welcome(message)
        return
    await message.answer(RULES_TEXT, reply_markup=rules_kb())


@dp.message(F.text == BTN_ACCEPT_RULES)
async def accept_rules_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    user_id = message.from_user.id
    # Сначала сбрасываем кэш, чтобы принудительно обновить статус
    if user_id in RULES_ACCEPTED_CACHE:
        del RULES_ACCEPTED_CACHE[user_id]
        
    referrer_id = accept_rules(user_id)
    await state.clear()
    await message.answer("✅ Правила приняты.")
    if referrer_id:
        await notify_referrer_premium(bot, referrer_id)
    await send_welcome(message)


@dp.message(F.text == BTN_PREMIUM)
async def premium_menu(message: Message, bot: Bot) -> None:
    if not await require_rules(message):
        return
    await send_premium_info(message, bot)


async def require_rules(message: Message) -> bool:
    user_id = message.from_user.id
    if user_accepted_rules(user_id):
        return True
    
    # Если правила не приняты, очищаем стейт и шлем правила
    # Это предотвратит "застревание" в стейтах создания поста
    await message.answer(RULES_TEXT, reply_markup=rules_kb())
    return False


@dp.message(F.text.in_({BTN_CREATE_POST, "✏️ Сделать свой пост"}))
async def create_post_start(message: Message, state: FSMContext) -> None:
    if not await require_rules(message):
        return
    if get_active_chat(message.from_user.id):
        await message.answer(
            "⚠️ Сначала завершите текущий диалог.",
            reply_markup=chat_kb(),
        )
        return
    await state.set_state(CreatePost.waiting_content)
    await message.answer(
        "✏️ <b>Создание поста</b>\n\n"
        "📤 Отправьте <b>только текст</b> (можно со смайликами).\n"
        "🚫 Фото, видео, стикеры и файлы — нельзя.\n\n"
        f"📏 До {MAX_POST_LENGTH} символов.\n\n"
        "↩️ Передумали? Нажмите «Назад».",
        reply_markup=back_kb(),
    )


@dp.message(CreatePost.waiting_content, F.text == BTN_BACK)
async def create_post_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await refresh_main_keyboard(message, "↩️ Отменено")


@dp.message(CreatePost.waiting_content, F.text == "🏠 В главное меню")
async def create_post_to_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await refresh_main_keyboard(message, "🏠 Меню")


@dp.message(
    CreatePost.waiting_content,
    F.photo | F.video | F.voice | F.audio | F.document | F.sticker | F.animation,
)
async def create_post_no_media(message: Message) -> None:
    await message.answer(
        "🚫 В пост можно отправить <b>только текст</b>.\n"
        "Фото, видео, стикеры и файлы не принимаются.",
    )


@dp.message(CreatePost.waiting_content, F.text)
async def create_post_text(message: Message, state: FSMContext, bot: Bot) -> None:
    user_id = message.from_user.id
    if get_active_chat(user_id):
        await state.clear()
        await relay_text_to_partner(message, bot)
        return
    text = message.text.strip()
    if text in MENU_BUTTONS:
        await state.clear()
        # Если пользователь нажал кнопку меню во время создания поста - отменяем создание
        if text == BTN_VIEW_POSTS or text == "🔍 Смотреть посты":
            await view_posts(message, state)
        elif text == BTN_PREMIUM:
            await premium_menu(message, bot)
        else:
            await refresh_main_keyboard(message, "🏠 Меню")
        return
    
    if not text:
        await message.answer("⚠️ Текст не может быть пустым.")
        return
    if len(text) > MAX_POST_LENGTH:
        await message.answer(
            f"⚠️ Слишком длинный текст. Максимум {MAX_POST_LENGTH} символов "
            f"(у вас {len(text)})."
        )
        return
    post_id = create_post(message.from_user.id, text)
    await state.clear()
    await message.answer(
        f"✅ <b>Опубликовано</b>\n{escape(text)}",
        reply_markup=main_menu_kb(),
    )


@dp.message(CreatePost.waiting_content)
async def create_post_invalid(message: Message) -> None:
    await message.answer(
        "📝 Отправьте текст поста (только буквы и смайлики).\n"
        "Или нажмите «◀️ Назад», чтобы вернуться в меню.",
    )


@dp.message(F.text.in_({BTN_VIEW_POSTS, "🔍 Смотреть посты"}))
async def view_posts(message: Message, state: FSMContext) -> None:
    if not await require_rules(message):
        return
    await state.clear()
    if get_active_chat(message.from_user.id):
        await message.answer(
            "💬 Вы в диалоге. Завершите его, чтобы смотреть посты.",
            reply_markup=chat_kb(),
        )
        return
    user_id = message.from_user.id
    total = count_posts_for_viewer(user_id)
    if total == 0:
        await message.answer(
            "🔍 <b>Пока пусто</b>\n\n"
            "Нет постов других пользователей.\n"
            "Попросите друзей зайти в бота и создать пост ✏️",
            reply_markup=main_menu_kb(),
        )
        return
    offset = next_viewer_offset(user_id, total)
    posts = list_posts_for_viewer(user_id, offset)
    if not posts:
        await message.answer("📭 Посты закончились.", reply_markup=main_menu_kb())
        return
    post = posts[0]
    await message.answer(
        format_post_caption(post),
        reply_markup=post_actions_kb(post["id"]),
    )


@dp.message(Command("admin_stats"))
async def admin_stats_command(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return

    stats = get_total_stats()
    text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего пользователей: <b>{stats['users']}</b>\n"
        f"📝 Активных постов: <b>{stats['active_posts']}</b>\n"
        f"💬 Активных чатов: <b>{stats['active_chats']}</b>\n"
        f"📂 Всего диалогов за всё время: <b>{stats['total_chats']}</b>"
    )
    await message.answer(text, parse_mode="HTML")


@dp.callback_query(F.data.startswith("report:"))
async def report_post(callback: CallbackQuery, bot: Bot) -> None:
    post_id = int(callback.data.split(":")[1])
    reporter_id = callback.from_user.id
    post = get_post(post_id)
    if not post:
        await callback.answer("Пост уже удалён", show_alert=True)
        return
    if post["user_id"] == reporter_id:
        await callback.answer("Нельзя пожаловаться на свой пост", show_alert=True)
        return
    if not save_report(post_id, reporter_id):
        await callback.answer("Вы уже жаловались на этот пост", show_alert=True)
        return

    reporter = callback.from_user
    reporter_label = f"@{reporter.username}" if reporter.username else f"id {reporter_id}"
    author_id = post["user_id"]
    admin_text = (
        f"🚩 <b>Жалоба на пост №{post_id}</b>\n\n"
        f"👤 Автор: <code>{author_id}</code>\n"
        f"📢 От: {escape(reporter_label)} (<code>{reporter_id}</code>)\n\n"
        f"📝 {escape(post['text'])}"
    )
    if not ADMIN_IDS:
        await callback.answer("Модераторы не настроены", show_alert=True)
        return

    kb = admin_moderation_kb(post_id, author_id)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_text, reply_markup=kb)
        except Exception:
            logger.exception("Failed to notify admin %s", admin_id)

    await callback.answer("Жалоба отправлена модераторам 🚩", show_alert=True)


@dp.callback_query(F.data.startswith("adm:"))
async def admin_action(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    parts = callback.data.split(":")
    action = parts[1]

    if action == "skip":
        await callback.message.edit_text(callback.message.text + "\n\n✅ <b>Пропущено</b>")
        await callback.answer()
        return

    if action == "del":
        post_id = int(parts[2])
        post = get_post(post_id)
        if delete_post(post_id):
            if post:
                try:
                    await bot.send_message(
                        post["user_id"],
                        f"🗑 Ваш пост №{post_id} удалён модератором за нарушение правил.",
                    )
                except Exception:
                    pass
            await callback.message.edit_text(
                callback.message.text + f"\n\n🗑 <b>Пост №{post_id} удалён</b>"
            )
            await callback.answer("Пост удалён")
        else:
            await callback.answer("Пост уже удалён", show_alert=True)
        return

    if action == "ban":
        user_id = int(parts[2])
        hours = int(parts[3])
        if is_admin(user_id):
            await callback.answer("Нельзя забанить админа", show_alert=True)
            return
        label = "навсегда" if hours <= 0 else f"на {hours} ч."
        ban_user(user_id, hours, "жалоба на пост", callback.from_user.id)
        try:
            await bot.send_message(
                user_id,
                ban_message_text(user_id) or "🚫 Вы заблокированы в боте.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await callback.message.edit_text(
            callback.message.text + f"\n\n🔒 <b>Пользователь {user_id} забанен {label}</b>"
        )
        await callback.answer(f"Бан: {label}")
        return

    await callback.answer("Неизвестное действие")


@dp.callback_query(F.data.startswith("chat:"))
async def start_chat_handler(callback: CallbackQuery, bot: Bot) -> None:
    post_id = int(callback.data.split(":")[1])
    initiator_id = callback.from_user.id
    chat_id, error = start_chat(post_id, initiator_id)
    if error:
        await callback.answer(error, show_alert=True)
        return

    chat = get_chat(chat_id)
    post = get_post(chat["post_id"])
    initiator_post = get_post(chat["initiator_post_id"])
    owner_id = chat["owner_id"]

    await clear_user_fsm(bot, initiator_id)
    await clear_user_fsm(bot, owner_id)

    await callback.answer("💬 Чат начат!")
    await callback.message.answer(
        "💬 <b>Диалог начат!</b>\n\n"
        "Пишите сообщения — они дойдут до собеседника.\n"
        "🚪 Выход — «Выйти».",
        reply_markup=chat_kb(),
    )

    await bot.send_message(
        owner_id,
        "🔔 <b>К вам хотят пообщаться!</b>\n\n"
        "Вот пост человека, который начал чат:",
    )
    await send_post(bot, owner_id, initiator_post, header="👤 Собеседник")
    await bot.send_message(
        owner_id,
        "✉️ Ответьте сообщением — диалог открыт.\n"
        "🚪 Выход — «Выйти».",
        reply_markup=chat_kb(),
    )


@dp.message(F.text == "🏠 В главное меню")
async def legacy_main_menu(message: Message, state: FSMContext) -> None:
    if get_active_chat(message.from_user.id):
        await message.answer("💬 Сначала «Выйти».", reply_markup=chat_kb())
        return
    await state.clear()
    await refresh_main_keyboard(message, "🏠 Меню")


@dp.message(F.text.in_({BTN_END_CHAT, "🚪 Прекратить диалог"}))
async def end_chat_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    user_id = message.from_user.id
    chat = get_active_chat(user_id)
    if not chat:
        await message.answer("ℹ️ Вы не в диалоге.", reply_markup=main_menu_kb())
        return

    ended = end_chat(chat["id"])
    if not ended:
        await message.answer("ℹ️ Диалог уже завершён.", reply_markup=main_menu_kb())
        return

    other = partner_id(ended, user_id)
    await message.answer(
        "👋 <b>Диалог завершён</b>\n\n⭐ Оцените собеседника:",
        reply_markup=main_menu_kb(),
    )
    await message.answer("Ваша оценка 👇", reply_markup=rating_kb(ended["id"]))

    await bot.send_message(
        other,
        "👋 <b>Собеседник завершил диалог</b>\n\n⭐ Оцените его:",
        reply_markup=main_menu_kb(),
    )
    await bot.send_message(other, "Ваша оценка 👇", reply_markup=rating_kb(ended["id"]))


@dp.callback_query(F.data.startswith("rate:"))
async def rate_partner(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    chat_id = int(parts[1])
    rating = int(parts[2])
    if rating < 1 or rating > 5:
        await callback.answer("Некорректная оценка")
        return

    chat = get_chat(chat_id)
    if not chat:
        await callback.answer("Диалог не найден")
        return

    user_id = callback.from_user.id
    if user_id not in (chat["initiator_id"], chat["owner_id"]):
        await callback.answer("Нет доступа")
        return

    target_post_id = rated_post_for_user(chat, user_id)
    save_post_rating(target_post_id, user_id, rating, chat_id)
    stars = "⭐" * rating
    await callback.message.edit_text(f"🙏 <b>Спасибо!</b>\n\nВаша оценка: {rating}/5 {stars}")
    await callback.answer("Оценка сохранена ✨")


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан. Положите токен в bot_token.txt")

    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запуск и приветствие"),
            BotCommand(command="menu", description="Главное меню"),
            BotCommand(command="profile", description="Профиль и язык"),
            BotCommand(command="random", description="Случайный кружок"),
            BotCommand(command="my", description="Мой кружок"),
            BotCommand(command="stats", description="Лимит и статистика"),
            BotCommand(command="invite", description="Реферальная ссылка"),
            BotCommand(command="admin_stats", description="Статистика бота (админ)"),
        ]
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
