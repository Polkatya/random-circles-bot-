import asyncio
import os
import traceback
import sqlite3
from telethon import TelegramClient, events
from twocaptcha import TwoCaptcha

print("MAIN STARTED")

# ================= CONFIG =================

API_ID = 34389639
API_HASH = "f2a76ed97c42872789897d20ca700510"

def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()

async def safe_send(client, chat, message):
    try:
        await client.send_message(chat, message)
    except Exception as e:
        if "FloodWaitError" in str(e):
            # Извлекаем время ожидания из ошибки
            import re
            match = re.search(r'A wait of (\d+) seconds', str(e))
            if match:
                wait_time = int(match.group(1))
                print(f"[!] Flood Wait: {wait_time}s for {chat}. Waiting...")
                await asyncio.sleep(wait_time)
                await client.send_message(chat, message)
            else:
                print(f"[!] Unknown FloodWait error: {e}")
        else:
            print(f"[!] Error sending to {chat}: {e}")

# Словарь для капчи с кнопками (слово -> список возможных эмодзи)
EMOJI_MAP = {
    "цветок": ["🌸", "🌼", "🌻", "🌺", "💐", "🌹", "🌷"],
    "собака": ["🐶", "🐕", "🐩", "🦮", "🐕‍🦺"],
    "кошка": ["🐱", "🐈", "😸", "😺", "😻"],
    "кот": ["🐱", "🐈", "😸", "😺", "😻"],
    "котенок": ["🐱", "🐈"],
    "звезда": ["⭐", "🌟", "✨"],
    "яблоко": ["🍎", "🍏"],
    "мяч": ["⚽", "🏀", "🏈", "🎾", "🏐", "⚾"],
    "сердце": ["❤️", "💖", "💗", "💓", "💙", "💚", "💛", "💜", "🧡"],
    "машина": ["🚗", "🚕", "🚙", "🚌", "🏎️"],
    "дом": ["🏠", "🏡", "🏢", "🏘️"],
    "солнце": ["☀️", "🌞"],
    "луна": ["🌙", "🌛", "🌜", "🌑", "🌕"],
    "слон": ["🐘", "🦣"],
    "клубника": ["🍓"],
    "вишня": ["🍒"],
    "лимон": ["🍋"],
    "зонт": ["☂️", "🌂", "☔"],
    "часы": ["⌚", "⏰", "⏱️", "⏲️", "🕒"],
    "самолет": ["✈️", "🛩️"],
    "медведь": ["🐻", "🧸"],
    "волк": ["🐺"],
    "лис": ["🦊"],
    "заяц": ["🐰", "🐇"],
    "рыбка": ["🐟", "🐠", "🐡", "🎣"],
    "птица": ["🐦", "🐧", "🕊️", "🦅", "🦆", "🦉", "🦩", "🦚", "🦜"],
    "гриб": ["🍄"],
    "дерево": ["🌳", "🌲", "🌴", "🌵"],
    "тучка": ["☁️", "🌧️", "⛈️", "🌥️"],
    "радуга": ["🌈"],
    "снег": ["❄️", "☃️", "⛄"],
    "лед": ["🥶", "🧊"],
    "огонь": ["🔥", "💥"],
    "вода": ["💧", "🌊", "⛲"],
    "песок": ["🌀", "🏖️", "⏳"],
    "трава": ["🌿", "🌱", "🍃"],
    "цветы": ["💐", "🌸", "🌹"],
    "листья": ["🌱", "🍃", "🌿"],
    "лев": ["🦁"],
    "банан": ["🍌"],
    "арбуз": ["🍉"],
    "орел": ["🦅"],
    "фрукт": ["🍏", "🍎", "🍐", "🍊", "🍋", "🍌", "🍉", "🍇", "🍓", "🫐", "🍈", "🍒", "🍑", "🥭", "🍍", "🥥", "🥝"],
    "овощ": ["🍅", "🍆", "🥑", "🥦", "🥬", "🥒", "🌶️", "🌽", "🥕", "🫒", "🧄", "🧅", "🥔", "🍠"],
    "насекомое": ["🐝", "🐛", "🦋", "🐌", "🐞", "🐜", "🦗", "🕷️"]
}

