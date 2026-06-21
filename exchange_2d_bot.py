import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from html import escape
from datetime import datetime, timezone

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

BTN_START = "🚀 Начать обмен"
BTN_VIEW_USERS = "👥 Смотреть пользователей"
BTN_MY_FILES = "📁 Мои файлы"
BTN_ACCEPT_RULES = "✅ 18+ Принимаю"
BTN_EXCHANGE = "🔄 Обменяться"
BTN_BACK = "◀️ Назад"

MAX_FILES_FOR_EXCHANGE = 5

RULES_TEXT = (
    "📜 <b>Правила 2D-Обмен</b>\n\n"
    "🔞 <b>СТРОГО ТОЛЬКО 18+.</b> Если вам нет 18 лет — НЕМЕДЛЕННО покиньте бота.\n\n"
    "📋 <b>Запрещено:</b>\n"
    "• контент для несовершеннолетних (CP), насилие, животные, зоо, шок-контент;\n"
    "• спам, реклама, воровство контента;\n"
    "• оскорбления, домогательства, разжигание ненависти;\n"
    "• любая реклама или предложения заработка;\n"
    "• нарушение <a href=\"https://telegram.org/tos\">правил Telegram</a>.\n\n"
    "⚖️ <b>НАРУШЕНИЕ ПРАВИЛ — БАН НАВСЕГДА.</b> Без предупреждения, без возможности разблокировки.\n\n"
    "🤝 Будьте вежливы. Не передавайте личные данные незнакомцам.\n\n"
    "Нажимая кнопку ниже, вы подтверждаете возраст 18+ и согласие с правилами."
)

WELCOME_TEXT = (
    "👋 Добро пожаловать в бот для обмена 2D-анимациями!\n\n"
    "📝 Сначала напишите ваш запрос (например: «Ищу обмен 2D-animation» или «Обменю с фетишами»).\n\n"
    "📁 Потом прикрепите {MAX_FILES_FOR_EXCHANGE} файлов (фото или видео, которые вы готовы обменять.\n\n"
    "🔄 Когда оба прикрепите 5 файлов и выберите пользователя — и обмен начнётся 5 на 5!"
).format(MAX_FILES_FOR_EXCHANGE=MAX_FILES_FOR_EXCHANGE)

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


