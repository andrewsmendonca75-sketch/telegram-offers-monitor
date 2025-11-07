# realtime.py — Telethon realtime monitor (sem cooldown) + regras avançadas
from __future__ import annotations
import os, json, re, asyncio, time
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telethon import TelegramClient, events, functions, types
import requests

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
PHONE = os.getenv("TELEGRAM_PHONE")

if not BOT_TOKEN or not API_ID or not API_HASH or not PHONE:
    raise SystemExit("Defina TELEGRAM_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE no .env")

# Carrega config.json
with open("config.json", "r", encoding="utf-8") as f:
    cfg = json.load(f)

CHANNELS = cfg.get("channels", [])
YOU_ID = int(cfg.get("user_chat_id"))

# ======== Utils ========
PRICE_REGEX = re.compile(r"R\$\s?[\d\.\,]+|\b\d{1,3}(?:\.\d{3})*(?:,\d{2})\b|\b\d{3,6}\b", re.IGNORECASE)
WATT_REGEX = re.compile(r"(\d{3,4})\s*W\b", re.IGNORECASE)

def normalize_price(s: str):
    if not s: return None
    s2 = s.replace(" ", "")
    s2 = re.sub(r"(?i)R\$", "", s2)
    s2 = s2.replace(".", "").replace(",", ".")
    s2 = re.sub(r"[^0-9\.]", "", s2)
    try:
        return float(s2)
    except:
        return None

def extract_prices(text: str):
    prices = []
    for m in PRICE_REGEX.findall(text or ""):
        v = normalize_price(m)
        if v is not None:
            prices.append(v)
    return prices

def send_alert(text: str):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": YOU_ID, "text": text}
        r = requests.post(url, data=data, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"! Falha ao enviar alerta: {e}")

def text_contains_any(text: str, words):
    low = (text or "").lower()
    return any(w.lower() in low for w in words)

def text_not_contains_any(text: str, words):
    low = (text or "").lower()
    return not any(w.lower() in low for w in words)

def is_ps5_console(text: str):
    low = (text or "").lower()
    if "ps5" not in low:
        return False
    console_terms = ["console", "video game", "videogame", "playstation 5", "ps5 slim", "digital edition", "edição digital"]
    exclude = ["jogo", "mídia", "midia", "headset", "fone", "fone de ouvido", "dock", "carregador", "base", "capa", "case", "controle", "dualsense"]
    return any(t in low for t in console_terms) and all(x not in low for x in exclude)

def is_ssd_1tb(text: str):
    low = (text or "").lower()
    return ("ssd" in low or "nvme" in low) and ("1tb" in low or "1 tb" in low)

def is_wc_240(text: str):
    low = (text or "").lower()
    return ("water cooler" in low or "watercooler" in low or "wc" in low) and ("240" in low and "mm" in low)

def fonte_600w_plus_ok(text: str):
    low = (text or "").lower()
    w = None
    m = WATT_REGEX.search(low)
    if m:
        try: w = int(m.group(1))
        except: w = None
    eff_ok = any(x in low for x in ["80 plus bronze","80+ bronze","80plus bronze","80 plus gold","80+ gold","80plus gold"])
    return (w is not None and w >= 600) and eff_ok, w

def is_ram_8_or_16(text: str):
    low = (text or "").lower()
    if not ("ddr4" in low or "ddr5" in low or "memória" in low or "memoria" in low or "ram" in low):
        return None
    # detect size
    size = None
    if re.search(r"\b8\s*gb\b", low): size = 8
    elif re.search(r"\b16\s*gb\b", low): size = 16
    return size

def is_rx7600(text: str):
    # Evitar RX 7600 XT
    return re.search(r"\brx\s*7600\b(?!\s*xt)", (text or "").lower()) is not None

def match_config_products(text: str, price_min: float):
    alerts = []
    low = (text or "").lower()
    for p in cfg.get("products", []):
        aliases = [p.get("name","")] + p.get("aliases", [])
        if any(a and a.lower() in low for a in aliases):
            maxp = float(p.get("max_price") or 0)
            if maxp > 0 and price_min is not None and price_min <= maxp:
                alerts.append(f"{p.get('name','(produto)')} <= R$ {maxp:.2f} | encontrado por R$ {price_min:.2f}")
    return alerts

# ======== Main ========
async def main():
    client = TelegramClient("telethon_realtime_session", int(API_ID), API_HASH)
    await client.start(phone=PHONE)

    # Resolver entidades
    entities = []
    for ch in CHANNELS:
        username = ch if ch.startswith("@") else f"@{ch}"
        try:
            e = await client.get_entity(username)
            entities.append(e)
        except Exception as e:
            print(f"! Falha ao resolver {username}: {e}")

    names = [getattr(e, 'username', None) for e in entities]
    names = [f"@{n}" for n in names if n]
    print("✅ Telethon conectado. Monitorando (tempo real):", ", ".join(names))

    @client.on(events.NewMessage(chats=entities))
    async def handler(event):
        chat = await event.get_chat()
        src = f"@{getattr(chat,'username', '')}" if getattr(chat,'username', None) else f"ID:{chat.id}"
        text = event.raw_text or ""
        prices = extract_prices(text)
        price_min = min(prices) if prices else None

        to_send = []

        # 1) Produtos do config.json (simples)
        if price_min is not None:
            to_send += match_config_products(text, price_min)

        # 2) Regras avançadas
        if price_min is not None:
            # SSD 1TB <= 450
            if is_ssd_1tb(text) and price_min <= 450:
                to_send.append(f"SSD NVMe 1TB <= R$ 450 | encontrado por R$ {price_min:.2f}")
            # Water Cooler 240mm <= 180
            if is_wc_240(text) and price_min <= 180:
                to_send.append(f"Water Cooler 240mm <= R$ 180 | encontrado por R$ {price_min:.2f}")
            # Fonte 600W+ Bronze/Gold <= 320
            ok, watts = fonte_600w_plus_ok(text)
            if ok and price_min <= 320:
                to_send.append(f"Fonte {watts}W 80 Plus (Bronze/Gold) <= R$ 320 | encontrado por R$ {price_min:.2f}")
            # RX 7600 (qualquer preço, se houver preço)
            if is_rx7600(text):
                to_send.append(f"Placa de vídeo RX 7600 | preço encontrado: R$ {price_min:.2f}")
            # RAM 8GB/16GB (qualquer preço, se houver preço)
            size = is_ram_8_or_16(text)
            if size in (8,16):
                tipo = "DDR5" if "ddr5" in text.lower() else ("DDR4" if "ddr4" in text.lower() else "RAM")
                to_send.append(f"Memória {tipo} {size}GB | preço encontrado: R$ {price_min:.2f}")

        # PS5 console (console apenas)
        if is_ps5_console(text) and price_min is not None:
            to_send.append(f"Console PS5 | preço encontrado: R$ {price_min:.2f}")

        if to_send:
            alert = f"🔥 Alerta em {src}\n\n" + "\n".join(f"• {line}" for line in to_send) + "\n\n" + text
            send_alert(alert)

    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
