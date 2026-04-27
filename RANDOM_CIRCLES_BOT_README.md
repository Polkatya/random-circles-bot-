# Random Circles Bot (aiogram)

## Что умеет
- Принимает кружки (`video_note`) от пользователей.
- Выдает случайные кружки от других людей.
- Кнопки: "Следующий" и "Пожаловаться".
- Автоблок контента после 3 уникальных жалоб.
- Есть админ-команда `/block <submission_id>`.

## Запуск
1. Установи зависимости:
   `pip install -r requirements.txt`
2. Укажи переменные окружения:
   - `BOT_TOKEN` - токен твоего бота из BotFather.
   - или сохрани токен в файл `bot_token.txt` в корне проекта (подхватится автоматически).
   - `ADMIN_IDS` - через запятую, например: `123456789,987654321` (необязательно).
   - `DB_PATH` - путь к SQLite файлу (необязательно, по умолчанию `circles.db`).
   - `BOT_TOKEN_FILE` - путь к файлу с токеном (необязательно, по умолчанию `bot_token.txt`).
3. Запусти:
   `python random_circles_bot.py`

## Команды
- `/start` - старт и помощь.
- `/random` - показать случайный кружок.
- `/stats` - сколько новых кружков доступно.
- `/block <submission_id>` - заблокировать кружок (только для админов).

## Termux (Android)
1. Установка:
   - `pkg update && pkg upgrade -y`
   - `pkg install -y python git`
   - `python -m pip install --upgrade pip`
2. В папке проекта:
   - `pip install -r requirements-termux.txt`
3. Запуск:
   - `export BOT_TOKEN="your_token"`
   - `export ADMIN_CHAT_ID="-5273311194"` (если нужен мод-чат)
   - `chmod +x run.sh`
   - `./run.sh`
