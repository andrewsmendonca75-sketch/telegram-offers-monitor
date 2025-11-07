# realtime.py
import os
import re
import json
import asyncio
import logging
from datetime import datetime, timezone

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
PHONE = os.getenv("TELEGRAM_PHONE", "")  # se estiver usando StringSession, não é necessário
STRING_SESSION = os.getenv("TELEGRAM_STRING_SESSION", "")
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
USER_CHAT_ID = int(os.getenv("USER_CHAT_ID", os.getenv("TELEGRAM_USER_CHAT_ID", "0")))

CONFIG_PATH = os.getenv("CONFIG_PATH", "config.json")

# ---------------------------------------
# LEITURA DO CONFIG (canais, produtos etc.)
# ---------------------------------------
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    channels = cfg.get("channels", [])
    products = cfg.get("products", [])
    # Prioriza env se quiser forçar pelo Render
    if not USER_CHAT_ID:
        user_id = cfg.get("user_chat_id", 0)
    else:
        user_id = USER_CHAT_ID
    return channels, products, int(user_id)

CHANNELS, PRODUCTS, USER_ID = load_config()

if not BOT_TOKEN:
    log.warning("⚠️ TELEGRAM_TOKEN (BotFather) não definido. Apenas fallback Telethon funcionará.")
if not USER_ID:
    log.warning("⚠️ user_chat_id não definido (nem em env, nem no config.json). Defina para receber via bot.")

# ---------------------------------------
# PARSER DE PREÇO (R$ 1.234,56 | 1234.56 | 1234)
# ---------------------------------------
PRICE_PAT = re.compile(
    r"(?:R\$\s*)?(\d{1,3}(?:[\.\s]\d{3})*(?:,\d{2})|\d+(?:\.\d{2})?)",
    flags=re.IGNORECASE
)

def to_float_brl(txt: str) -> float:
    # normaliza formatos tipo "2.699,99" → 2699.99
    t = txt.strip()
    if "," in t and "." in t:
        # Brasil: ponto milhar, vírgula decimal
        t = t.replace(".", "").replace(",", ".")
    elif "," in t and "." not in t:
        # "2699,99"
        t = t.replace(",", ".")
    try:
        return float(t)
    except Exception:
        return -1.0

def find_price(text: str) -> float:
    m = PRICE_PAT.search(text.replace("\n", " "))
    if not m:
        return -1.0
    return to_float_brl(m.group(1))

# ---------------------------------------
# ENVIO DO ALERTA
# ---------------------------------------
def send_via_bot(alert_text: str) -> bool:
    if not BOT_TOKEN or not USER_ID:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": USER_ID, "text": alert_text},
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
        log.info("🧷 Fallback -> enviado para Saved Messages (me).")
    except Exception as e:
        log.exception(f"❌ Fallback Telethon falhou: {e}")

async def send_alert(client: TelegramClient, title: str, price: float, raw: str, channel: str):
    msg = (
        f"🔥 Alerta em {channel}\n\n"
        f"• {title} | preço encontrado: R$ {price}\n\n"
        f"{raw.strip()}"
    )
    ok = send_via_bot(msg)
    if not ok:
        await send_fallback_telethon(client, msg)

# ---------------------------------------
# DETECÇÃO DE PRODUTOS (exemplo genérico)
# Mantém sua ideia: match por palavras + preço (se houver)
# ---------------------------------------
# Palavras-chave (ajuste conforme sua lista em config.json se quiser)
KEYWORDS = [
    "ps5", "dualsense", "controle ps5",
    "memória ram 8gb", "memoria ram 8gb", "ram 8gb",
    "memória ram 16gb", "memoria ram 16gb", "ram 16gb",
    "rx 7600",
    "ryzen 7 5700x", "ryzen 7 5700",
    "ssd 1tb", "ssd nvme 1tb",
    "water cooler 240mm",
    "fonte 600w", "fonte 650w",
]

# Regras de preço (opcional): se não atender, ainda pode alertar com o preço encontrado.
LIMITS = {
    "ssd 1tb": 450.0,
    "ssd nvme 1tb": 450.0,
    "water cooler 240mm": 180.0,
    # exemplo de fonte ≥600W bronze/gold <= 320 — checagem simples por palavra
    "fonte 600w": 320.0,
    "fonte 650w": 320.0,
}

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
    # Filtrar falsos positivos de "ps5" (headset, jogo)
    if "ps5" in kw:
        if "headset" in t or "fone" in t or "jogo" in t or "game" in t:
            return False
    # Fonte precisa mencionar 80 Plus bronze/gold? (opcional, comenta se não quiser filtrar)
    if "fonte" in kw:
        if not ("bronze" in t or "gold" in t or "80 plus" in t or "80plus" in t):
            # Permite mesmo sem isso? Descomente abaixo para exigir:
            # return False
            pass
    return True

# ---------------------------------------
# MAIN
# ---------------------------------------
async def main():
    # Telethon client
    if STRING_SESSION:
        client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    else:
        # Conexão por fluxo clássico (PHONE). Recomendado usar STRING_SESSION em produção.
        client = TelegramClient("telethon_realtime_session", API_ID, API_HASH)

    await client.connect()
    if not await client.is_user_authorized():
        if STRING_SESSION:
            log.error("STRING_SESSION inválida/expirada. Refaça a sessão.")
            return
        else:
            # login por telefone (último recurso)
            await client.send_code_request(PHONE)
            code = input("Digite o código do Telegram: ").strip()
            await client.sign_in(PHONE, code)

    # Loga o DC e status
    try:
        dc = await client(functions.help.GetConfigRequest())
        log.info("✅ Conectado ao Telegram; iniciando monitoramento…")
    except Exception:
        log.info("✅ Conectado ao Telegram; iniciando monitoramento…")

    # Garante que estamos inscritos/ouvindo os canais (públicos)
    for ch in CHANNELS:
        try:
            await client.get_entity(ch)
        except Exception as e:
            log.warning(f"Não foi possível resolver canal {ch}: {e}")

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
            # Se houver limite para o kw, logar decisão
            limit = LIMITS.get(kw, None)
            if price > 0:
                log.info(f"Keyword: {kw} | Preço identificado: R$ {price} (limite: {limit})")
            else:
                log.info(f"Keyword: {kw} | Preço não identificado")

            # Envia sempre que bater keyword; se houver limite e preço, pode checar:
            if limit is not None and price > 0 and price > limit:
                log.info(f"Preço acima do limite para '{kw}'. Enviando mesmo assim (tracking).")

            title = kw.replace("_", " ").title()
            await send_alert(client, title, price if price > 0 else 0.0, text, channel_ref)

        except Exception as e:
            log.exception(f"Erro no handler: {e}")

    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