def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()

# Функция для починки "сломанных" сессий
def fix_session_db(path):
    try:
        conn = sqlite3.connect(path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(sessions)")
        cols = cursor.fetchall()
        num_cols = len(cols)
        
        # Если колонок не 6 (новая версия Telethon), чиним
        if num_cols != 6:
            print(f"[FIX] Repairing session file: {path} (Columns found: {num_cols})")
            cursor.execute("ALTER TABLE sessions RENAME TO sessions_old")
            # Создаем таблицу с 6 колонками (новый стандарт Telethon)
            cursor.execute("""
                CREATE TABLE sessions (
                    dc_id INTEGER PRIMARY KEY,
                    server_address TEXT,
                    port INTEGER,
                    auth_key BLOB,
                    takeout_id INTEGER,
                    tmp_auth_key BLOB
                )
            """)
            
            # Переносим данные
            if num_cols == 5:
                cursor.execute("INSERT INTO sessions (dc_id, server_address, port, auth_key, takeout_id, tmp_auth_key) SELECT dc_id, server_address, port, auth_key, takeout_id, NULL FROM sessions_old")
            elif num_cols > 6:
                cursor.execute("INSERT INTO sessions SELECT * FROM sessions_old")
            else:
                cursor.execute("INSERT INTO sessions (dc_id, server_address, port, auth_key, takeout_id, tmp_auth_key) SELECT dc_id, server_address, port, auth_key, takeout_id, NULL FROM sessions_old")
            
            cursor.execute("DROP TABLE sessions_old")
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"[FIX ERROR] {e}")

captcha_api = read_file("./templates/captcha_api.txt")
spam_text = read_file("./templates/spam_text.txt")

solver = TwoCaptcha(captcha_api)

# ================= SESSIONS =================

os.makedirs("./sessions", exist_ok=True)
sessions = [s for s in os.listdir("./sessions") if s.endswith(".session")]

import re

