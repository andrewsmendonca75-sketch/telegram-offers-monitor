# realtime.py
import os
import re
import json
import asyncio
import logging
from typing import List, Union, Optional, Tuple

from telethon import TelegramClient, events
from telethon.sessions import StringSession
import aiohttp  # envio via Bot API

# ----------------- LOGGING -----------------
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ----------------- ENV -----------------
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
STRING_SESSION = os.environ["TELEGRAM_STRING_SESSION"]

BOT_TOKEN = os.environ["TELEGRAM_TOKEN"]            # bot que enviará a msg
TARGET_CHAT = os.getenv("USER_CHAT_ID", "").strip() # seu chat id numérico
if TARGET_CHAT.isdigit():
    TARGET_CHAT = int(TARGET_CHAT)
else:
    # fallback: permite username tipo @usuario como destino
    TARGET_CHAT = TARGET_CHAT if TARGET_CHAT else None

RAW_CHANNELS = os.getenv("MONITORED_CHANNELS", "").split(",")
MONITORED_CHANNELS = [c for c in (x.strip() for x in RAW_CHANNELS) if c]

# ---------- REGRAS / LIMITES ----------
PRICE_MAX_SANE = 100_000.0

CPU_PRICE_LIMIT = 900.0
MOBO_PRICE_LIMIT = 680.0

PSU_PRICE_LIMIT = 350.0
PSU_MIN_WATTS = 600
PSU_ALLOWED_EFF = ("bronze", "gold")

WATER_LIMIT = 200.0

CASE_ALERT_PRICE = 230.0
CASE_BLOCK_PRICE = 150.0
CASE_MIN_FANS_FOR_ALERT = 5

# ===================== MONITOR/HELPERS =====================
def parse_monitored_chats(raw_list: List[str]) -> Tuple[set, set]:
    """
    Retorna (usernames_set, ids_set)
     - aceita '@canal' | 'canal'  -> username sem '@'
     - 'id:123' ou '123'          -> id int
    """
    names, ids = set(), set()
    for item in raw_list:
        if not item:
            continue
        s = str(item).strip()
        if not s:
            continue
        if s.lower().startswith("id:"):
            s = s.split(":", 1)[1].strip()
        if s.isdigit():
            try:
                ids.add(int(s))
            except:
                pass
        else:
            if s.startswith("@"):
                s = s[1:]
            if s:
                names.add(s.lower())
    return names, ids


async def warm_entity_cache(client: TelegramClient):
    try:
        await client.get_dialogs(limit=None)
        logger.info("Cache de entidades aquecido (get_dialogs).")
    except Exception as e:
        logger.warning(f"Falha ao aquecer cache de entidades: {e}")


# ===================== PREÇO / PARSER =====================
_CURRENCY = r"(?:R\$\s*|\b(?:por|preço|valor|apenas|somente)\s*:?\s*)?"
_NUM = r"(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{2})?"
PRICE_REGEX = re.compile(
    rf"(?<![%\d]){_CURRENCY}({_NUM})\b",
    flags=re.IGNORECASE
)

def _to_float_br(num_str: str) -> float:
    s = num_str.replace('.', '').replace(',', '.')
    try:
        return float(s)
    except:
        return float('nan')

def _near(text: str, idx: int, window: int = 18) -> str:
    a = max(0, idx - window)
    b = min(len(text), idx + window)
    return text[a:b].lower()

def extract_reasonable_price(text: str) -> Optional[float]:
    candidates: List[Tuple[float, int, bool]] = []
    for m in PRICE_REGEX.finditer(text):
        raw = m.group(1)
        price = _to_float_br(raw)
        if not (0 < price <= PRICE_MAX_SANE):
            continue

        ctx = _near(text, m.start())
        if "%" in ctx:
            continue
        if "cupom" in ctx:
            continue
        if "cashback" in ctx:
            continue

        pre = text[max(0, m.start()-4):m.start()]
        has_rsym = "R$" in pre
        candidates.append((price, m.start(), has_rsym))

    if not candidates:
        return None
    with_rs = [c for c in candidates if c[2]]
    pool = with_rs if with_rs else candidates
    best = sorted(pool, key=lambda t: (t[0], t[1]))[0]
    return best[0]


