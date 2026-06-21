import sqlite3
from contextlib import closing

DB_PATH = "exchange_2d.db"

print("Исправляем базу данных...")

with closing(sqlite3.connect(DB_PATH)) as conn:
    # Создаём таблицу репортов, если её нет
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter_id INTEGER NOT NULL,
            reported_user_id INTEGER NOT NULL,
            report_type TEXT NOT NULL,
            report_text TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    # Создаём таблицу рейтинга
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user_id INTEGER NOT NULL,
            to_user_id INTEGER NOT NULL,
            rating INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    # Создаём таблицу сообщений
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user_id INTEGER NOT NULL,
            to_user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    # Создаём таблицу истории обменов
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS exchange_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user1_id INTEGER NOT NULL,
            user2_id INTEGER NOT NULL,
            last_exchange TEXT NOT NULL,
            UNIQUE(user1_id, user2_id)
        );
        """
    )
    conn.commit()
    print("Таблицы reports, ratings, user_messages, exchange_history проверены/созданы!")
    
    # Проверяем, есть ли столбец is_active в user_files
    cursor = conn.execute("PRAGMA table_info(user_files)")
    columns = [row[1] for row in cursor.fetchall()]
    print(f"Столбцы в user_files: {columns}")
    
    if "is_active" not in columns:
        print("Добавляем столбец is_active...")
        conn.execute("ALTER TABLE user_files ADD COLUMN is_active INTEGER DEFAULT 1")
        conn.commit()
        print("Столбец добавлен!")
    
    # Обновляем все существующие файлы, чтобы они были активными
    conn.execute("UPDATE user_files SET is_active = 1 WHERE is_active IS NULL")
    conn.commit()
    print("Все файлы отмечены как активные!")
    
    print("Проверка users...")
    cursor = conn.execute("PRAGMA table_info(users)")
    columns = [row[1] for row in cursor.fetchall()]
    print(f"Столбцы в users: {columns}")
    
    for col in ["what_seek", "what_give", "is_premium", "premium_until", "rating"]:
        if col not in columns:
            print(f"Добавляем столбец {col}...")
            if col in ["is_premium", "rating"]:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT 0")
            else:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
            conn.commit()
            print(f"Столбец {col} добавлен!")

print("Готово!")