class ExchangeBot(StatesGroup):
    waiting_request = State()
    waiting_files = State()
    waiting_exchange_response = State()
    waiting_exchange_files = State()


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
                files_count INTEGER DEFAULT 0,
                rules_accepted INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                file_type TEXT NOT NULL,
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
            """
        )
        conn.commit()


def ensure_user(user_id: int, first_name: str = None, username: str = None):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        user = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if user:
            conn.execute("UPDATE users SET last_active_at = ? WHERE user_id = ?", (now_iso(), user_id))
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
        row = conn.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return bool(row and row[0])


def set_user_request(user_id: int, request: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE users SET request = ? WHERE user_id = ?", (request, user_id))
        conn.commit()


def get_user_request(user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute("SELECT request FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else None


def get_user_files(user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute("SELECT file_id, file_type FROM user_files WHERE user_id = ?", (user_id,)).fetchall()
        return rows


def add_user_file(user_id: int, file_id: str, file_type: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO user_files (user_id, file_id, file_type, created_at) VALUES (?, ?, ?, ?)",
            (user_id, file_id, file_type, now_iso())
        )
        # Обновляем счётчик файлов
        count = conn.execute("SELECT COUNT(*) FROM user_files WHERE user_id = ?", (user_id,)).fetchone()[0]
        conn.execute("UPDATE users SET files_count = ? WHERE user_id = ?", (count, user_id))
        conn.commit()
        return count


def clear_user_files(user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM user_files WHERE user_id = ?", (user_id,))
        conn.execute("UPDATE users SET files_count = 0 WHERE user_id = ?", (user_id,))
        conn.commit()


def get_users_ready_for_exchange(exclude_user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT user_id, first_name, username, request, files_count FROM users "
            "WHERE user_id != ? AND rules_accepted = 1 AND is_banned = 0 AND files_count >= ?",
            (exclude_user_id, MAX_FILES_FOR_EXCHANGE)
        ).fetchall()
        return rows


def create_exchange(user1_id: int, user2_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO exchanges (user1_id, user2_id, created_at) VALUES (?, ?, ?)",
            (user1_id, user2_id, now_iso())
        )
        conn.commit()


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
        updated = conn.execute("SELECT * FROM exchanges WHERE id = ?", (exchange_id,)).fetchone()
        if updated[3] == 1 and updated[4] == 1:
            conn.execute("UPDATE exchanges SET status = 'approved' WHERE id = ?", (exchange_id,))
        
        conn.commit()
        return updated


def ban_user(user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
        conn.commit()


def rules_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_ACCEPT_RULES)],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def main_kb(is_admin: bool = False) -> ReplyKeyboardMarkup:
    kb = [[KeyboardButton(text=BTN_START), KeyboardButton(text=BTN_VIEW_USERS), KeyboardButton(text=BTN_MY_FILES)]
    if is_admin:
        kb.append([KeyboardButton(text="/admin_stats")
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
async def start_handler(message: Message, state: FSMContext, bot: Bot) -> None:
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
async def accept_rules_handler(message: Message, state: FSMContext, bot: Bot) -> None:
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


@dp.message(F.text == BTN_START)
async def start_exchange(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not await require_rules(message):
        return

    await state.set_state(ExchangeBot.waiting_request)
    await message.answer("📝 Напишите ваш запрос (например: «Ищу обмен 2D-animation»):", reply_markup=ReplyKeyboardRemove())


@dp.message(ExchangeBot.waiting_request, F.text)
async def got_request(message: Message, state: FSMContext):
    user_id = message.from_user.id
    request = message.text
    set_user_request(user_id, request)
    await state.set_state(ExchangeBot.waiting_files)
    await message.answer(f"✅ Запрос сохранён!\n\nТеперь прикрепите {MAX_FILES_FOR_EXCHANGE} файлов (фото или видео) для обмена:")


@dp.message(ExchangeBot.waiting_files)
async def got_file(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
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

    count = add_user_file(user_id, file_id, file_type)
    await message.answer(f"✅ Файл сохранён! ({count}/{MAX_FILES_FOR_EXCHANGE})")

    if count >= MAX_FILES_FOR_EXCHANGE:
        await state.clear()
        await message.answer(f"✅ Отлично! Вы загрузили все {MAX_FILES_FOR_EXCHANGE} файлов! Теперь вы можете смотреть пользователей и начинать обмен.",
                         reply_markup=main_kb(user_id in ADMIN_IDS))


@dp.message(F.text == BTN_MY_FILES)
async def my_files(message: Message, state: FSMContext):
    user_id = message.from_user.id
    files = get_user_files(user_id)

    if not files:
        await message.answer("❌ У вас нет загруженных файлов.")
        return

    await message.answer("📁 Ваши файлы:")
    for file_id, file_type in files:
        if file_type == "photo":
            await message.answer_photo(file_id)
        elif file_type == "video":
            await message.answer_video(file_id)
        elif file_type == "video_note":
            await message.answer_video_note(file_id)
        elif file_type == "document":
            await message.answer_document(file_id)


@dp.message(F.text == BTN_VIEW_USERS)
async def view_users(message: Message, state: FSMContext):
    user_id = message.from_user.id
    users = get_users_ready_for_exchange(user_id)

    if not users:
        await message.answer("❌ Пока нет пользователей готовых к обмену.")
        return

    for u in users:
        uid, name, uname, req, count = u
        name_text = name or "Пользователь"
        uname_text = f"@{uname}" if uname else ""
        text = f"<b>{escape(name_text)}</b> {uname_text}\n\n<b>Запрос:</b> {escape(req or 'Без запроса')}\n<b>Файлов:</b> {count}"
        
        inline_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=BTN_EXCHANGE, callback_data=f"exchange_{uid}")]
        ])
        await message.answer(text, reply_markup=inline_kb)


@dp.callback_query(F.data.startswith("exchange_"))
async def exchange_callback(query: CallbackQuery, state: FSMContext, bot: Bot):
    initiator_id = query.from_user.id
    target_id = int(query.data.split("_")[1])

    # Проверяем, загрузил ли иницииатор файлы
    initiator_files = get_user_files(initiator_id)
    if len(initiator_files) < MAX_FILES_FOR_EXCHANGE:
        await query.answer(f"❌ Сначала загрузите {MAX_FILES_FOR_EXCHANGE} файлов!", show_alert=True)
        return

    # Создаём обмен
    create_exchange(initiator_id, target_id)
    await query.answer("✅ Запрос на обмен отправлен!")

    # Уведомляем целевого пользователя
    try:
        await bot.send_message(
            target_id,
            f"🔄 Вам пришёл запрос на обмен от {query.from_user.first_name}!\n"
            f"Нажмите «Отправить файлы», чтобы начать обмен 5 на 5."
        )
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение: {e}")


@dp.message(Command("admin_stats"))
async def admin_stats(message: Message, state: FSMContext, bot: Bot):
    if message.from_user.id not in ADMIN_IDS:
        return

    with closing(sqlite3.connect(DB_PATH)) as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        ready = conn.execute("SELECT COUNT(*) FROM users WHERE files_count >= ?", (MAX_FILES_FOR_EXCHANGE,)).fetchone()[0]
        banned = conn.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1").fetchone()[0]

    await message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Всего пользователей: {total}\n"
        f"✅ Готовы к обмену: {ready}\n"
        f"🚫 Забанены: {banned}"
    )


async def main() -> None:
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    
    # Кэш для dp, чтобы не повторяться
    dp["bot"] = bot
    
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать работу"),
        BotCommand(command="admin_stats", description="Статистика (для админа)"),
    ])
    
    init_db()
    logging.info("Starting bot polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