# ===================== CLASSIFICADORES =====================
_RE_GAB = re.compile(r"\b(gabinete)\b", re.IGNORECASE)
_RE_FAN_KIT = re.compile(r"\b(?:kit\s*(?:de)?\s*)?(?:fan|fans|ventoinha|ventoinhas)s?\b", re.IGNORECASE)
_RE_FAN_COUNT = re.compile(r"\b([3-9])\s*(?:x|un|fans?|ventoinhas?)\b", re.IGNORECASE)

_RE_RAM_DDR4 = re.compile(r"\bddr4\b", re.IGNORECASE)

# Intel alvo >= 12ª gen i5 com F/KF (inclui 12600F/KF, 13400F, 14400F etc.)
_RE_I5_TARGETS = re.compile(
    r"\b(i\d[-\s]*1[2-9]\d{2}(?:[fk]|f|kf)?|i5[-\s]*14400(?:f|kf)?)\b",
    re.IGNORECASE
)
# Ryzen 7 5700/5700X+ (5800X/5800X3D/5900X/5950X)
_RE_R7_TARGETS = re.compile(
    r"\b(ryzen\s*7\s*(?:5700x|5700|5800x3d|5800x|5900x|5950x))\b",
    re.IGNORECASE
)

# Mobo Intel LGA1700
_RE_MOBO_INTEL = re.compile(r"\b(h610|b660|b760|z690|z790)\b", re.IGNORECASE)
# AMD AM4 ok: B550/X570 | bloquear A520
_RE_MOBO_AMD_OK = re.compile(r"\b(b550|x570)\b", re.IGNORECASE)
_RE_MOBO_AMD_BLOCK = re.compile(r"\b(a520)\b", re.IGNORECASE)

# GPU
_RE_RTX5060 = re.compile(r"\brtx\s*5060\b", re.IGNORECASE)
_RE_RX7600  = re.compile(r"\brx\s*7600\b", re.IGNORECASE)

# PS5
_RE_PS5_CONSOLE = re.compile(r"\bps5\b|playstation\s*5|console\s*ps5", re.IGNORECASE)
_NEG_PS5 = re.compile(r"\b(capa|case|jogo|mídia|midia|digital|código|code|gift\s*card|dualsense|controle|dock)\b",
                      re.IGNORECASE)

# PSU
_RE_PSU = re.compile(r"\b(fonte|psu)\b", re.IGNORECASE)
_RE_WATTS = re.compile(r"\b(\d{3,4})\s*w\b", re.IGNORECASE)
_RE_EFF  = re.compile(r"\b(80\s*\+\s*(?:bronze|gold))\b", re.IGNORECASE)

# Water Cooler
_RE_WC = re.compile(r"\bwater\s*cooler\b", re.IGNORECASE)

# iCLAMPER
_RE_ICLAMPER = re.compile(r"\biclamper\b", re.IGNORECASE)

def is_ps5_accessory_or_game(text: str) -> bool:
    return bool(_NEG_PS5.search(text))

def count_fans(text: str) -> int:
    counts = [int(m.group(1)) for m in _RE_FAN_COUNT.finditer(text)]
    if counts:
        return max(counts)
    m = re.search(r"\b(\d+)\s*(?:coolers?|fans?|ventoinhas?)\b", text, flags=re.IGNORECASE)
    return int(m.group(1)) if m else 0


