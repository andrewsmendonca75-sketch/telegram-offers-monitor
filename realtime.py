# realtime.py — Telethon em tempo real (cooldown por produto+marca+fonte, ignora se preço cair ou variar ≥5%)
import os, json, re, asyncio
from datetime import datetime, timezone
from dotenv import load_dotenv
from telethon import TelegramClient, events
import requests

load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
PHONE = os.getenv("TELEGRAM_PHONE")

if not (BOT_TOKEN and API_ID and API_HASH and PHONE):
    raise SystemExit("Defina TELEGRAM_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH e TELEGRAM_PHONE no .env")

with open("config.json", "r", encoding="utf-8") as f:
    cfg = json.load(f)

CHANNELS = [c if c.startswith("@") else "@"+c for c in cfg.get("channels", [])]
PRODUCTS = cfg.get("products", [])
YOU_ID = cfg.get("user_chat_id")

PRICE_REGEX = re.compile(r"R\$\s?[\d\.\,]+|\b\d{1,3}(?:\.\d{3})*(?:,\d{2})\b|\b\d{3,6}\b", re.I)

def normalize_price(s: str):
    if not s: return None
    s2 = s.replace(" ", "")
    s2 = re.sub(r"(?i)R\$", "", s2)
    s2 = s2.replace(".", "").replace(",", ".")
    s2 = re.sub(r"[^0-9\.]", "", s2)
    try: return float(s2)
    except: return None

def text_contains_product(text: str, product: dict):
    low = (text or "").lower()
    if not low: return False
    if product.get("name") and product["name"].lower() in low: return True
    for a in product.get("aliases", []) or []:
        if a and a.lower() in low: return True
    return False

def send_alert(text: str):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": YOU_ID, "text": text}, timeout=60
        )
        r.raise_for_status()
    except Exception as e:
        print("! Falha ao enviar alerta:", e)

last_alert = {}   # chave: (product_name, brand, src) -> { 'ts': int, 'price': float }
COOLDOWN = 30*60  # 30 min
BRANDS = ['inno3d','galax','asus','msi','gigabyte','aorus','zotac','pny','colorful','gainward','palit','evga','xfx','powercolor']

def detect_brand(text: str):
    low = (text or '').lower()
    for b in BRANDS:
        if b in low:
            return b
    return 'unknown'

def maybe_alert(src_username: str, text: str):
    if not text: return
    prices = [normalize_price(m) for m in PRICE_REGEX.findall(text)]
    prices = [p for p in prices if p is not None]
    if not prices: return
    min_price = min(prices)

    for p in PRODUCTS:
        if not text_contains_product(text, p):
            continue
        if min_price <= float(p.get("max_price", 0)):
            brand = detect_brand(text)
            key = (p.get("name","").lower(), brand, src_username)
            now = int(datetime.now(tz=timezone.utc).timestamp())

            prev = last_alert.get(key, {'ts': 0, 'price': float('inf')})
            prev_ts = prev['ts']
            prev_price = prev['price']

            price_dropped = (min_price < prev_price - 0.01)
            PCT_DELTA = 0.05  # 5%
            price_moved_enough = abs(min_price - prev_price) >= (prev_price * PCT_DELTA) if prev_price != float('inf') else True
            brand_first_seen = (prev_ts == 0)

            if (not price_dropped) and (not brand_first_seen) and (not price_moved_enough) and (now - prev_ts < COOLDOWN):
                return

            alert = "\n".join([
                f"🔥 ALERTA (tempo real): {str(p.get('name','')).upper()} <= R$ {p.get('max_price')}",
                f"Preço encontrado: R$ {min_price}",
                (f"Marca detectada: {brand}" if brand != 'unknown' else ""),
                f"Canal: {src_username}",
                "",
                text
            ]).strip()
            send_alert(alert)
            last_alert[key] = {'ts': now, 'price': min_price}
            print(f"⚡ Enviado: {p.get('name')} [{brand}] | R${min_price} | {src_username}")

async def main():
    client = TelegramClient("telethon_realtime_session", API_ID, API_HASH)
    await client.start(phone=PHONE)
    print("✅ Telethon conectado. Monitorando (tempo real):", ", ".join(CHANNELS))

    entities = []
    for ch in CHANNELS:
        try:
            e = await client.get_entity(ch)
            entities.append(e)
        except Exception as e:
            print(f"! Falha ao resolver {ch}: {e}")

    @client.on(events.NewMessage(chats=entities))
    async def handler(event):
        chat = await event.get_chat()
        username = getattr(chat, "username", None)
        src = f"@{username}" if username else f"ID:{chat.id}"
        text = event.raw_text or ""
        print("📥 novo post:", src, "|", text[:160])
        maybe_alert(src, text)

    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