def normalize_text(text):
    if not text: return ""
    # Таблица замены для стилизованных букв (математические символы и др.)
    # Покрывает жирный, курсив и другие "шрифты" из Unicode
    styled_chars = {
        # Mathematical bold
        '𝐚': 'а', '𝐛': 'б', '𝐜': 'с', '𝐝': 'д', '𝐞': 'е', '𝐟': 'ф', '𝐠': 'г', '𝐡': 'х', '𝐢': 'и', '𝐣': 'й', '𝐤': 'к', '𝐥': 'л', '𝐦': 'м', '𝐧': 'н', '𝐨': 'о', '𝐩': 'п', '𝐪': 'к', '𝐫': 'р', '𝐬': 'с', '𝐭': 'т', '𝐮': 'у', '𝐯': 'в', '𝐰': 'в', '𝐱': 'х', '𝐲': 'у', '𝐳': 'з',
        '𝐀': 'а', '𝐁': 'б', '𝐂': 'с', '𝐃': 'д', '𝐄': 'е', '𝐅': 'ф', '𝐆': 'г', '𝐇': 'х', '𝐈': 'и', '𝐉': 'й', '𝐊': 'к', '𝐋': 'л', '𝐌': 'м', '𝐍': 'н', '𝐎': 'о', '𝐏': 'п', '𝐐': 'к', '𝐑': 'р', '𝐒': 'с', '𝐓': 'т', '𝐔': 'у', '𝐕': 'в', '𝐖': 'в', '𝐗': 'х', '𝐘': 'у', '𝐙': 'з',
        # Mathematical italic
        '𝑎': 'а', '𝑏': 'б', '𝑐': 'с', '𝑑': 'д', '𝑒': 'е', '𝑓': 'ф', '𝑔': 'г', '𝐡': 'х', '𝑖': 'и', '𝑗': 'й', '𝑘': 'к', '𝑙': 'л', '𝑚': 'м', '𝑛': 'н', '𝑜': 'о', '𝑝': 'п', '𝑞': 'к', '𝑟': 'р', '𝑠': 'с', '𝑡': 'т', '𝑢': 'у', '𝑣': 'в', '𝑤': 'в', '𝑥': 'х', '𝑦': 'у', '𝑧': 'з',
        '𝐴': 'а', '𝐵': 'б', '𝐶': 'с', '𝐷': 'д', '𝐸': 'е', '𝐹': 'ф', '𝐺': 'г', '𝐻': 'х', '𝐼': 'и', '𝐽': 'й', '𝐾': 'к', '𝐿': 'л', '𝑀': 'м', '𝑁': 'н', '𝑂': 'о', '𝑃': 'п', '𝑄': 'к', '𝑅': 'р', '𝑆': 'с', '𝑇': 'т', '𝑈': 'у', '𝑉': 'в', '𝑊': 'в', '𝑋': 'х', '𝑌': 'у', '𝑍': 'з',
        # Mathematical sans-serif italic
        '𝖺': 'а', '𝖻': 'б', '𝖼': 'с', '𝖽': 'д', '𝖾': 'е', '𝖿': 'ф', '𝗀': 'г', '𝗁': 'х', '𝗂': 'и', '𝗃': 'й', '𝗄': 'к', '𝗅': 'л', '𝗆': 'м', '𝗇': 'н', '𝗈': 'о', '𝗉': 'п', '𝗊': 'к', '𝗋': 'р', '𝗌': 'с', '𝗍': 'т', '𝗎': 'у', '𝗏': 'в', '𝗐': 'в', '𝗑': 'х', '𝗒': 'у', '𝗓': 'з',
        '𝖠': 'а', '𝖡': 'б', '𝖢': 'с', '𝖣': 'д', '𝖤': 'е', '𝖥': 'ф', '𝖦': 'г', '𝖧': 'х', '𝖨': 'и', '𝖩': 'й', '𝖪': 'к', '𝖫': 'л', '𝖬': 'м', '𝖭': 'н', '𝖮': 'о', '𝖯': 'п', '𝖰': 'к', '𝖱': 'р', '𝖲': 'с', '𝖳': 'т', '𝖴': 'у', '𝖵': 'в', '𝖶': 'в', '𝖷': 'х', '𝖸': 'у', '𝖹': 'з',
        # Regular Cyrillic
        'а': 'а', 'б': 'б', 'в': 'в', 'г': 'г', 'д': 'д', 'е': 'е', 'ё': 'е', 'ж': 'ж', 'з': 'з', 'и': 'и', 'й': 'й', 'к': 'к', 'л': 'л', 'м': 'м', 'н': 'н', 'о': 'о', 'п': 'п', 'р': 'р', 'с': 'с', 'т': 'т', 'у': 'у', 'ф': 'ф', 'х': 'х', 'ц': 'ц', 'ч': 'ч', 'ш': 'ш', 'щ': 'щ', 'ъ': '', 'ы': 'ы', 'ь': '', 'э': 'э', 'ю': 'ю', 'я': 'я'
    }
    
    # Для латиницы (если бот пишет задания латиницей)
    latin_to_cyrillic = {'cat': 'кот', 'dog': 'собака', 'flower': 'цветок', 'star': 'звезда', 'apple': 'яблоко', 'ball': 'мяч', 'heart': 'сердце', 'car': 'машина', 'house': 'дом', 'sun': 'солнце', 'moon': 'луна', 'elephant': 'слон'}
    
    res = ""
    for char in text.lower():
        res += styled_chars.get(char, char)
    
    # Очистка от всего кроме букв
    res = re.sub(r'[^а-яa-z\s]', '', res)
    
    # Перевод латиницы в кириллицу для поиска по EMOJI_MAP
    for lat, cyr in latin_to_cyrillic.items():
        if lat in res:
            res += f" {cyr}"
            
    return res

