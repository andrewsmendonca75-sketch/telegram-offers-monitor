import os
import json
import asyncio
from telethon import TelegramClient, events
from dotenv import load_dotenv
import requests

# Carrega variáveis do .env
load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
PHONE = os.getenv("TELEGRAM_PHONE")

# Carrega config.json
with open("config.json", "r") as f:
    cfg = json.load(f)

CHANNELS = cfg["channels"]
PRODUCTS = [p.lower() for p in cfg["products"]]
USER_CHAT_ID = cfg["user_chat_id"]

client = TelegramClient("telethon_realtime_session", API_ID, API_HASH)

async def main():
    await client.start(phone=PHONE)
    print("✅ Telethon conectado. Monitorando canais:", ", ".join(CHANNELS))

    @client.on(events.NewMessage(chats=CHANNELS))
    async def handler(event):
        msg = event.raw_text.lower()
        for product in PRODUCTS:
            if product in msg:
                print(f"[DETECTED] {product.upper()} em {event.chat.title if event.chat else 'canal desconhecido'}")
                try:
                    requests.get(
                        f"https://api.telegram.org/bot{os.getenv('TELEGRAM_TOKEN')}/sendMessage",
                        params={
                            "chat_id": USER_CHAT_ID,
                            "text": f"🔥 Oferta detectada: {event.message.message[:4000]}"
                        }
                    )
                    print(f"[SENT] Mensagem enviada para {USER_CHAT_ID}")
                except Exception as e:
                    print(f"[ERROR] Falha ao enviar alerta: {e}")
                break

    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
