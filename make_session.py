# make_session.py
# Gera um StringSession para usar no Render (sem arquivo .session).

from telethon.sessions import StringSession
from telethon.sync import TelegramClient
import os

api_id = input("TELEGRAM_API_ID: ").strip()
api_hash = input("TELEGRAM_API_HASH: ").strip()
phone = input("TELEGRAM_PHONE (+55...): ").strip()

with TelegramClient(StringSession(), int(api_id), api_hash) as client:
    client.start(phone=phone)
    print("\n=== STRING SESSION GERADA ===")
    print(client.session.save())
    print("\nAdicione essa string como TELEGRAM_STRING_SESSION no Render.")