async def solve_button_captcha(event):
    raw_text = (event.raw_text or "").lower()
    
    # Ищем ключевые слова капчи
    captcha_keywords = ["проверку на робота", "нажми на кнопку", "изображен", "нажми", "робота", "кнопку"]
    
    if any(word in raw_text for word in captcha_keywords):
        print(f"[CAPTCHA DEBUG] Raw message: '{raw_text}'")
        print(f"[CAPTCHA DEBUG] Keywords found: {[word for word in captcha_keywords if word in raw_text]}")
        
        clean_text = normalize_text(raw_text)
        print(f"[CAPTCHA DEBUG] Normalized text: '{clean_text}'")
        
        # Ищем ключевые слова в EMOJI_MAP
        target_emojis = []
        found_word = ""
        for word, emojis in EMOJI_MAP.items():
            if word in clean_text:
                target_emojis = emojis
                found_word = word
                print(f"[CAPTCHA] Target found in clean text: '{word}' -> {emojis}")
                break
            elif word in raw_text:
                target_emojis = emojis
                found_word = word
                print(f"[CAPTCHA] Target found in raw text: '{word}' -> {emojis}")
                break
        
        if not target_emojis:
            print(f"[CAPTCHA] ERROR: Could not identify target emoji!")
            print(f"[CAPTCHA] Available words: {list(EMOJI_MAP.keys())[:10]}...")  # Показываем первые 10 слов
            return False

        if event.reply_markup:
            print(f"[CAPTCHA] Found {len(event.reply_markup.rows)} button rows")
            all_buttons = []
            for row_idx, row in enumerate(event.reply_markup.rows):
                for btn_idx, button in enumerate(row.buttons):
                    btn_text = (button.text or "").strip()
                    clean_btn = btn_text.replace("\ufe0f", "")
                    all_buttons.append(clean_btn)
                    print(f"[CAPTCHA] Button[{row_idx},{btn_idx}]: '{clean_btn}'")
                    
                    for emoji in target_emojis:
                        clean_target = emoji.replace("\ufe0f", "")
                        if clean_target in clean_btn or clean_btn in clean_target:
                            print(f"[CAPTCHA] MATCH! Clicking: {btn_text}")
                            try:
                                # Используем data если есть, иначе текст
                                if hasattr(button, 'data') and button.data:
                                    await event.click(data=button.data)
                                else:
                                    await event.click(text=btn_text)
                                print(f"[CAPTCHA] SUCCESS: Button clicked!")
                                return True
                            except Exception as e:
                                print(f"[CAPTCHA CLICK ERROR] {e}")
            
            print(f"[CAPTCHA] ERROR: No matching button found!")
            print(f"[CAPTCHA] Looking for emojis: {target_emojis}")
            print(f"[CAPTCHA] Available buttons: {all_buttons}")
        else:
            print("[CAPTCHA] ERROR: No reply_markup/buttons found!")
    else:
        # Обрабатываем сообщения без капчи
        if len(raw_text) > 20:  # Длина сообщения больше 20 символов
            print(f"[MESSAGE] Not captcha: '{raw_text[:50]}...'")  # Показываем первые 50 символов
    
    return False

# ================= BOT =================

