import asyncio
import logging
import os
import sqlite3
import sys
from contextlib import closing
from html import escape
from datetime import datetime, timezone, timedelta

# Подавляем предупреждения asyncio при завершении на Windows
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart, StateFilter
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

DB_PATH = os.getenv("DB_PATH", "exchange_2d.db")
BOT_TOKEN_FILE = os.getenv("BOT_TOKEN_FILE", "bot_token.txt")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "7704968798")
ADMIN_IDS = {int(part.strip()) for part in ADMIN_IDS_RAW.split(",") if part.strip().isdigit()}

BTN_CREATE_EXCHANGE = "Создать обмен"
BTN_START_EXCHANGE = "Начать обмен"
BTN_MY_FILES = "📁 Мои файлы"
BTN_ACCEPT_RULES = "✅ 18+ Принимаю"
BTN_EXCHANGE = "🔄 Обменяться"
BTN_BACK = "◀️ Назад"
BTN_PREMIUM = "💎 Премиум"

MAX_FILES_FOR_EXCHANGE = 5

# Список запрещённых ключевых слов (можно дополнять)
FORBIDDEN_KEYWORDS = [
    "цп", "детское порно", "child porn", "cp", "лои", "лоли", "шота",
    "несовершеннолетние", "ребенок", "дети", "подросток", "подростков",
    "несовершеннолетняя", "несовершеннолетний", "lolita", "loli", "shota",
    "нсфл детей", "нсфл ребенок", "детское"
]

def check_forbidden_content(text: str) -> bool:
    """Проверяет, содержит ли текст запрещённые ключевые слова"""
    if not text:
        return False
    text_lower = text.lower()
    for keyword in FORBIDDEN_KEYWORDS:
        if keyword.lower() in text_lower:
            return True
    return False

