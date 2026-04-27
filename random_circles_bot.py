import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message


DB_PATH = os.getenv("DB_PATH", "circles.db")
BOT_TOKEN_FILE = os.getenv("BOT_TOKEN_FILE", "bot_token.txt")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))


def read_secret(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as file:
        return file.read().strip()


BOT_TOKEN = os.getenv("BOT_TOKEN", read_secret(BOT_TOKEN_FILE))
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {int(part.strip()) for part in ADMIN_IDS_RAW.split(",") if part.strip().isdigit()}

# Храним состояние "пользователь пишет комментарий к submission_id".
PENDING_COMMENTS: dict[int, int] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                bonus_views INTEGER NOT NULL DEFAULT 0,
                language TEXT NOT NULL DEFAULT 'ru',
                created_at TEXT NOT NULL
            )
            """
        )
        user_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        if "language" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN language TEXT NOT NULL DEFAULT 'ru'")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                file_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                is_blocked INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS views (
                user_id INTEGER NOT NULL,
                submission_id INTEGER NOT NULL,
                viewed_at TEXT NOT NULL,
                PRIMARY KEY (user_id, submission_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER NOT NULL,
                submission_id INTEGER NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (reporter_id, submission_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reactions (
                user_id INTEGER NOT NULL,
                submission_id INTEGER NOT NULL,
                reaction_type TEXT NOT NULL CHECK(reaction_type IN ('like', 'dislike')),
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, submission_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                submission_id INTEGER NOT NULL,
                comment_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                referred_user_id INTEGER PRIMARY KEY,
                inviter_user_id INTEGER NOT NULL,
                bonus_granted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_bans (
                user_id INTEGER PRIMARY KEY,
                banned_until TEXT NOT NULL,
                reason TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def ensure_user(user_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO users (user_id, bonus_views, language, created_at)
            VALUES (?, 0, 'ru', ?)
            """,
            (user_id, now_iso()),
        )
        conn.commit()


def get_user_language(user_id: int) -> str:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT language FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        lang = (row[0] if row else "ru") or "ru"
        return "en" if lang == "en" else "ru"


def set_user_language(user_id: int, language: str) -> None:
    language = "en" if language == "en" else "ru"
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE users SET language = ? WHERE user_id = ?",
            (language, user_id),
        )
        conn.commit()


def set_referral(referred_user_id: int, inviter_user_id: int) -> None:
    if referred_user_id == inviter_user_id:
        return
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO referrals (
                referred_user_id,
                inviter_user_id,
                bonus_granted,
                created_at
            )
            VALUES (?, ?, 0, ?)
            """,
            (referred_user_id, inviter_user_id, now_iso()),
        )
        conn.commit()


def grant_referral_bonus_if_needed(referred_user_id: int) -> Optional[int]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            """
            SELECT inviter_user_id, bonus_granted
            FROM referrals
            WHERE referred_user_id = ?
            """,
            (referred_user_id,),
        ).fetchone()
        if not row:
            return None
        inviter_user_id, bonus_granted = int(row[0]), int(row[1])
        if bonus_granted:
            return None

        conn.execute(
            "UPDATE users SET bonus_views = bonus_views + 50 WHERE user_id = ?",
            (inviter_user_id,),
        )
        conn.execute(
            "UPDATE referrals SET bonus_granted = 1 WHERE referred_user_id = ?",
            (referred_user_id,),
        )
        conn.commit()
        return inviter_user_id


def add_submission(user_id: int, file_id: str) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        try:
            conn.execute(
                """
                INSERT INTO submissions (user_id, file_id, created_at)
                VALUES (?, ?, ?)
                """,
                (user_id, file_id, now_iso()),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def get_user_active_submission(user_id: int) -> Optional[sqlite3.Row]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, user_id, file_id, created_at
            FROM submissions
            WHERE user_id = ?
              AND is_active = 1
              AND is_blocked = 0
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        return row


def delete_user_active_submission(user_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            """
            UPDATE submissions
            SET is_active = 0
            WHERE user_id = ?
              AND is_active = 1
              AND is_blocked = 0
            """,
            (user_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def user_has_submissions(user_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM submissions
            WHERE user_id = ?
              AND is_active = 1
              AND is_blocked = 0
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        return row is not None


def user_total_views(user_id: int) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM views WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return int(row[0]) if row else 0


def user_view_limit(user_id: int) -> int:
    base_limit = 100 if user_has_submissions(user_id) else 10
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT bonus_views FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        bonus = int(row[0]) if row else 0
    return base_limit + bonus


def get_random_submission_for_user(user_id: int) -> Optional[sqlite3.Row]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT s.id, s.user_id, s.file_id, s.created_at
            FROM submissions s
            WHERE s.is_active = 1
              AND s.is_blocked = 0
              AND s.user_id != ?
              AND NOT EXISTS (
                    SELECT 1
                    FROM views v
                    WHERE v.user_id = ?
                      AND v.submission_id = s.id
              )
            ORDER BY RANDOM()
            LIMIT 1
            """,
            (user_id, user_id),
        ).fetchone()
        return row