async def start_bot(session_file):

    session_name = os.path.join("./sessions", session_file.replace(".session", ""))

    print(f"[~] Starting session: {session_name}")

    client = TelegramClient(session_name, API_ID, API_HASH)

    try:
        await client.connect()

        if not await client.is_user_authorized():
            print(f"[-] NOT AUTHORIZED: {session_file}")
            return

        print(f"[+] CONNECTED: {session_file}")

        # ================= HANDLER 1 =================

        @client.on(events.NewMessage(from_users="AnonRubot"))
        async def handler1(event):
            try:
                # Сначала проверяем капчу с кнопками
                if await solve_button_captcha(event):
                    print("[AnonRubot] Button captcha solved")
                    return

                text = (event.raw_text or "").lower()
                print(f"[AnonRubot] Received: '{text}'")

                if "собеседник найден" in text or "found" in text:
                    print(f"[AnonRubot] Found partner - sending spam")
                    await safe_send(client, "AnonRubot", spam_text)
                    print(f"[AnonRubot] Spam sent: '{spam_text}'")
                    await asyncio.sleep(3)
                    await safe_send(client, "AnonRubot", "/next")
                    print(f"[AnonRubot] Sent /next")

                elif "собеседник закончил" in text or "закончил" in text:
                    print(f"[AnonRubot] Partner left - searching")
                    await safe_send(client, "AnonRubot", "/search")
                    print(f"[AnonRubot] Sent /search")

                elif "исчерпали дневной лимит" in text:
                    print(f"[AnonRubot] DAILY LIMIT REACHED")

                elif "код с картинки" in text and event.media:
                    print(f"[AnonRubot] Got captcha")
                    file = await event.download_media(file="./cap.jpg")

                    try:
                        result = solver.normal(file)
                        await safe_send(client, "AnonRubot", result["code"])
                        print("[CAPTCHA SOLVED]")

                    except Exception as e:
                        print(f"[CAPTCHA ERROR] {e}")

            except Exception:
                print(f"[HANDLER ERROR] AnonRubot")
                print(traceback.format_exc())

        # ================= HANDLER 2 =================

        @client.on(events.NewMessage(chats="anonimnyychatbot"))
        async def handler2(event):
            try:
                # Сначала проверяем капчу с кнопками
                if await solve_button_captcha(event):
                    print("[anonimnyychatbot] Button captcha solved")
                    return

                text = (event.raw_text or "").lower()
                print(f"[anonimnyychatbot] Received: '{text}'")

                # "Нашёл кое-кого для тебя!" или "Found someone for you!"
                # Используем более универсальный поиск
                if "нашёл" in text or "found" in text or "ourël" in text:
                    print(f"[anonimnyychatbot] Found partner - waiting 10s")
                    await asyncio.sleep(10)
                    await safe_send(client, "anonimnyychatbot", spam_text)
                    print(f"[anonimnyychatbot] Spam sent: '{spam_text}'")
                    await asyncio.sleep(3)
                    await safe_send(client, "anonimnyychatbot", "/next")
                    print(f"[anonimnyychatbot] Sent /next")

                elif "dialog ostanovlen" in text or "диалог остановлен" in text:
                    print(f"[anonimnyychatbot] Dialog stopped - restarting")
                    await safe_send(client, "anonimnyychatbot", "/start")
                    print(f"[anonimnyychatbot] Sent /start")

                elif "вы уже в очереди" in text:
                    print(f"[anonimnyychatbot] Already in queue/dialog")
            
            except Exception:
                print(f"[HANDLER ERROR] anonimnyychatbot")
                print(traceback.format_exc())

        # ================= HANDLER 3 =================

        @client.on(events.NewMessage(chats="MessageAnonBot"))
        async def handler3(event):
            try:
                # Сначала проверяем капчу с кнопками
                if await solve_button_captcha(event):
                    print("[MessageAnonBot] Button captcha solved")
                    return

                text = (event.raw_text or "").lower()
                print(f"[MessageAnonBot] Received: '{text}'")

                if any(word in text for word in ["начинай общение", "начинай", "общение", "найден", "found"]):
                    print(f"[MessageAnonBot] Found partner - sending spam")
                    await safe_send(client, "MessageAnonBot", spam_text)
                    print(f"[MessageAnonBot] Spam sent: '{spam_text}'")
                    await asyncio.sleep(3) # Задержка 3 секунды перед /next
                    await safe_send(client, "MessageAnonBot", "/next")
                    print(f"[MessageAnonBot] Sent /next")

                elif any(word in text for word in ["собеседник покинул", "покинул", "закончил", "left", "жми /next"]):
                    print(f"[MessageAnonBot] Partner left/waiting - searching")
                    await safe_send(client, "MessageAnonBot", "/next")
                    print(f"[MessageAnonBot] Sent /next")

            except Exception:
                print(f"[HANDLER ERROR] MessageAnonBot")
                print(traceback.format_exc())

        # ================= HANDLER 5 =================

        @client.on(events.NewMessage(chats="anonimnyi_chat_bot"))
        async def handler5(event):
            try:
                if await solve_button_captcha(event): return
                text = (event.raw_text or "").lower()
                print(f"[anonimnyi_chat_bot] Received: '{text}'")

                if any(word in text for word in ["собеседник найден", "найден", "found", "начинай"]):
                    print(f"[anonimnyi_chat_bot] Found partner - sending spam")
                    await safe_send(client, "anonimnyi_chat_bot", spam_text)
                    print(f"[anonimnyi_chat_bot] Spam sent: '{spam_text}'")
                    await asyncio.sleep(3)
                    await safe_send(client, "anonimnyi_chat_bot", "/next")
                    print(f"[anonimnyi_chat_bot] Sent /next")

                elif any(word in text for word in ["собеседник покинул", "покинул", "закончил", "left", "жми /next"]):
                    print(f"[anonimnyi_chat_bot] Partner left - searching")
                    await safe_send(client, "anonimnyi_chat_bot", "/next")
                    print(f"[anonimnyi_chat_bot] Sent /next")

            except Exception:
                print(f"[HANDLER ERROR] anonimnyi_chat_bot")
                print(traceback.format_exc())

        # ================= HANDLER 6 =================

        @client.on(events.NewMessage(chats="anonimnyi_chatbot"))
        async def handler6(event):
            try:
                if await solve_button_captcha(event): return
                text = (event.raw_text or "").lower()
                print(f"[anonimnyi_chatbot] Received: '{text}'")

                # "Собеседник найден!" или "Напишите что-нибудь"
                if "собеседник найден" in text or "напишите что-нибудь" in text:
                    print(f"[anonimnyi_chatbot] Found partner - sending spam")
                    await safe_send(client, "anonimnyi_chatbot", spam_text)
                    print(f"[anonimnyi_chatbot] Spam sent: '{spam_text}'")
                    await asyncio.sleep(3)
                    await safe_send(client, "anonimnyi_chatbot", "/next")
                    print(f"[anonimnyi_chatbot] Sent /next")

                elif "нажмите /next" in text or "поиск собеседника" in text:
                    # Если бот предлагает нажать /next для старта
                    if "нажмите /next чтобы начать" in text:
                        print(f"[anonimnyi_chatbot] Starting search")
                        await safe_send(client, "anonimnyi_chatbot", "/next")

            except Exception:
                print(f"[HANDLER ERROR] anonimnyi_chatbot")
                print(traceback.format_exc())

        # ================= START =================

        print("[*] BOT STARTED")
        asyncio.create_task(safe_send(client, "anonimnyychatbot", "/start"))
        asyncio.create_task(safe_send(client, "AnonRubot", "/start"))
        asyncio.create_task(safe_send(client, "MessageAnonBot", "/start"))
        asyncio.create_task(safe_send(client, "anonimnyi_chat_bot", "/start"))
        asyncio.create_task(safe_send(client, "anonimnyi_chatbot", "/start"))

        await client.run_until_disconnected()

    except Exception:
        print(f"[FATAL ERROR] {session_file}")
        print(traceback.format_exc())

    finally:
        await client.disconnect()


# ================= MAIN =================

async def main():

    if not sessions:
        print("[-] NO SESSIONS FOUND")
        return

    print(f"[+] Sessions found: {len(sessions)}")

    # Пропускаем починку сессий
    # for session in sessions:
    #     fix_session_db(os.path.join("./sessions", session))

    # Запускаем все сессии одновременно
    tasks = []
    for session in sessions:
        tasks.append(start_bot(session))
    
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
