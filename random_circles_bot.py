import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from html import escape
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import BaseFilter, CommandObject, CommandStart, StateFilter
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

DB_PATH = os.getenv("DB_PATH", "circles.db")
BOT_TOKEN_FILE = os.getenv("BOT_TOKEN_FILE", "bot_token.txt")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "7704968798")
ADMIN_IDS = {int(part.strip()) for part in ADMIN_IDS_RAW.split(",") if part.strip().isdigit()}

BTN_CREATE_POST = "✏️ Сделать свой пост"
BTN_VIEW_POSTS = "🔍 Смотреть посты"
BTN_END_CHAT = "🚪 Прекратить диалог"
BTN_MAIN_MENU = "🏠 В главное меню"
BTN_BACK = "◀️ Назад"
BTN_ACCEPT_RULES = "✅ Мне есть 18+ — принимаю правила"
BTN_PREMIUM = "💎 Премиум"

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
    BTN_MAIN_MENU,
    BTN_BACK,
})

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
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT rules_accepted FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return bool(row and row[0])


def accept_rules(user_id: int) -> int | None:
    ensure_user(user_id)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE users SET rules_accepted = 1, last_active_at = ? WHERE user_id = ?",
            (now_iso(), user_id),
        )
        conn.commit()
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


def format_author_status(user_id: int) -> str:
    activity = get_user_activity(user_id)
    lines = []

    if get_active_chat(user_id):
        lines.append("🔴 <b>Сейчас в диалоге</b>")

    active_at = activity.get("last_active_at")
    if active_at:
        dt = parse_iso(active_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - dt <= ONLINE_THRESHOLD:
            lines.append("🟢 <b>В боте:</b> сейчас онлайн")
        else:
            lines.append(f"🕐 <b>В боте:</b> {format_time_ago(active_at)}")
    else:
        lines.append("🕐 <b>В боте:</b> давно не заходил")

    chat_at = activity.get("last_chat_at")
    if chat_at:
        lines.append(f"💬 <b>Последний чат:</b> {format_time_ago(chat_at)}")
    else:
        lines.append("💬 <b>Последний чат:</b> ещё не было")

    lines.append("<i>ℹ️ По активности в боте (не статус Telegram)</i>")
    return "\n".join(lines)


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
        return None, "Вы уже в диалоге. Завершите его кнопкой «Прекратить диалог»."
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
            [KeyboardButton(text=BTN_CREATE_POST)],
            [KeyboardButton(text=BTN_VIEW_POSTS)],
            [KeyboardButton(text=BTN_PREMIUM)],
        ],
        resize_keyboard=True,
    )


def back_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_BACK)]],
        resize_keyboard=True,
    )


def chat_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_END_CHAT)],
            [KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
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


def post_nav_kb(post_id: int, offset: int, total: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="💬 Начать чат", callback_data=f"chat:{post_id}")],
        [InlineKeyboardButton(text="🚩 Пожаловаться", callback_data=f"report:{post_id}")],
    ]
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"posts:{offset - 1}"))
    if offset + 1 < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"posts:{offset + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_rating(post_id: int) -> str:
    avg, count = get_post_rating(post_id)
    if avg is None:
        return "⭐ Оценка: пока нет отзывов"
    word = "отзыв" if count % 10 == 1 and count % 100 != 11 else "отзывов"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        word = "отзыва"
    return f"⭐ Оценка: {avg}/5 · {count} {word}"


def format_post_caption(post: dict, header: str = "") -> str:
    title = header or f"📄 Пост №{post['id']}"
    lines = [f"<b>{escape(title)}</b>"]
    if has_active_premium(post["user_id"]):
        lines.append("👑 <b>ТОП · Премиум</b>")
    lines.append(f"📝 {escape(post['text'])}")
    lines.append(format_rating(post["id"]))
    lines.append(format_author_status(post["user_id"]))
    return "\n\n".join(lines)


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
    await message.answer(
        "👋 <b>Добро пожаловать!</b>\n\n"
        "✏️ <b>Сделать свой пост</b> — только текст и смайлики\n"
        "🔍 <b>Смотреть посты</b> — листайте анкеты и начинайте чат\n"
        "💎 <b>Премиум</b> — пост в топе за приглашённых друзей\n\n"
        "💡 Сначала создайте пост, потом смотрите чужие и нажимайте «Начать чат»",
        reply_markup=main_menu_kb(),
    )


storage = MemoryStorage()
dp = Dispatcher(storage=storage)

CHAT_SKIP_TEXT = MENU_BUTTONS | {BTN_END_CHAT}


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
        return
    other = partner_id(chat, user_id)
    caption = "💬 Собеседник"
    if message.caption:
        caption += f":\n{message.caption}"
    try:
        if message.photo:
            await bot.send_photo(other, message.photo[-1].file_id, caption=caption)
        elif message.video:
            await bot.send_video(other, message.video.file_id, caption=caption)
        elif message.video_note:
            await bot.send_message(other, "🎥 Собеседник отправил кружок:")
            await bot.send_video_note(other, message.video_note.file_id)
        elif message.voice:
            await bot.send_voice(other, message.voice.file_id, caption=caption)
        elif message.audio:
            await bot.send_audio(other, message.audio.file_id, caption=caption)
        elif message.document:
            await bot.send_document(other, message.document.file_id, caption=caption)
        elif message.sticker:
            await bot.send_message(other, "💬 Собеседник:")
            await bot.send_sticker(other, message.sticker.file_id)
    except Exception:
        logger.exception("Failed to relay media to %s", other)
        await message.answer("Не удалось доставить файл.")


