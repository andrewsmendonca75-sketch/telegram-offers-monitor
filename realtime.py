# realtime.py
import os
import re
import json
import asyncio
import logging
from datetime import datetime

import requests
from dotenv import load_dotenv
from telethon import TelegramClient, events, functions
from telethon.sessions import StringSession

# ---------------------------------------
# LOGGING
# ---------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("monitor")

# ---------------------------------------
# ENV / CONFIG
# ---------------------------------------
load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
STRING_SESSION = os.getenv("TELEGRAM_STRING_SESSION", "")
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
USER_CHAT_ID = int(os.getenv("USER_CHAT_ID", os.getenv("TELEGRAM_USER_CHAT_ID", "0")))

# ---------------------------------------
# CANAIS MONITORADOS
# ---------------------------------------
CHANNELS = [
    "@canalandrwss",
    "@pcdorafa",
    "@ofertaskabum",
    "@terabyteshopoficial",
    "@pichauofertas",
    "@sohardwaredorocha",
    "@soplacadevideo",
    "@chinasuperofertas",
    "@compramosnachin",
]

# ---------------------------------------
# PALAVRAS-CHAVE
# ---------------------------------------
KEYWORDS = [
    # Consoles
    "ps5", "dualsense", "controle ps5",

    # Memórias
    "memoria ram 8gb", "memória ram 8gb", "ram 8gb",
    "memoria ram 16gb", "memória ram 16gb", "ram 16gb",

    # Placas de vídeo e processadores
    "rx 7600",
    "ryzen 7 5700x", "ryzen 7 5700",
    "rtx 5060",

    # Armazenamento
    "ssd 1tb", "ssd nvme 1tb",

    # Coolers e fontes
    "water cooler 240mm",
    "fonte 600w", "fonte 650w",

    # Periféricos
    "redragon kumara", "kumara", "k552",
    "teclado kumara", "teclado redragon kumara",
    "teclado redragon k552", "teclado mecanico kumara",
    "teclado mecânico kumara", "teclado mecanico redragon",
]

# ---------------------------------------
# LIMITES DE PREÇO (alerta especial)
# ---------------------------------------
LIMITS = {
    "ssd 1tb": 450.0,
    "ssd nvme 1tb": 450.0,
    "water cooler 240mm": 180.0,
    "fonte 600w": 320.0,
    "fonte 650w": 320.0,
}

# ---------------------------------------
# PARSER DE PREÇO (corrigido)
# ---------------------------------------
PRICE_PAT = re.compile(
    r"(?:R\$\s*)?(\d{1,3}(?:[\.\s]\d{3})*(?:,\d{2})|\d+(?:[.,]\d{2})?)",
    flags=re.IGNORECASE
)

def to_float_brl(txt: str) -> float:
    t = txt.strip().replace(" ", "")
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return -1.0

def find_price(text: str) -> float:
    matches = PRICE_PAT.findall(text.replace("\n", " "))
    if not matches:
        return -1.0
    prices = [to_float_brl(m) for m in matches if to_float_brl(m) > 0]
    if not prices:
        return -1.0
    return min(prices)

# ---------------------------------------
# FILTROS
# ---------------------------------------
def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower())

def match_keyword(text: str) -> str:
    t = normalize(text)
    for kw in KEYWORDS:
        if kw in t:
            return kw
    return ""

def extra_constraints_ok(kw: str, text: str) -> bool:
    t = normalize(text)
    if "ps5" in kw:
        if any(x in t for x in ["headset", "fone", "jogo", "game", "midia"]):
            return False
    return True

# ---------------------------------------
# ENVIO DE ALERTAS
# ---------------------------------------
def send_via_bot(alert_text: str) -> bool:
    if not BOT_TOKEN or not USER_CHAT_ID:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": USER_CHAT_ID, "text": alert_text},
            timeout=10
        )
        if resp.ok:
            log.info(f"📨 Bot API -> {resp.status_code} OK")
            return True
        else:
            log.error(f"❌ Bot API falhou -> {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        log.exception(f"❌ Bot API exception: {e}")
        return False

async def send_fallback_telethon(client: TelegramClient, alert_text: str):
    try:
        await client.send_message("me", f"[fallback] {alert_text}")
        log.info("🧷 Fallback -> enviado para mensagens salvas.")
    except Exception as e:
        log.exception(f"❌ Fallback Telethon falhou: {e}")

async def send_alert(client: TelegramClient, title: str, price: float, raw: str, channel: str):
    msg = (
        f"🔥 Alerta em {channel}\n\n"
        f"• {title.title()} | preço encontrado: R$ {price:.2f}\n\n"
        f"{raw.strip()}"
    )
    ok = send_via_bot(msg)
    if not ok:
        await send_fallback_telethon(client, msg)

# ---------------------------------------
# MAIN
# ---------------------------------------
async def main():
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        log.error("❌ Sessão inválida. Refaça o make_session.py.")
        return

    try:
        await client(functions.help.GetConfigRequest())
        log.info("✅ Conectado ao Telegram; iniciando monitoramento…")
    except Exception:
        log.info("✅ Conectado; monitorando canais…")

    for ch in CHANNELS:
        try:
            await client.get_entity(ch)
        except Exception as e:
            log.warning(f"⚠️ Não foi possível resolver canal {ch}: {e}")

    log.info(f"✅ Logado — monitorando {len(CHANNELS)} canais...")

    @client.on(events.NewMessage(chats=CHANNELS))
    async def handler(event):
        try:
            text = event.raw_text or ""
            ch = getattr(event.chat, 'username', None)
            channel_ref = f"@{ch}" if ch else "canal"

            kw = match_keyword(text)
            if not kw:
                return
            if not extra_constraints_ok(kw, text):
                log.info(f"Ignorado (restrição adicional): {kw} em {channel_ref}")
                return

            price = find_price(text)
            limit = LIMITS.get(kw, None)

            if price > 0:
                log.info(f"Keyword: {kw} | Preço identificado: R$ {price:.2f} (limite: {limit})")
            else:
                log.info(f"Keyword: {kw} | Preço não identificado")

            title = kw.replace("_", " ").title()
            await send_alert(client, title, price if price > 0 else 0.0, text, channel_ref)

        except Exception as e:
            log.exception(f"Erro no handler: {e}")

    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