def get_user_strikes(user_id: int) -> int:
    """Получает количество предупреждений пользователя"""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute("SELECT strikes FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else 0

def add_user_strike(user_id: int) -> int:
    """Добавляет предупреждение пользователю и возвращает новое количество"""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        current_strikes = get_user_strikes(user_id)
        new_strikes = current_strikes + 1
        conn.execute("UPDATE users SET strikes = ? WHERE user_id = ?", (new_strikes, user_id))
        conn.commit()
        return new_strikes

RULES_TEXT = (
    "📜 <b>Правила 2D-Обмен</b>\n\n"
    "🔞 <b>СТРОГО ЗАПРЕЩЕНО: контент с лицами младше 18 лет (даже в анимации)! За нарушение — БАН НАВСЕГДА!</b>\n\n"
    "📋 Также запрещено:\n"
    "• Контент с насилием, животными, зоо, шок\n"
    "• Спам, реклама, обман\n"
    "• Оскорбления, домогательства\n"
    "• Нарушение правил Telegram\n\n"
    "⚖️ <b>Бан без предупреждений</b> за любые нарушения!\n\n"
    "🤝 Как работает бот?\n"
    "1. Напиши, что ты ищете (например: «Ищу видео по Mortal Kombat»)\n"
    "2. Напиши, что ты даёшь (например: «Даю видео по СФ6»)\n"
    "3. Прикрепи 5 файлов\n"
    "4. Начни обмен с другими!\n\n"
    "Нажимая кнопку ниже, ты подтверждаешь возраст 18+ и согласие с правилами."
)

WELCOME_TEXT = (
    "👋 Добро пожаловать в бот для обмена контентом!\n\n"
    "📝 Нажми «Начать обмен», чтобы заполнить форму:\n"
    "1. Напиши, что ты ищете (например: «Ищу видео по Mortal Kombat»)\n"
    "2. Напиши, что ты даёшь (например: «Даю видео по СФ6»)\n"
    "3. Прикрепи 5 файлов (фото/видео)\n\n"
    "🔄 Затем ты сможешь найти других пользователей и обменяться с ними 5 на 5!"
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

# Инициализируем бота и диспетчер сразу, чтобы хендлеры работали
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())


class ExchangeBot(StatesGroup):
    waiting_what_seek = State()
    waiting_what_give = State()
    waiting_files = State()
    waiting_exchange_files = State()
    waiting_report_type = State()
    waiting_report_text = State()
    waiting_message = State()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                request TEXT,
                what_seek TEXT,
                what_give TEXT,
                files_count INTEGER DEFAULT 0,
                rules_accepted INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                is_premium INTEGER DEFAULT 0,
                premium_until TEXT,
                rating INTEGER DEFAULT 0,
                strikes INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                file_type TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS exchanges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user1_id INTEGER NOT NULL,
                user2_id INTEGER NOT NULL,
                user1_approved INTEGER DEFAULT 0,
                user2_approved INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER NOT NULL,
                reported_user_id INTEGER NOT NULL,
                report_type TEXT NOT NULL,
                report_text TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_user_id INTEGER NOT NULL,
                to_user_id INTEGER NOT NULL,
                rating INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_user_id INTEGER NOT NULL,
                to_user_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS exchange_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user1_id INTEGER NOT NULL,
                user2_id INTEGER NOT NULL,
                last_exchange TEXT NOT NULL,
                UNIQUE(user1_id, user2_id)
            );
            """
        )
        # Добавляем новые столбцы, если они не существуют — каждый отдельно
        try:
            conn.execute("ALTER TABLE users ADD COLUMN what_seek TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN what_give TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN is_premium INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN premium_until TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN rating INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN strikes INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE user_files ADD COLUMN is_active INTEGER DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        conn.commit()


def ensure_user(user_id: int, first_name: str = None, username: str = None):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        user = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if user:
            pass
        else:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name, created_at) VALUES (?, ?, ?, ?)",
                (user_id, username, first_name, now_iso())
            )
        conn.commit()


def user_accepted_rules(user_id: int) -> bool:
    if user_id in RULES_ACCEPTED_CACHE:
        return RULES_ACCEPTED_CACHE[user_id]
    
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute("SELECT rules_accepted FROM users WHERE user_id = ?", (user_id,)).fetchone()
        accepted = bool(row and row[0])
        if accepted:
            RULES_ACCEPTED_CACHE[user_id] = True
        return accepted


def accept_rules(user_id: int):
    ensure_user(user_id)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE users SET rules_accepted = 1 WHERE user_id = ?",
            (user_id,)
        )
        conn.commit()
    RULES_ACCEPTED_CACHE[user_id] = True


def is_user_banned(user_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute("SELECT is_banned, premium_until FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return False
        
        is_banned, premium_until = row
        if is_banned and premium_until:
            try:
                ban_until = datetime.fromisoformat(premium_until)
                if datetime.now(timezone.utc) > ban_until:
                    # Бан истек, разбаниваем
                    conn.execute("UPDATE users SET is_banned = 0, premium_until = NULL WHERE user_id = ?", (user_id,))
                    conn.commit()
                    return False
            except:
                pass
        return bool(is_banned)


def set_user_request(user_id: int, request: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE users SET request = ? WHERE user_id = ?", (request, user_id))
        conn.commit()


def set_what_seek(user_id: int, what_seek: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE users SET what_seek = ?, created_at = ? WHERE user_id = ?", (what_seek, now_iso(), user_id))
        conn.commit()


def set_what_give(user_id: int, what_give: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE users SET what_give = ?, created_at = ? WHERE user_id = ?", (what_give, now_iso(), user_id))
        conn.commit()


def has_exchanged(user1_id: int, user2_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        # Проверяем в обе стороны
        row = conn.execute("""
            SELECT last_exchange FROM exchange_history
            WHERE (user1_id = ? AND user2_id = ?) OR (user1_id = ? AND user2_id = ?)
        """, (user1_id, user2_id, user2_id, user1_id)).fetchone()
        if not row:
            return False
        last_exchange = row[0]
        
        # Проверяем, обновил ли пользователь анкету после последнего обмена
        row1 = conn.execute("SELECT created_at FROM users WHERE user_id = ?", (user2_id,)).fetchone()
        if row1 and row1[0] > last_exchange:
            return False
        
        # Проверяем, обновил ли файлы
        row2 = conn.execute("""
            SELECT MAX(created_at) FROM user_files
            WHERE user_id = ? AND is_active = 1
        """, (user2_id,)).fetchone()
        if row2 and row2[0] and row2[0] > last_exchange:
            return False
        
        return True


def save_exchange(user1_id: int, user2_id: int):
    now = now_iso()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        try:
            conn.execute("""
                INSERT OR REPLACE INTO exchange_history (user1_id, user2_id, last_exchange)
                VALUES (?, ?, ?)
            """, (user1_id, user2_id, now))
            conn.commit()
        except:
            pass


def get_user_request(user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute("SELECT request FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else None


def get_user_info(user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute("SELECT what_seek, what_give FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row if row else (None, None)


def get_user_files(user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute("SELECT file_id, file_type FROM user_files WHERE user_id = ? AND is_active = 1", (user_id,)).fetchall()
        return rows


def set_user_files_inactive(user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE user_files SET is_active = 0 WHERE user_id = ?", (user_id,))
        conn.execute("UPDATE users SET files_count = 0 WHERE user_id = ?", (user_id,))
        conn.commit()


def add_user_file(user_id: int, file_id: str, file_type: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        # Сначала посмотрим, сколько активных файлов уже есть
        current_active_count = conn.execute("SELECT COUNT(*) FROM user_files WHERE user_id = ? AND is_active = 1", (user_id,)).fetchone()[0]
        
        conn.execute(
            "INSERT INTO user_files (user_id, file_id, file_type, is_active, created_at) VALUES (?, ?, ?, 1, ?)",
            (user_id, file_id, file_type, now_iso())
        )
        # Обновляем счётчик файлов (только активные) и время изменения анкеты
        count = conn.execute("SELECT COUNT(*) FROM user_files WHERE user_id = ? AND is_active = 1", (user_id,)).fetchone()[0]
        conn.execute("UPDATE users SET files_count = ?, created_at = ? WHERE user_id = ?", (count, now_iso(), user_id))
        conn.commit()
        return count


def clear_user_files(user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM user_files WHERE user_id = ?", (user_id,))
        conn.execute("UPDATE users SET files_count = 0 WHERE user_id = ?", (user_id,))
        conn.commit()


def get_users_ready_for_exchange(exclude_user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        # Сначала сортируем по is_premium (1 первыми), потом по created_at
        rows = conn.execute(
            "SELECT user_id, first_name, username, what_seek, what_give, files_count, is_premium FROM users "
            "WHERE user_id != ? AND rules_accepted = 1 AND is_banned = 0 AND files_count >= ? "
            "ORDER BY is_premium DESC, created_at DESC",
            (exclude_user_id, MAX_FILES_FOR_EXCHANGE)
        ).fetchall()
        # Фильтруем пользователей, с которыми уже обменялись
        filtered_rows = []
        for row in rows:
            user_id = row[0]
            if not has_exchanged(exclude_user_id, user_id):
                filtered_rows.append(row)
        return filtered_rows


def is_user_premium(user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute("SELECT is_premium, premium_until FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return False
        is_premium, premium_until = row
        if not is_premium:
            return False
        # Проверяем, не истек ли премиум
        if premium_until:
            try:
                until = datetime.fromisoformat(premium_until)
                now = datetime.now(timezone.utc)
                if now > until:
                    # Премиум истек, обновляем базу
                    conn.execute("UPDATE users SET is_premium = 0, premium_until = NULL WHERE user_id = ?", (user_id,))
                    conn.commit()
                    return False
            except:
                pass  # Если дата не валидна, считаем премиум активным
        return True


def grant_premium(user_id: int, hours: int = 24):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
        conn.execute(
            "UPDATE users SET is_premium = 1, premium_until = ? WHERE user_id = ?",
            (until, user_id)
        )
        conn.commit()


def create_exchange(user1_id: int, user2_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        # Проверяем, нет ли уже активного обмена
        existing = conn.execute(
            "SELECT id FROM exchanges WHERE ((user1_id = ? AND user2_id = ?) OR (user1_id = ? AND user2_id = ?)) AND status = 'pending'",
            (user1_id, user2_id, user2_id, user1_id)
        ).fetchone()
        if existing:
            return existing[0]
        
        conn.execute(
            "INSERT INTO exchanges (user1_id, user2_id, created_at) VALUES (?, ?, ?)",
            (user1_id, user2_id, now_iso())
        )
        conn.commit()
        return conn.lastrowid


def get_exchange(exchange_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute("SELECT * FROM exchanges WHERE id = ?", (exchange_id,)).fetchone()
        return row


def approve_exchange(exchange_id: int, user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        exchange = conn.execute("SELECT * FROM exchanges WHERE id = ?", (exchange_id,)).fetchone()
        if not exchange:
            return None
        
        user1_id, user2_id = exchange[1], exchange[2]
        
        if user_id == user1_id:
            conn.execute("UPDATE exchanges SET user1_approved = 1 WHERE id = ?", (exchange_id,))
        elif user_id == user2_id:
            conn.execute("UPDATE exchanges SET user2_approved = 1 WHERE id = ?", (exchange_id,))
        else:
            return None
        
        # Проверяем, оба ли одобрили
        temp = conn.execute("SELECT * FROM exchanges WHERE id = ?", (exchange_id,)).fetchone()
        if temp[3] == 1 and temp[4] == 1:
            conn.execute("UPDATE exchanges SET status = 'approved' WHERE id = ?", (exchange_id,))
            conn.commit()
            # Получаем уже обновленный объект с новым статусом
            updated = conn.execute("SELECT * FROM exchanges WHERE id = ?", (exchange_id,)).fetchone()
        else:
            updated = temp
            conn.commit()
        return updated


def ban_user(user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
        conn.commit()


def rules_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_ACCEPT_RULES)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def main_kb(is_admin: bool = False) -> ReplyKeyboardMarkup:
    kb = [[KeyboardButton(text=BTN_CREATE_EXCHANGE), KeyboardButton(text=BTN_START_EXCHANGE), KeyboardButton(text=BTN_MY_FILES)],
          [KeyboardButton(text=BTN_PREMIUM)]]
    if is_admin:
        kb.append([KeyboardButton(text="/admin_stats")])
    return ReplyKeyboardMarkup(
        keyboard=kb,
        resize_keyboard=True,
    )


async def require_rules(message: Message) -> bool:
    user_id = message.from_user.id
    if user_accepted_rules(user_id):
        return True
    await message.answer(RULES_TEXT, reply_markup=rules_kb())
    return False


@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    user_id = message.from_user.id
    first_name = message.from_user.first_name
    username = message.from_user.username
    ensure_user(user_id, first_name, username)

    if is_user_banned(user_id):
        await message.answer("❌ Вы заблокированы навсегда.")
        return

    if not await require_rules(message):
        return

    await state.clear()
    await message.answer(WELCOME_TEXT, reply_markup=main_kb(user_id in ADMIN_IDS))


@dp.message(F.text == BTN_ACCEPT_RULES)
async def accept_rules_handler(message: Message, state: FSMContext):
    user_id = message.from_user.id

    if user_id in RULES_ACCEPTED_CACHE:
        del RULES_ACCEPTED_CACHE[user_id]

    accept_rules(user_id)
    await state.clear()
    await message.answer("✅ Правила приняты!")
    await send_welcome(message)


async def send_welcome(message: Message):
    user_id = message.from_user.id
    await message.answer(WELCOME_TEXT, reply_markup=main_kb(user_id in ADMIN_IDS))


@dp.message(F.text == BTN_CREATE_EXCHANGE)
async def create_exchange(message: Message, state: FSMContext):
    user_id = message.from_user.id
    logger.info(f"Создаём обмен для пользователя {user_id}")
    if not await require_rules(message):
        logger.info(f"Пользователь {user_id} не принял правила")
        return
    
    # Сразу очищаем состояние на всякий случай
    logger.info(f"Очищаем состояние для пользователя {user_id}")
    await state.clear()
    logger.info(f"Устанавливаем состояние waiting_what_seek для пользователя {user_id}")
    await state.set_state(ExchangeBot.waiting_what_seek)
    await message.answer(
        "📝 <b>Часть 1/3</b>\nНапишите, что вы ищете для обмена (например: «Ищу видео по Mortal Kombat 18+»):",
        reply_markup=ReplyKeyboardRemove()
    )


@dp.message(ExchangeBot.waiting_what_seek, F.text)
async def got_what_seek(message: Message, state: FSMContext):
    user_id = message.from_user.id
    logger.info(f"Получено что ищет пользователь {user_id}: {message.text}")
    what_seek = message.text
    
    # Проверка на запрещённый контент
    if check_forbidden_content(what_seek):
        strikes = add_user_strike(user_id)
        if strikes >= 2:
            # Забаним пользователя при повторном нарушении
            with closing(sqlite3.connect(DB_PATH)) as conn:
                conn.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
                conn.commit()
            await message.answer(
                "❌ <b>ВАША АНКЕТА СОДЕРЖИТ ЗАПРЕЩЁННЫЙ КОНТЕНТ!</b>\n\n"
                "⚠️ Вы нарушили правила повторно — вы заблокированы навсегда!\n"
                "‼️ Жалоба отправлена в Telegram и соответствующие органы."
            )
            await state.clear()
            return
        else:
            await message.answer(
                "⚠️ <b>ВНИМАНИЕ! ЗАПРЕЩЁННЫЙ КОНТЕНТ!</b>\n\n"
                "❌ Вы указали запрещённую тематику!\n"
                "‼️ При повторном нарушении — жалоба будет отправлена в Telegram и органы!"
            )
            return
    
    set_what_seek(user_id, what_seek)
    logger.info(f"Устанавливаем состояние waiting_what_give для пользователя {user_id}")
    await state.set_state(ExchangeBot.waiting_what_give)
    await message.answer(
        "✅ Запрос сохранён!\n\n📝 <b>Часть 2/3</b>\nНапишите, что вы даёте в обмен (например: «Даю видео по Street Fighter 6»):"
    )


@dp.message(ExchangeBot.waiting_what_give, F.text)
async def got_what_give(message: Message, state: FSMContext):
    user_id = message.from_user.id
    logger.info(f"Получено что даёт пользователь {user_id}: {message.text}")
    what_give = message.text
    
    # Проверка на запрещённый контент
    if check_forbidden_content(what_give):
        strikes = add_user_strike(user_id)
        if strikes >= 2:
            # Забаним пользователя при повторном нарушении
            with closing(sqlite3.connect(DB_PATH)) as conn:
                conn.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
                conn.commit()
            await message.answer(
                "❌ <b>ВАША АНКЕТА СОДЕРЖИТ ЗАПРЕЩЁННЫЙ КОНТЕНТ!</b>\n\n"
                "⚠️ Вы нарушили правила повторно — вы заблокированы навсегда!\n"
                "‼️ Жалоба отправлена в Telegram и соответствующие органы."
            )
            await state.clear()
            return
        else:
            await message.answer(
                "⚠️ <b>ВНИМАНИЕ! ЗАПРЕЩЁННЫЙ КОНТЕНТ!</b>\n\n"
                "❌ Вы указали запрещённую тематику!\n"
                "‼️ При повторном нарушении — жалоба будет отправлена в Telegram и органы!"
            )
            return
    
    set_what_give(user_id, what_give)
    # Сбросим старые файлы, чтобы не было конфликтов
    logger.info(f"Сбрасываем старые файлы для пользователя {user_id}")
    set_user_files_inactive(user_id)
    
    logger.info(f"Устанавливаем состояние waiting_files для пользователя {user_id}")
    await state.set_state(ExchangeBot.waiting_files)
    await message.answer(
        f"✅ Готово!\n\n📝 <b>Часть 3/3</b>\nПрикрепите 5 файлов с соответствующей тематики (фото/видео):"
    )


@dp.message(ExchangeBot.waiting_files, F.photo)
@dp.message(ExchangeBot.waiting_files, F.video)
@dp.message(ExchangeBot.waiting_files, F.video_note)
@dp.message(ExchangeBot.waiting_files, F.document)
async def got_file(message: Message, state: FSMContext):
    user_id = message.from_user.id
    logger.info(f"Получен файл от пользователя {user_id}")
    file_id = None
    file_type = None

    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
        logger.info("Тип: фото")
    elif message.video:
        file_id = message.video.file_id
        file_type = "video"
        logger.info("Тип: видео")
    elif message.video_note:
        file_id = message.video_note.file_id
        file_type = "video_note"
        logger.info("Тип: видеозаметка")
    elif message.document:
        file_id = message.document.file_id
        file_type = "document"
        logger.info("Тип: документ")

    if not file_id:
        await message.answer("⚠️ Пришлите фото или видео!")
        return

    count = add_user_file(user_id, file_id, file_type)
    logger.info(f"Файл сохранён, всего: {count}")
    await message.answer(f"✅ Файл сохранён! ({count}/{MAX_FILES_FOR_EXCHANGE})")

    if count >= MAX_FILES_FOR_EXCHANGE:
        await state.clear()
        await message.answer(
            f"✅ Отлично! Вы загрузили все {MAX_FILES_FOR_EXCHANGE} файлов!\nТеперь вы можете смотреть пользователей и начинать обмен.",
            reply_markup=main_kb(user_id in ADMIN_IDS)
        )

@dp.message(ExchangeBot.waiting_files)
async def got_file_error(message: Message, state: FSMContext):
    logger.info(f"Получено сообщение неправильного типа от пользователя {message.from_user.id}: {message.content_type}")
    await message.answer("⚠️ Пришлите фото или видео!")


@dp.message(F.text == BTN_MY_FILES)
async def my_files(message: Message, state: FSMContext):
    user_id = message.from_user.id
    files = get_user_files(user_id)

    if not files:
        await message.answer("❌ У вас нет активных файлов для обмена. Нажмите «Создать обмен» чтобы загрузить новые!")
        return

    await message.answer("📁 Ваши активные файлы для обмена:")
    for file_id, file_type in files:
        if file_type == "photo":
            await message.answer_photo(file_id)
        elif file_type == "video":
            await message.answer_video(file_id)
        elif file_type == "video_note":
            await message.answer_video_note(file_id)
        elif file_type == "document":
            await message.answer_document(file_id)
    
    # Добавляем кнопку для удаления и загрузки новых файлов
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🗑️ Удалить все и загрузить новые"), KeyboardButton(text="◀️ Назад")]],
        resize_keyboard=True
    )
    await message.answer("Вы можете удалить все активные файлы и загрузить новые:", reply_markup=kb)


@dp.message(F.text == "🗑️ Удалить все и загрузить новые")
async def clear_and_restart(message: Message, state: FSMContext):
    user_id = message.from_user.id
    set_user_files_inactive(user_id)
    await state.clear()
    await message.answer("✅ Все активные файлы удалены!")
    # Переводим пользователя на шаг загрузки новых файлов
    await state.set_state(ExchangeBot.waiting_what_seek)
    await message.answer(
        "📝 <b>Часть 1/3</b>\nНапишите, что вы ищете для обмена (например: «Ищу видео по Mortal Kombat 18+»):",
        reply_markup=ReplyKeyboardRemove()
    )


@dp.message(F.text == "◀️ Назад")
async def go_back(message: Message, state: FSMContext):
    user_id = message.from_user.id
    await state.clear()
    await message.answer("🏠 Главное меню", reply_markup=main_kb(user_id in ADMIN_IDS))


def get_user_rating(user_id: int) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        # Получаем сумму всех рейтингов для пользователя
        total = conn.execute("SELECT COALESCE(SUM(rating), 0) FROM ratings WHERE to_user_id = ?", (user_id,)).fetchone()[0]
        return total

async def show_user_profile(message_or_query, user_id: int, users_list: list, current_idx: int = 0, state: FSMContext = None):
    # Сохраняем состояние
    if state:
        await state.update_data(users_list=users_list, current_idx=current_idx)
    
    total = len(users_list)
    if current_idx >= total:
        if hasattr(message_or_query, 'message'):
            await message_or_query.message.answer("✅ Это все пользователи!")
        else:
            await message_or_query.answer("✅ Это все пользователи!", show_alert=True)
        return
    
    u = users_list[current_idx]
    uid, name, uname, what_seek, what_give, count, is_premium = u
    rating = get_user_rating(uid)
    text = f"🔄 Запрос на обмен ({current_idx + 1}/{total})\n\n"
    if is_premium:
        text += "💎 Премиум пользователь\n\n"
    text += f"⭐ Рейтинг: {rating}\n"
    text += f"🔍 Ищет: {escape(what_seek or 'не указано')}\n"
    text += f"🎁 Даёт: {escape(what_give or 'не указано')}"
    
    # Формируем клавиатуру
    keyboard_buttons = []
    keyboard_buttons.append([InlineKeyboardButton(text="🔄 Обменяться", callback_data=f"exchange_{uid}")])
    keyboard_buttons.append([InlineKeyboardButton(text="⚠️ Пожаловаться", callback_data=f"report_{uid}")])
    
    # Добавляем кнопку «Дальше», если не последняя анкета
    if current_idx < total - 1:
        keyboard_buttons.append([InlineKeyboardButton(text="➡️ Дальше", callback_data=f"next_{current_idx}")])
    
    inline_kb = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    
    if hasattr(message_or_query, 'message'):
        if hasattr(message_or_query.message, 'edit_text'):
            await message_or_query.message.edit_text(text, reply_markup=inline_kb)
        else:
            await message_or_query.answer(text, reply_markup=inline_kb)
    else:
        await message_or_query.answer(text, reply_markup=inline_kb)

@dp.message(F.text == BTN_START_EXCHANGE)
async def start_exchange(message: Message, state: FSMContext):
    user_id = message.from_user.id
    users = get_users_ready_for_exchange(user_id)

    if not users:
        await message.answer("❌ Пока нет пользователей готовых к обмену.")
        return

    await show_user_profile(message, user_id, users, 0, state)

@dp.callback_query(F.data.startswith("next_"))
async def next_profile_callback(query: CallbackQuery, state: FSMContext):
    await query.answer()  # Сразу отвечаем на callback
    current_idx = int(query.data.split("_")[1])
    data = await state.get_data()
    users_list = data.get("users_list")
    next_idx = current_idx + 1
    
    await show_user_profile(query, query.from_user.id, users_list, next_idx, state)


@dp.callback_query(F.data.startswith("exchange_"))
async def exchange_callback(query: CallbackQuery, state: FSMContext):
    await query.answer()  # Сразу отвечаем на callback
    initiator_id = query.from_user.id
    target_id = int(query.data.split("_")[1])

    # Проверяем, загрузил ли целевой пользователь файлы
    target_files = get_user_files(target_id)
    if len(target_files) < MAX_FILES_FOR_EXCHANGE:
        await query.answer("❌ Этот пользователь ещё не загрузил файлы!", show_alert=True)
        return

    # Получаем информацию, что ищет целевой пользователь
    target_info = get_user_info(target_id)
    target_what_seek = target_info[0] or "контент"

    # Сохраняем ID целевого пользователя в состоянии
    await state.update_data(target_id=target_id, exchange_files=[])
    await state.set_state(ExchangeBot.waiting_exchange_files)

    await query.message.answer(
        f"📝 Пришлите 5 файлов по тематике: «{escape(target_what_seek)}»\n"
        f"После этого обмен произойдёт автоматически!"
    )

@dp.message(ExchangeBot.waiting_exchange_files, F.photo)
@dp.message(ExchangeBot.waiting_exchange_files, F.video)
@dp.message(ExchangeBot.waiting_exchange_files, F.video_note)
@dp.message(ExchangeBot.waiting_exchange_files, F.document)
async def got_exchange_file(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    target_id = data.get("target_id")
    exchange_files = data.get("exchange_files", [])

    file_id = None
    file_type = None

    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
    elif message.video:
        file_id = message.video.file_id
        file_type = "video"
    elif message.video_note:
        file_id = message.video_note.file_id
        file_type = "video_note"
    elif message.document:
        file_id = message.document.file_id
        file_type = "document"

    if not file_id:
        await message.answer("⚠️ Пришлите фото или видео!")
        return

    exchange_files.append((file_id, file_type))
    await state.update_data(exchange_files=exchange_files)
    count = len(exchange_files)
    await message.answer(f"✅ Файл сохранён! ({count}/{MAX_FILES_FOR_EXCHANGE})")

    if count >= MAX_FILES_FOR_EXCHANGE:
        await state.clear()

        # Проверяем, загрузил ли целевой пользователь файлы (ещё раз)
        target_files = get_user_files(target_id)
        if len(target_files) < MAX_FILES_FOR_EXCHANGE:
            await message.answer("❌ Этот пользователь уже удалил свои файлы!")
            return

        # Получаем информацию
        initiator_info = get_user_info(user_id)
        target_info = get_user_info(target_id)
        initiator_what_seek = initiator_info[0] or "контент"
        target_what_seek = target_info[0] or "контент"

        await message.answer("✅ Обмен начался!")

        # Сохраняем обмен в историю
        save_exchange(user_id, target_id)

        # Отправляем файлы целевому пользователю
        try:
            await bot.send_message(target_id, f"🔄 Обмен! Вот контент от другого пользователя ({escape(target_what_seek)}):")
            for f_id, f_type in exchange_files:
                if f_type == "photo":
                    await bot.send_photo(target_id, f_id)
                elif f_type == "video":
                    await bot.send_video(target_id, f_id)
                elif f_type == "video_note":
                    await bot.send_video_note(target_id, f_id)
                elif f_type == "document":
                    await bot.send_document(target_id, f_id)
            
            # Добавляем кнопки для целевого пользователя
            target_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👍 Лайк", callback_data=f"like_{user_id}"), InlineKeyboardButton(text="👎 Дизлайк", callback_data=f"dislike_{user_id}")],
                [InlineKeyboardButton(text="💬 Отправить сообщение", callback_data=f"msg_{user_id}")],
                [InlineKeyboardButton(text="⚠️ Пожаловаться на пользователя", callback_data=f"report_{user_id}")]
            ])
            await bot.send_message(target_id, "Оцените пользователя или напишите ему:", reply_markup=target_kb)
        except Exception as e:
            logger.error(f"Не удалось отправить файлы целевому пользователю: {e}")

        # Отправляем файлы инициатору
        try:
            await bot.send_message(user_id, f"🔄 Обмен! Вот контент для тебя ({escape(initiator_what_seek)}):")
            for f_id, f_type in target_files:
                if f_type == "photo":
                    await bot.send_photo(user_id, f_id)
                elif f_type == "video":
                    await bot.send_video(user_id, f_id)
                elif f_type == "video_note":
                    await bot.send_video_note(user_id, f_id)
                elif f_type == "document":
                    await bot.send_document(user_id, f_id)
            
            # Добавляем кнопки для инициатора
            initiator_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👍 Лайк", callback_data=f"like_{target_id}"), InlineKeyboardButton(text="👎 Дизлайк", callback_data=f"dislike_{target_id}")],
                [InlineKeyboardButton(text="💬 Отправить сообщение", callback_data=f"msg_{target_id}")],
                [InlineKeyboardButton(text="⚠️ Пожаловаться на пользователя", callback_data=f"report_{target_id}")]
            ])
            await bot.send_message(user_id, "Оцените пользователя или напишите ему:", reply_markup=initiator_kb)
        except Exception as e:
            logger.error(f"Не удалось отправить файлы инициатору: {e}")

@dp.message(ExchangeBot.waiting_exchange_files)
async def got_exchange_file_error(message: Message, state: FSMContext):
    await message.answer("⚠️ Пришлите фото или видео!")


@dp.callback_query(F.data.startswith("report_"))
async def report_callback(query: CallbackQuery, state: FSMContext):
    await query.answer()  # Сразу отвечаем на callback
    reporter_id = query.from_user.id
    reported_user_id = int(query.data.split("_")[1])
    
    logger.info(f"Пользователь {reporter_id} хочет пожаловаться на {reported_user_id}")
    
    await state.update_data(reported_user_id=reported_user_id)
    await state.set_state(ExchangeBot.waiting_report_type)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 Нарушение правил Telegram или законов", callback_data="rtype_rules")],
        [InlineKeyboardButton(text="🚫 Отправка контента не соответствующему тематике", callback_data="rtype_theme")],
        [InlineKeyboardButton(text="📝 Другое", callback_data="rtype_other")],
    ])
    
    await query.message.answer("⚠️ Выберите причину жалобы:", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("rtype_"))
async def report_type_callback(query: CallbackQuery, state: FSMContext):
    await query.answer()  # Сразу отвечаем на callback
    reporter_id = query.from_user.id
    report_type = query.data.split("_")[1]
    
    type_texts = {
        "rules": "Нарушение правил Telegram или законов",
        "theme": "Отправка контента не соответствующему тематике",
        "other": "Другое"
    }
    
    await state.update_data(report_type=type_texts[report_type])
    
    if report_type == "other":
        await state.set_state(ExchangeBot.waiting_report_text)
        await query.message.answer("📝 Опишите проблему:")
    else:
        await save_report(query, state)


@dp.callback_query(F.data.startswith("like_"))
async def like_callback(query: CallbackQuery, state: FSMContext):
    await query.answer()  # Сразу отвечаем на callback
    from_user_id = query.from_user.id
    to_user_id = int(query.data.split("_")[1])
    
    # Проверяем, не оценил ли пользователь уже этого человека
    with closing(sqlite3.connect(DB_PATH)) as conn:
        existing = conn.execute("SELECT id FROM ratings WHERE from_user_id = ? AND to_user_id = ?", (from_user_id, to_user_id)).fetchone()
        if existing:
            await query.answer("Ты уже оценивал этого пользователя!", show_alert=True)
            return
        
        # Добавляем лайк (+1 к рейтингу)
        conn.execute("INSERT INTO ratings (from_user_id, to_user_id, rating, created_at) VALUES (?, ?, 1, ?)", (from_user_id, to_user_id, now_iso()))
        conn.commit()
    
    await query.answer("👍 Лайк поставлен!")
    await query.message.edit_text("Спасибо за оценку!")

@dp.callback_query(F.data.startswith("dislike_"))
async def dislike_callback(query: CallbackQuery, state: FSMContext):
    await query.answer()  # Сразу отвечаем на callback
    from_user_id = query.from_user.id
    to_user_id = int(query.data.split("_")[1])
    
    # Проверяем, не оценил ли пользователь уже этого человека
    with closing(sqlite3.connect(DB_PATH)) as conn:
        existing = conn.execute("SELECT id FROM ratings WHERE from_user_id = ? AND to_user_id = ?", (from_user_id, to_user_id)).fetchone()
        if existing:
            await query.answer("Ты уже оценивал этого пользователя!", show_alert=True)
            return
        
        # Добавляем дизлайк (-1 к рейтингу)
        conn.execute("INSERT INTO ratings (from_user_id, to_user_id, rating, created_at) VALUES (?, ?, -1, ?)", (from_user_id, to_user_id, now_iso()))
        conn.commit()
    
    await query.answer("👎 Дизлайк поставлен!")
    await query.message.edit_text("Спасибо за оценку!")

@dp.callback_query(F.data.startswith("msg_"))
async def message_callback(query: CallbackQuery, state: FSMContext):
    await query.answer()  # Сразу отвечаем на callback
    to_user_id = int(query.data.split("_")[1])
    
    await state.update_data(to_user_id=to_user_id)
    await state.set_state(ExchangeBot.waiting_message)
    await query.message.answer("💬 Напиши сообщение, которое хочешь отправить:")

@dp.message(ExchangeBot.waiting_message, F.text)
async def send_user_message(message: Message, state: FSMContext):
    from_user_id = message.from_user.id
    data = await state.get_data()
    to_user_id = data.get("to_user_id")
    
    # Сохраняем сообщение в базу
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("INSERT INTO user_messages (from_user_id, to_user_id, message, created_at) VALUES (?, ?, ?, ?)", (from_user_id, to_user_id, message.text, now_iso()))
        conn.commit()
    
    # Отправляем сообщение пользователю
    try:
        # Сохраняем ID отправителя для возможности ответа
        await bot.send_message(
            to_user_id,
            f"💬 Новое сообщение от пользователя:\n{escape(message.text)}"
        )
        # Добавляем кнопку ответа
        reply_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Ответить", callback_data=f"reply_{from_user_id}")]
        ])
        await bot.send_message(to_user_id, "Вы можете ответить:", reply_markup=reply_kb)
        await message.answer("✅ Сообщение отправлено!")
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение: {e}")
        await message.answer("❌ Не удалось отправить сообщение.")
    
    await state.clear()

@dp.callback_query(F.data.startswith("reply_"))
async def reply_callback(query: CallbackQuery, state: FSMContext):
    await query.answer()  # Сразу отвечаем на callback
    to_user_id = int(query.data.split("_")[1])
    
    await state.update_data(to_user_id=to_user_id)
    await state.set_state(ExchangeBot.waiting_message)
    await query.message.answer("💬 Напиши ответ:")

@dp.callback_query(F.data.startswith("delpost_"))
async def delete_post_callback(query: CallbackQuery, state: FSMContext):
    await query.answer()  # Сразу отвечаем на callback
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Ты не админ!", show_alert=True)
        return
    
    user_id = int(query.data.split("_")[1])
    set_user_files_inactive(user_id)
    await query.answer("✅ Пост удалён!")
    await query.message.edit_text("Действие выполнено.")

@dp.callback_query(F.data.startswith("ban1_"))
async def ban1_callback(query: CallbackQuery, state: FSMContext):
    await query.answer()  # Сразу отвечаем на callback
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Ты не админ!", show_alert=True)
        return
    
    user_id = int(query.data.split("_")[1])
    ban_until = datetime.now(timezone.utc) + timedelta(days=1)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE users SET is_banned = 1, premium_until = ? WHERE user_id = ?", (ban_until.isoformat(), user_id))
        conn.commit()
    
    # Удаляем файлы
    set_user_files_inactive(user_id)
    await query.answer("✅ Пользователь забанен на 1 день!")
    await query.message.edit_text("Действие выполнено.")

@dp.callback_query(F.data.startswith("ban7_"))
async def ban7_callback(query: CallbackQuery, state: FSMContext):
    await query.answer()  # Сразу отвечаем на callback
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Ты не админ!", show_alert=True)
        return
    
    user_id = int(query.data.split("_")[1])
    ban_until = datetime.now(timezone.utc) + timedelta(days=7)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE users SET is_banned = 1, premium_until = ? WHERE user_id = ?", (ban_until.isoformat(), user_id))
        conn.commit()
    
    # Удаляем файлы
    set_user_files_inactive(user_id)
    await query.answer("✅ Пользователь забанен на 7 дней!")
    await query.message.edit_text("Действие выполнено.")

@dp.callback_query(F.data.startswith("noaction_"))
async def noaction_callback(query: CallbackQuery, state: FSMContext):
    await query.answer()  # Сразу отвечаем на callback
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Ты не админ!", show_alert=True)
        return
    
    await query.answer("✅ Ничего не сделано.")
    await query.message.edit_text("Действие выполнено.")

@dp.message(ExchangeBot.waiting_report_text, F.text)
async def got_report_text(message: Message, state: FSMContext):
    await state.update_data(report_text=message.text)
    await save_report(message, state)


async def save_report(event, state: FSMContext):
    data = await state.get_data()
    reporter_id = event.from_user.id
    reported_user_id = data.get("reported_user_id")
    report_type = data.get("report_type")
    report_text = data.get("report_text", "")
    
    logger.info(f"Сохраняем репорт: reporter={reporter_id}, reported={reported_user_id}, type={report_type}")
    
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO reports (reporter_id, reported_user_id, report_type, report_text, created_at) VALUES (?, ?, ?, ?, ?)",
            (reporter_id, reported_user_id, report_type, report_text, now_iso())
        )
        conn.commit()
    
    await state.clear()
    
    if hasattr(event, 'message'):
        await event.message.answer("✅ Спасибо за обращение! Мы проверим вашу жалобу.", reply_markup=main_kb(event.from_user.id in ADMIN_IDS))
    else:
        await event.answer("✅ Спасибо за обращение! Мы проверим вашу жалобу.", show_alert=True)
    
    # Отправляем репорт админам с файлами пользователя
    reported_user_files = get_user_files(reported_user_id)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"⚠️ <b>НОВАЯ ЖАЛОБА</b>\n\n"
                f"Пожаловался: {event.from_user.id} ({event.from_user.username or 'Нет юзернейма'})\n"
                f"На пользователя: {reported_user_id}\n"
                f"Причина: {report_type}\n"
                f"{'Комментарий: ' + report_text if report_text else ''}"
            )
            # Пересылаем файлы пользователя
            if reported_user_files:
                await bot.send_message(admin_id, "📁 Файлы пользователя:")
                for file_id, file_type in reported_user_files:
                    if file_type == "photo":
                        await bot.send_photo(admin_id, file_id)
                    elif file_type == "video":
                        await bot.send_video(admin_id, file_id)
                    elif file_type == "video_note":
                        await bot.send_video_note(admin_id, file_id)
                    elif file_type == "document":
                        await bot.send_document(admin_id, file_id)
            # Добавляем админские кнопки
            admin_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🗑️ Удалить пост", callback_data=f"delpost_{reported_user_id}")],
                [InlineKeyboardButton(text="🚫 Бан на 1 день", callback_data=f"ban1_{reported_user_id}")],
                [InlineKeyboardButton(text="🚫 Бан на 7 дней", callback_data=f"ban7_{reported_user_id}")],
                [InlineKeyboardButton(text="✅ Ничего не делать", callback_data=f"noaction_{reported_user_id}")]
            ])
            await bot.send_message(admin_id, "Выберите действие:", reply_markup=admin_kb)
        except Exception as e:
            logger.error(f"Не удалось отправить репорт админу {admin_id}: {e}")


@dp.message(Command("admin_stats"))
async def admin_stats(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    with closing(sqlite3.connect(DB_PATH)) as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        ready = conn.execute("SELECT COUNT(*) FROM users WHERE files_count >= ?", (MAX_FILES_FOR_EXCHANGE,)).fetchone()[0]
        banned = conn.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1").fetchone()[0]
        active_exchanges = conn.execute("SELECT COUNT(*) FROM exchanges WHERE status = 'pending'").fetchone()[0]

    await message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Всего пользователей: {total}\n"
        f"✅ Готовы к обмену: {ready}\n"
        f"🔄 Ожидающих обмена: {active_exchanges}\n"
        f"🚫 Забанены: {banned}"
    )


async def main() -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать работу"),
        BotCommand(command="admin_stats", description="Статистика (для админа)"),
    ])

    init_db()
    logging.info("Starting bot polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