# ===================== DECISÃO =====================
def should_alert(text: str, price: Optional[float]) -> bool:
    t = text.lower()

    # iCLAMPER
    if _RE_ICLAMPER.search(t):
        return True

    # Kits de fans (3 a 9)
    if _RE_FAN_KIT.search(t):
        fans = count_fans(text)
        if 3 <= fans <= 9:
            return True

    # RAM DDR4
    if _RE_RAM_DDR4.search(t):
        return True

    # GPUs
    if _RE_RTX5060.search(t) or _RE_RX7600.search(t):
        return True

    # PS5 (somente console)
    if _RE_PS5_CONSOLE.search(t):
        if is_ps5_accessory_or_game(t):
            return False
        return True

    # CPUs
    if _RE_I5_TARGETS.search(t):
        return (price is not None) and (price <= CPU_PRICE_LIMIT)

    if _RE_R7_TARGETS.search(t):
        return (price is not None) and (price <= CPU_PRICE_LIMIT)

    # Mobos
    if _RE_MOBO_INTEL.search(t):
        return (price is not None) and (price <= MOBO_PRICE_LIMIT)

    if _RE_MOBO_AMD_BLOCK.search(t):
        return False
    if _RE_MOBO_AMD_OK.search(t):
        return (price is not None) and (price <= MOBO_PRICE_LIMIT)

    # Water Cooler
    if _RE_WC.search(t):
        return (price is not None) and (price <= WATER_LIMIT)

    # PSU
    if _RE_PSU.search(t):
        if price is None or price > PSU_PRICE_LIMIT:
            return False
        eff_ok = any(e in t for e in PSU_ALLOWED_EFF)
        if not eff_ok:
            m = _RE_EFF.search(t)
            eff_ok = bool(m and any(g in m.group(0).lower() for g in PSU_ALLOWED_EFF))
        if not eff_ok:
            return False
        m_w = _RE_WATTS.search(t)
        if not m_w:
            return False
        watts = int(m_w.group(1))
        return watts >= PSU_MIN_WATTS

    # Gabinetes
    if _RE_GAB.search(t):
        fans = count_fans(text)
        if price is not None and price < CASE_BLOCK_PRICE and fans < CASE_MIN_FANS_FOR_ALERT:
            return False
        if price is not None and price <= CASE_ALERT_PRICE and fans >= CASE_MIN_FANS_FOR_ALERT:
            return True
        return False

    return False


# ===================== ENVIO VIA BOT API =====================
async def send_via_bot(token: str, chat_id: Union[int, str], text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": False,
        "parse_mode": None
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, timeout=30) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.error(f"Falha ao enviar via bot ({resp.status}): {body}")
            else:
                logger.info("· envio=ok → destino=bot")


# ===================== MAIN =====================
async def main():
    if not MONITORED_CHANNELS:
        logger.warning("MONITORED_CHANNELS vazio — nada será filtrado (handler ouvirá tudo, mas filtrará por 0 canais).")
    names_allowed, ids_allowed = parse_monitored_chats(MONITORED_CHANNELS)

    printable = []
    if names_allowed:
        printable += [f"@{n}" for n in sorted(names_allowed)]
    if ids_allowed:
        printable += [str(i) for i in sorted(ids_allowed)]
    logger.info("▶️ Canais: %s", ", ".join(printable) if printable else "(nenhum)")
    
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Sessão inválida: gere nova TELEGRAM_STRING_SESSION.")

    await warm_entity_cache(client)
    logger.info("✅ Logado — monitorando %d canais…", len(names_allowed) + len(ids_allowed))

    @client.on(events.NewMessage())
    async def handler(event: events.NewMessage.Event):
        try:
            # ---- FILTRO DE CANAL/GRUPO AQUI (sem chats=...) ----
            chat = await event.get_chat()
            text = event.raw_text or ""
            if not text.strip():
                return

            # Decide se a msg vem de um dos monitorados
            ok_source = False
            # username
            uname = (getattr(chat, "username", None) or "").lower()
            if uname and uname in names_allowed:
                ok_source = True
            # id numérico
            cid = getattr(chat, "id", None)
            if isinstance(cid, int) and cid in ids_allowed:
                ok_source = True

            if not ok_source:
                return

            price = extract_reasonable_price(text)
            decision = should_alert(text, price)

            src_name = f"@{uname}" if uname else getattr(chat, "title", str(cid))
            logger.info("· [%s] %s → price=%s",
                        src_name,
                        "match" if decision else "ignorado (sem match)",
                        f"{price:.2f}" if isinstance(price, float) else "None")

            if decision and TARGET_CHAT:
                await send_via_bot(BOT_TOKEN, TARGET_CHAT, text)

        except Exception as e:
            logger.exception(f"Erro no handler: {e}")

    logger.info("▶️ Rodando. Pressione Ctrl+C para sair.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
