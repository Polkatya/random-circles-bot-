import asyncio
from telethon.sync import TelegramClient

API_ID = 34389639
API_HASH = "f2a76ed97c42872789897d20ca700510"

async def create_session():
    print("Создание новой сессии...")
    
    # Создаем клиент
    client = TelegramClient('./sessions/1', API_ID, API_HASH)
    
    try:
        # Подключаемся
        await client.connect()
        
        # Если не авторизованы, запрашиваем код
        if not await client.is_user_authorized():
            print("Введите номер телефона:")
            phone = input()
            
            # Отправляем код
            await client.send_code_request(phone)
            
            print("Введите код из Telegram:")
            code = input()
            
            # Авторизуемся
            await client.sign_in(phone, code)
            
        print("Сессия успешно создана!")
        
    except Exception as e:
        print(f"Ошибка: {e}")
    
    finally:
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(create_session())
