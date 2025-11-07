# realtime.py (com StringSession + novos produtos)
import os, json, re, asyncio, logging, requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
SESSION = os.getenv("TELEGRAM_STRING_SESSION", "")
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
USER_CHAT_ID = os.getenv("USER_CHAT_ID", "")
if not (API_ID and API_HASH and SESSION):
    raise SystemExit("⚠️ Faltam variáveis no ambiente (API_ID, API_HASH ou SESSION).")

with open("config.json", "r", encoding="utf-8") as f:
    cfg = json.load(f)

CHANNELS = cfg.get("channels", [])
USER_CHAT_ID = cfg.get("user_chat_id", USER_CHAT_ID)

client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

def send_alert(text):
    if BOT_TOKEN and USER_CHAT_ID:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": USER_CHAT_ID, "text": text, "parse_mode": "HTML"})

PRODUCTS = [
    ("ps5", 0), ("controle ps5", 0), ("dualsense", 0),
    ("memoria ram 8gb", 0), ("memoria ram 16gb", 0),
    ("rx 7600", 0), ("ryzen 7 5700x", 0), ("ryzen 7 5700", 0),
    ("water cooler 240mm", 180), ("ssd 1tb", 450),
    ("fonte 600w bronze", 320), ("fonte 600w gold", 320),
]

def match_product(msg):
    msg_lower = msg.lower()
    for prod, price_limit in PRODUCTS:
        if prod in msg_lower:
            m = re.search(r"r\$\s?(\d+[.,]?\d*)", msg_lower)
            if not m:
                return prod, None
            price = float(m.group(1).replace('.', '').replace(',', '.'))
            if price_limit == 0 or price <= price_limit:
                return prod, price
    return None, None

@client.on(events.NewMessage(chats=CHANNELS))
async def handler(event):
    msg = event.raw_text or ""
    prod, price = match_product(msg)
    if prod:
        canal = getattr(event.chat, "username", "desconhecido")
        alert = f"🔥 Alerta em @{canal}\n\n• {prod.title()} | preço encontrado: R$ {price or '???'}\n\n{msg[:1500]}"
        logging.info(alert)
        send_alert(alert)

async def main():
    await client.start()
    me = await client.get_me()
    logging.info(f"✅ Logado como {me.first_name} ({me.username}) — monitorando {len(CHANNELS)} canais...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