@dp.message(ActiveChatFilter(), F.text.not_in(CHAT_SKIP_TEXT))
async def relay_chat_text_in_dialog(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await relay_text_to_partner(message, bot)


@dp.message(
    ActiveChatFilter(),
    F.photo | F.video | F.video_note | F.voice | F.audio | F.document | F.sticker,
)
async def relay_chat_media_in_dialog(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
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
    referrer_id = accept_rules(user_id)
    await state.clear()
    await message.answer("✅ Спасибо! Правила приняты.", reply_markup=main_menu_kb())
    if referrer_id:
        await notify_referrer_premium(bot, referrer_id)
    await send_welcome(message)


@dp.message(F.text == BTN_PREMIUM)
async def premium_menu(message: Message, bot: Bot) -> None:
    if not await require_rules(message):
        return
    await send_premium_info(message, bot)


async def require_rules(message: Message) -> bool:
    if user_accepted_rules(message.from_user.id):
        return True
    await message.answer(RULES_TEXT, reply_markup=rules_kb())
    return False


@dp.message(F.text == BTN_CREATE_POST)
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
    await message.answer(
        "↩️ Создание поста отменено.",
        reply_markup=main_menu_kb(),
    )


@dp.message(CreatePost.waiting_content, F.text == BTN_MAIN_MENU)
async def create_post_to_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🏠 Главное меню", reply_markup=main_menu_kb())


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
    if get_active_chat(message.from_user.id):
        await state.clear()
        await relay_text_to_partner(message, bot)
        return
    text = message.text.strip()
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
        f"✅ <b>Пост опубликован!</b> №{post_id}\n\n📝 {escape(text)}",
        reply_markup=main_menu_kb(),
    )


@dp.message(CreatePost.waiting_content)
async def create_post_invalid(message: Message) -> None:
    await message.answer(
        "📝 Отправьте текст поста (только буквы и смайлики).\n"
        "Или нажмите «◀️ Назад», чтобы вернуться в меню.",
    )


@dp.message(F.text == BTN_VIEW_POSTS)
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
    total = count_posts_for_viewer(message.from_user.id)
    if total == 0:
        await message.answer(
            "🔍 <b>Пока пусто</b>\n\n"
            "Нет постов других пользователей.\n"
            "Попросите друзей зайти в бота и создать пост ✏️",
            reply_markup=main_menu_kb(),
        )
        return
    await message.answer("🔍 Листайте посты кнопками ⬅️ ➡️", reply_markup=main_menu_kb())
    await show_post_at_offset(message, 0)


async def show_post_at_offset(message: Message, offset: int) -> None:
    user_id = message.from_user.id
    total = count_posts_for_viewer(user_id)
    if offset < 0 or offset >= total:
        await message.answer("📭 Больше постов нет.", reply_markup=main_menu_kb())
        return
    posts = list_posts_for_viewer(user_id, offset)
    if not posts:
        await message.answer("📭 Посты закончились.", reply_markup=main_menu_kb())
        return
    post = posts[0]
    caption = format_post_caption(post)
    kb = post_nav_kb(post["id"], offset, total)
    await message.answer(caption, reply_markup=kb)


@dp.callback_query(F.data == "menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer("🏠 Главное меню", reply_markup=main_menu_kb())
    await callback.answer()


@dp.callback_query(F.data.startswith("posts:"))
async def posts_nav(callback: CallbackQuery) -> None:
    offset = int(callback.data.split(":")[1])
    total = count_posts_for_viewer(callback.from_user.id)
    if offset < 0 or offset >= total:
        await callback.answer("Нет такого поста")
        return
    posts = list_posts_for_viewer(callback.from_user.id, offset)
    if not posts:
        await callback.answer("Пост не найден")
        return
    post = posts[0]
    caption = format_post_caption(post)
    kb = post_nav_kb(post["id"], offset, total)
    await callback.message.delete()
    await callback.message.answer(caption, reply_markup=kb)
    await callback.answer()


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
        "🚪 Чтобы выйти — «Прекратить диалог».",
        reply_markup=chat_kb(),
    )

    await bot.send_message(
        owner_id,
        "🔔 <b>К вам хотят пообщаться!</b>\n\n"
        "Вот пост человека, который начал чат:",
    )
    await send_post(bot, owner_id, initiator_post, header="👤 Пост собеседника")
    await bot.send_message(
        owner_id,
        "✉️ Ответьте сообщением — диалог открыт.\n"
        "🚪 Для выхода: «Прекратить диалог».",
        reply_markup=chat_kb(),
    )


@dp.message(F.text == BTN_MAIN_MENU)
async def main_menu_from_anywhere(message: Message, state: FSMContext) -> None:
    if get_active_chat(message.from_user.id):
        await message.answer(
            "💬 Вы в диалоге.\n"
            "Чтобы выйти в меню — сначала «🚪 Прекратить диалог».",
            reply_markup=chat_kb(),
        )
        return
    await state.clear()
    await message.answer("🏠 Главное меню", reply_markup=main_menu_kb())


@dp.message(F.text == BTN_END_CHAT)
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
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
