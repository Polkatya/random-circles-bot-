from telethon.sync import TelegramClient

api_id = 34389639
api_hash = "f2a76ed97c42872789897d20ca700510"

# Удалите старый sessions/1.session, если раньше запускали скрипт без start() — он «пустой».
with TelegramClient("sessions/1", api_id, api_hash) as client:
    client.start()
    me = client.get_me()
    print("SESSION OK:", me.phone, getattr(me, "username", None) or me.id)