def get_submission(submission_id: int) -> Optional[sqlite3.Row]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, user_id, file_id, created_at
            FROM submissions
            WHERE id = ?
            """,
            (submission_id,),
        ).fetchone()
        return row


def delete_submission(submission_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            """
            UPDATE submissions
            SET is_active = 0, is_blocked = 1
            WHERE id = ?
            """,
            (submission_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def set_user_ban(user_id: int, hours: int, reason: str) -> None:
    banned_until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT INTO user_bans (user_id, banned_until, reason, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                banned_until = excluded.banned_until,
                reason = excluded.reason,
                updated_at = excluded.updated_at
            """,
            (user_id, banned_until, reason, now_iso()),
        )
        conn.commit()


def get_ban_text_if_active(user_id: int) -> Optional[str]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT banned_until, reason FROM user_bans WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return None

        banned_until = datetime.fromisoformat(row[0])
        now = datetime.now(timezone.utc)
        if banned_until <= now:
            conn.execute("DELETE FROM user_bans WHERE user_id = ?", (user_id,))
            conn.commit()
            return None

        remaining = banned_until - now
        hours = int(remaining.total_seconds() // 3600)
        reason = row[1] or "moderation"
        return f"Ты временно ограничен. Осталось ~{hours}ч. Причина: {reason}"


def mark_view(user_id: int, submission_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO views (user_id, submission_id, viewed_at)
            VALUES (?, ?, ?)
            """,
            (user_id, submission_id, now_iso()),
        )
        conn.commit()


def add_reaction(user_id: int, submission_id: int, reaction_type: str) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        if reaction_type not in {"like", "dislike"}:
            return False
        conn.execute(
            """
            INSERT INTO reactions (user_id, submission_id, reaction_type, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, submission_id) DO UPDATE SET
                reaction_type = excluded.reaction_type,
                created_at = excluded.created_at
            """,
            (user_id, submission_id, reaction_type, now_iso()),
        )
        conn.commit()
        return True


def reaction_counts(submission_id: int) -> tuple[int, int]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        likes_row = conn.execute(
            "SELECT COUNT(*) FROM reactions WHERE submission_id = ? AND reaction_type = 'like'",
            (submission_id,),
        ).fetchone()
        dislikes_row = conn.execute(
            "SELECT COUNT(*) FROM reactions WHERE submission_id = ? AND reaction_type = 'dislike'",
            (submission_id,),
        ).fetchone()
        likes = int(likes_row[0]) if likes_row else 0
        dislikes = int(dislikes_row[0]) if dislikes_row else 0
        return likes, dislikes


def add_comment(user_id: int, submission_id: int, comment_text: str) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT INTO comments (user_id, submission_id, comment_text, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, submission_id, comment_text.strip(), now_iso()),
        )
        conn.commit()


def get_comments_for_submission(submission_id: int, limit: int = 20) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, user_id, comment_text, created_at
            FROM comments
            WHERE submission_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (submission_id, limit),
        ).fetchall()
        return list(rows)


def add_report(reporter_id: int, submission_id: int, reason: str = "manual") -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        try:
            conn.execute(
                """
                INSERT INTO reports (reporter_id, submission_id, reason, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (reporter_id, submission_id, reason, now_iso()),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def total_reports_for_submission(submission_id: int) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM reports WHERE submission_id = ?",
            (submission_id,),
        ).fetchone()
        return int(row[0]) if row else 0


def block_submission(submission_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "UPDATE submissions SET is_blocked = 1 WHERE id = ?",
            (submission_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def unviewed_count_for_user(user_id: int) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM submissions s
            WHERE s.is_active = 1
              AND s.is_blocked = 0
              AND s.user_id != ?
              AND NOT EXISTS (
                    SELECT 1
                    FROM views v
                    WHERE v.user_id = ?
                      AND v.submission_id = s.id
              )
            """,
            (user_id, user_id),
        ).fetchone()
        return int(row[0]) if row else 0


def next_keyboard(submission_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏭ Следующий", callback_data="next"),
                InlineKeyboardButton(text="👍", callback_data=f"like:{submission_id}"),
                InlineKeyboardButton(text="👎", callback_data=f"dislike:{submission_id}"),
            ],
            [
                InlineKeyboardButton(text="💬 Комментарии", callback_data=f"comments:{submission_id}"),
                InlineKeyboardButton(text="🚨 Пожаловаться", callback_data=f"report:{submission_id}"),
            ],
        ]
    )


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎬 Смотреть кружки",
                    callback_data="next",
                )
            ],
            [
                InlineKeyboardButton(
                    text="➕ Добавить свой кружок",
                    callback_data="add_own",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎁 Пригласить друга (+50)",
                    callback_data="invite",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📦 Мой кружок",
                    callback_data="my_circle",
                )
            ],
            [
                InlineKeyboardButton(
                    text="👤 Профиль",
                    callback_data="profile",
                )
            ],
        ]
    )


def my_circle_keyboard(submission_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💬 Мои комментарии", callback_data=f"my_comments:{submission_id}"),
            ],
            [
                InlineKeyboardButton(text="🗑 Удалить мой кружок", callback_data="delete_my_circle"),
            ],
        ]
    )


def comments_keyboard(submission_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✍️ Написать комментарий",
                    callback_data=f"comment:{submission_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад к кружкам",
                    callback_data="next",
                )
            ],
        ]
    )


def report_admin_keyboard(submission_id: int, owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💬 Комментарии", callback_data=f"mod_comments:{submission_id}"),
                InlineKeyboardButton(text="🚫 Блок кружок", callback_data=f"mod_block:{submission_id}"),
            ],
            [
                InlineKeyboardButton(text="🗑 Удалить кружок", callback_data=f"mod_delete:{submission_id}"),
            ],
            [
                InlineKeyboardButton(text="⛔ Бан 1д", callback_data=f"mod_ban:{owner_id}:1d"),
                InlineKeyboardButton(text="⛔ Бан 7д", callback_data=f"mod_ban:{owner_id}:7d"),
                InlineKeyboardButton(text="⛔ Бан 1г", callback_data=f"mod_ban:{owner_id}:1y"),
            ],
        ]
    )


def quick_nav_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🏠 Меню", callback_data="menu"),
                InlineKeyboardButton(text="🎬 Смотреть", callback_data="next"),
            ],
            [
                InlineKeyboardButton(text="📦 Мой кружок", callback_data="my_circle"),
            ],
        ]
    )


def profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🌐 Язык / Language", callback_data="lang_menu"),
            ],
            [
                InlineKeyboardButton(text="🏠 Меню", callback_data="menu"),
            ],
        ]
    )


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data="set_lang:ru"),
                InlineKeyboardButton(text="🇬🇧 English", callback_data="set_lang:en"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад в профиль", callback_data="profile"),
            ],
        ]
    )


def referral_link(bot_username: Optional[str], user_id: int) -> str:
    if bot_username:
        return f"https://t.me/{bot_username}?start=ref_{user_id}"
    return f"/start ref_{user_id}"


def format_limits_text(user_id: int) -> str:
    limit = user_view_limit(user_id)
    used = user_total_views(user_id)
    remaining = max(0, limit - used)
    return (
        f"Лимит просмотров: {limit}\n"
        f"Использовано: {used}\n"
        f"Осталось: {remaining}"
    )


async def send_admin_report(bot: Bot, submission_id: int, reporter_id: int, reports_count: int) -> None:
    if ADMIN_CHAT_ID == 0:
        return
    submission = get_submission(submission_id)
    if not submission:
        return

    await bot.send_video_note(chat_id=ADMIN_CHAT_ID, video_note=submission["file_id"])
    await bot.send_message(
        ADMIN_CHAT_ID,
        (
            "Новый репорт\n"
            f"submission_id: {submission_id}\n"
            f"owner_id: {submission['user_id']}\n"
            f"reporter_id: {reporter_id}\n"
            f"reports_total: {reports_count}"
        ),
        reply_markup=report_admin_keyboard(submission_id, submission["user_id"]),
    )


async def show_random_circle(target: Message | CallbackQuery, user_id: int) -> None:
    ban_text = get_ban_text_if_active(user_id)
    if ban_text:
        if isinstance(target, CallbackQuery):
            await target.message.answer(ban_text, reply_markup=quick_nav_keyboard())
        else:
            await target.answer(ban_text, reply_markup=quick_nav_keyboard())
        return

    ensure_user(user_id)
    limit = user_view_limit(user_id)
    used = user_total_views(user_id)
    remaining = limit - used

    if remaining <= 0:
        text = (
            "Ты исчерпал лимит просмотров.\n"
            "Без своего кружка доступно 10 просмотров.\n"
            "После отправки своего кружка станет 100.\n"
            "За каждого приглашенного друга +50.\n\n"
            f"{format_limits_text(user_id)}"
        )
        if isinstance(target, CallbackQuery):
            await target.message.answer(text, reply_markup=main_keyboard())
        else:
            await target.answer(text, reply_markup=main_keyboard())
        return

    submission = get_random_submission_for_user(user_id)
    if not submission:
        text = (
            "Пока нет новых кружков для тебя.\n"
            "Отправь свой кружок, и попробуй снова позже."
        )
        if isinstance(target, CallbackQuery):
            await target.message.answer(text, reply_markup=main_keyboard())
        else:
            await target.answer(text, reply_markup=main_keyboard())
        return

    mark_view(user_id, submission["id"])
    likes, dislikes = reaction_counts(submission["id"])
    caption = (
        "Случайный кружок от другого пользователя\n"
        f"ID: {submission['id']}\n"
        f"Лайки: {likes} | Дизлайки: {dislikes}\n"
        f"Просмотров осталось: {max(0, remaining - 1)}\n\n"
        "Под кружком можно оставить 💬 комментарий или отправить 🚨 жалобу."
    )
    if isinstance(target, CallbackQuery):
        await target.message.answer_video_note(submission["file_id"])
        await target.message.answer(caption, reply_markup=next_keyboard(submission["id"]))
    else:
        await target.answer_video_note(submission["file_id"])
        await target.answer(caption, reply_markup=next_keyboard(submission["id"]))


dp = Dispatcher()


@dp.message(CommandStart(deep_link=True))
async def start_with_ref(message: Message, command: CommandObject) -> None:
    ensure_user(message.from_user.id)
    args = command.args or ""
    if args.startswith("ref_") and args[4:].isdigit():
        inviter_id = int(args[4:])
        set_referral(message.from_user.id, inviter_id)
    await start_handler(message)


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    ensure_user(message.from_user.id)
    bot_info = await message.bot.get_me()
    invite = referral_link(bot_info.username, message.from_user.id)
    await message.answer(
        "✨ Добро пожаловать в Random Circle!\n\n"
        "📌 Как это работает:\n"
        "• Без своего кружка: 10 просмотров\n"
        "• После своего кружка: 100 просмотров\n"
        "• За каждого приглашенного: +50 просмотров\n\n"
        "⚙️ Команды:\n"
        "/random — случайный кружок\n"
        "/stats — твой лимит и статистика\n"
        "/invite — твоя реферальная ссылка\n\n"
        f"🔗 Твоя ссылка: {invite}",
        reply_markup=main_keyboard(),
    )


@dp.message(Command("menu"))
async def menu_command(message: Message) -> None:
    await message.answer("Главное меню:", reply_markup=main_keyboard())


@dp.message(Command("profile"))
async def profile_command(message: Message) -> None:
    await profile_message(message, message.from_user.id)


@dp.message(Command("my"))
async def my_command(message: Message) -> None:
    await my_circle_message(message, message.from_user.id)


@dp.message(Command("invite"))
async def invite_command(message: Message) -> None:
    bot_info = await message.bot.get_me()
    invite = referral_link(bot_info.username, message.from_user.id)
    await message.answer(
        "Приглашай друзей по ссылке.\n"
        "Когда приглашенный отправит свой первый кружок, ты получишь +50 просмотров.\n"
        f"{invite}"
    )


@dp.callback_query(F.data == "invite")
async def invite_callback(callback: CallbackQuery) -> None:
    bot_info = await callback.bot.get_me()
    invite = referral_link(bot_info.username, callback.from_user.id)
    await callback.answer()
    await callback.message.answer(
        "🎁 Приглашай друзей этой ссылкой:\n"
        f"{invite}\n\n"
        "После первого кружка друга тебе начислится +50 просмотров."
    )


@dp.callback_query(F.data == "add_own")
async def add_own_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    current = get_user_active_submission(callback.from_user.id)
    if current:
        await callback.message.answer(
            "У тебя уже есть активный кружок.\n"
            "Открой раздел «📦 Мой кружок», удали его и отправь новый.",
            reply_markup=quick_nav_keyboard(),
        )
        return
    await callback.message.answer("➕ Отправь сюда кружок (video note), и я добавлю его в ленту.")


@dp.callback_query(F.data == "my_circle")
async def my_circle_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    await my_circle_message(callback.message, callback.from_user.id)


async def my_circle_message(message: Message, user_id: int) -> None:
    submission = get_user_active_submission(user_id)
    if not submission:
        await message.answer(
            "У тебя пока нет активного кружка.\n"
            "Нажми «➕ Добавить свой кружок».",
            reply_markup=quick_nav_keyboard(),
        )
        return
    likes, dislikes = reaction_counts(submission["id"])
    await message.answer_video_note(submission["file_id"])
    await message.answer(
        (
            f"Твой кружок (ID: {submission['id']})\n"
            f"Реакции: 👍 {likes} | 👎 {dislikes}\n"
            "Открой комментарии или удали кружок, чтобы загрузить новый."
        ),
        reply_markup=my_circle_keyboard(submission["id"]),
    )


@dp.callback_query(F.data.startswith("my_comments:"))
async def my_comments_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    _, raw_id = callback.data.split(":", maxsplit=1)
    submission_id = int(raw_id)
    submission = get_submission(submission_id)
    if not submission or submission["user_id"] != callback.from_user.id:
        await callback.message.answer("Это не твой кружок.", reply_markup=quick_nav_keyboard())
        return

    comments = get_comments_for_submission(submission_id, limit=15)
    if not comments:
        await callback.message.answer("Комментариев пока нет.", reply_markup=quick_nav_keyboard())
        return

    lines = ["Последние комментарии к твоему кружку:"]
    for idx, row in enumerate(comments, start=1):
        text = (row["comment_text"] or "").replace("\n", " ").strip()
        if len(text) > 140:
            text = text[:140] + "..."
        lines.append(f"{idx}) от {row['user_id']}: {text}")
    await callback.message.answer("\n".join(lines), reply_markup=quick_nav_keyboard())


@dp.callback_query(F.data == "delete_my_circle")
async def delete_my_circle_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    deleted = delete_user_active_submission(callback.from_user.id)
    if deleted:
        await callback.message.answer(
            "Твой текущий кружок удален из ленты.\n"
            "Теперь можешь отправить новый.",
            reply_markup=quick_nav_keyboard(),
        )
    else:
        await callback.message.answer("У тебя нет активного кружка.", reply_markup=quick_nav_keyboard())


@dp.message(Command("random"))
async def random_command(message: Message) -> None:
    await show_random_circle(message, message.from_user.id)


@dp.message(Command("stats"))
async def stats_command(message: Message) -> None:
    ensure_user(message.from_user.id)
    count = unviewed_count_for_user(message.from_user.id)
    await message.answer(
        (
            f"Новых кружков в ленте: {count}\n{format_limits_text(message.from_user.id)}\n\n"
            "Команды: /menu /random /my /stats /invite"
        ),
        reply_markup=quick_nav_keyboard(),
    )


@dp.message(F.video_note)
async def video_note_handler(message: Message) -> None:
    user_id = message.from_user.id
    ban_text = get_ban_text_if_active(user_id)
    if ban_text:
        await message.answer(ban_text, reply_markup=quick_nav_keyboard())
        return

    ensure_user(user_id)
    existing = get_user_active_submission(user_id)
    if existing:
        await message.answer(
            "У тебя уже есть активный кружок.\n"
            "Сначала удали его в разделе «📦 Мой кружок», потом отправь новый.",
            reply_markup=quick_nav_keyboard(),
        )
        return

    file_id = message.video_note.file_id
    created = add_submission(user_id, file_id)
    if created:
        inviter = grant_referral_bonus_if_needed(user_id)
        if inviter and ADMIN_CHAT_ID:
            await message.bot.send_message(
                ADMIN_CHAT_ID,
                f"Реферальный бонус выдан: inviter_id={inviter}, invited_id={user_id}, +50",
            )
        await message.answer(
            "Кружок добавлен. Спасибо!\n"
            "Теперь твой базовый лимит просмотров: 100.",
            reply_markup=main_keyboard(),
        )
    else:
        await message.answer("Этот кружок уже есть в базе.")


@dp.message(F.text)
async def comment_input_handler(message: Message) -> None:
    ban_text = get_ban_text_if_active(message.from_user.id)
    if ban_text:
        await message.answer(ban_text, reply_markup=quick_nav_keyboard())
        return

    submission_id = PENDING_COMMENTS.pop(message.from_user.id, None)
    if not submission_id:
        return
    comment_text = (message.text or "").strip()
    if not comment_text:
        await message.answer("Пустой комментарий не сохранен.")
        return
    if len(comment_text) > 500:
        await message.answer("Комментарий слишком длинный (максимум 500 символов).")
        return
    add_comment(message.from_user.id, submission_id, comment_text)
    await message.answer("Комментарий сохранен.")
    submission = get_submission(submission_id)
    if submission and submission["user_id"] != message.from_user.id:
        try:
            await message.bot.send_message(
                submission["user_id"],
                (
                    "💬 Новый комментарий к твоему кружку:\n"
                    f"{comment_text}\n\n"
                    "Открой «📦 Мой кружок», чтобы посмотреть все комментарии."
                ),
                reply_markup=quick_nav_keyboard(),
            )
        except Exception:
            # Владелец мог заблокировать бота.
            pass


@dp.callback_query(F.data == "next")
async def next_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    await show_random_circle(callback, callback.from_user.id)


@dp.callback_query(F.data == "menu")
async def menu_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer("Главное меню:", reply_markup=main_keyboard())


@dp.callback_query(F.data == "profile")
async def profile_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    await profile_message(callback.message, callback.from_user.id)


@dp.callback_query(F.data == "lang_menu")
async def lang_menu_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(
        "Выбери язык интерфейса / Choose interface language:",
        reply_markup=language_keyboard(),
    )


@dp.callback_query(F.data.startswith("set_lang:"))
async def set_lang_callback(callback: CallbackQuery) -> None:
    ensure_user(callback.from_user.id)
    _, lang = callback.data.split(":", maxsplit=1)
    set_user_language(callback.from_user.id, lang)
    await callback.answer("Language updated" if lang == "en" else "Язык обновлен")
    await profile_message(callback.message, callback.from_user.id)


async def profile_message(message: Message, user_id: int) -> None:
    ensure_user(user_id)
    lang = get_user_language(user_id)
    limit = user_view_limit(user_id)
    used = user_total_views(user_id)
    remaining = max(0, limit - used)
    if lang == "en":
        text = (
            "👤 Profile\n"
            f"Language: English\n"
            f"Views limit: {limit}\n"
            f"Used: {used}\n"
            f"Remaining: {remaining}"
        )
    else:
        text = (
            "👤 Профиль\n"
            f"Язык: Русский\n"
            f"Лимит просмотров: {limit}\n"
            f"Использовано: {used}\n"
            f"Осталось: {remaining}"
        )
    await message.answer(text, reply_markup=profile_keyboard())


def is_admin_user(user_id: int) -> bool:
    if ADMIN_IDS:
        return user_id in ADMIN_IDS
    return True


@dp.callback_query(F.data.startswith("mod_comments:"))
async def mod_comments_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Нет прав", show_alert=True)
        return
    _, raw_id = callback.data.split(":", maxsplit=1)
    submission_id = int(raw_id)
    comments = get_comments_for_submission(submission_id, limit=20)
    if not comments:
        await callback.answer("Комментариев нет")
        return
    lines = [f"Комментарии по submission {submission_id}:"]
    for row in comments:
        text = (row["comment_text"] or "").replace("\n", " ").strip()
        if len(text) > 120:
            text = text[:120] + "..."
        lines.append(f"- {row['user_id']}: {text}")
    await callback.answer()
    await callback.message.answer("\n".join(lines))


@dp.callback_query(F.data.startswith("mod_block:"))
async def mod_block_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Нет прав", show_alert=True)
        return
    _, raw_id = callback.data.split(":", maxsplit=1)
    submission_id = int(raw_id)
    ok = block_submission(submission_id)
    await callback.answer("Готово" if ok else "Не найдено")


@dp.callback_query(F.data.startswith("mod_delete:"))
async def mod_delete_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Нет прав", show_alert=True)
        return
    _, raw_id = callback.data.split(":", maxsplit=1)
    submission_id = int(raw_id)
    ok = delete_submission(submission_id)
    await callback.answer("Удалено" if ok else "Не найдено")


@dp.callback_query(F.data.startswith("mod_ban:"))
async def mod_ban_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Нет прав", show_alert=True)
        return
    _, raw_user, period = callback.data.split(":", maxsplit=2)
    target_user_id = int(raw_user)
    hours_map = {"1d": 24, "7d": 24 * 7, "1y": 24 * 365}
    hours = hours_map.get(period)
    if not hours:
        await callback.answer("Неверный период", show_alert=True)
        return
    set_user_ban(target_user_id, hours=hours, reason=f"admin:{period}")
    await callback.answer(f"Пользователь забанен: {period}")
    try:
        await callback.bot.send_message(
            target_user_id,
            f"Тебе выдан бан на {period}.",
        )
    except Exception:
        pass


@dp.callback_query(F.data.startswith("like:"))
async def like_callback(callback: CallbackQuery) -> None:
    _, raw_id = callback.data.split(":", maxsplit=1)
    submission_id = int(raw_id)
    add_reaction(callback.from_user.id, submission_id, "like")
    likes, dislikes = reaction_counts(submission_id)
    await callback.answer(f"Лайк учтен ({likes}/{dislikes})")


@dp.callback_query(F.data.startswith("dislike:"))
async def dislike_callback(callback: CallbackQuery) -> None:
    _, raw_id = callback.data.split(":", maxsplit=1)
    submission_id = int(raw_id)
    add_reaction(callback.from_user.id, submission_id, "dislike")
    likes, dislikes = reaction_counts(submission_id)
    await callback.answer(f"Дизлайк учтен ({likes}/{dislikes})")


@dp.callback_query(F.data.startswith("comment:"))
async def comment_callback(callback: CallbackQuery) -> None:
    _, raw_id = callback.data.split(":", maxsplit=1)
    submission_id = int(raw_id)
    PENDING_COMMENTS[callback.from_user.id] = submission_id
    await callback.answer("Жду текст комментария")
    await callback.message.answer("Напиши текст комментария следующим сообщением.")


@dp.callback_query(F.data.startswith("comments:"))
async def comments_list_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    _, raw_id = callback.data.split(":", maxsplit=1)
    submission_id = int(raw_id)

    submission = get_submission(submission_id)
    if not submission:
        await callback.message.answer("Кружок не найден.")
        return

    comments = get_comments_for_submission(submission_id, limit=15)
    if not comments:
        text = "Комментариев пока нет. Будь первым 👇"
    else:
        lines = ["Комментарии к кружку:"]
        for idx, row in enumerate(comments, start=1):
            comment = (row["comment_text"] or "").replace("\n", " ").strip()
            if len(comment) > 140:
                comment = comment[:140] + "..."
            lines.append(f"{idx}) {comment}")
        text = "\n".join(lines)

    await callback.message.answer(text, reply_markup=comments_keyboard(submission_id))


@dp.callback_query(F.data.startswith("report:"))
async def report_callback(callback: CallbackQuery) -> None:
    await callback.answer("Жалоба принята")
    _, raw_id = callback.data.split(":", maxsplit=1)
    submission_id = int(raw_id)
    reported = add_report(callback.from_user.id, submission_id)
    total_reports = total_reports_for_submission(submission_id)

    if total_reports >= 3:
        block_submission(submission_id)

    if reported:
        await send_admin_report(callback.bot, submission_id, callback.from_user.id, total_reports)
        await callback.message.answer("Спасибо! Репорт отправлен админу.")
    else:
        await callback.message.answer("Ты уже жаловался на этот кружок.")


@dp.message(Command("block"))
async def block_command(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Недостаточно прав.")
        return

    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Используй: /block <submission_id>")
        return

    submission_id = int(parts[1])
    if block_submission(submission_id):
        await message.answer(f"Кружок {submission_id} заблокирован.")
    else:
        await message.answer("Не найдено.")


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN env variable")

    init_db()
    bot = Bot(token=BOT_TOKEN)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запуск и приветствие"),
            BotCommand(command="menu", description="Главное меню"),
            BotCommand(command="profile", description="Профиль и язык"),
            BotCommand(command="random", description="Случайный кружок"),
            BotCommand(command="my", description="Мой кружок"),
            BotCommand(command="stats", description="Лимит и статистика"),
            BotCommand(command="invite", description="Реферальная ссылка"),
        ]
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
