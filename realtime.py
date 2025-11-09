# realtime.py
import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional, Dict, List

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import Message

# ----------------------- Log -----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("realtime")

# --------------------- Helpers ENV -----------------
def getenv_any(*keys: str, default: Optional[str] = None) -> Optional[str]:
    for k in keys:
        v = os.getenv(k)
        if v:
            return v
    return default

def require_any(*keys: str) -> str:
    v = getenv_any(*keys)
    if not v:
        joined = " ou ".join(keys)
        raise RuntimeError(f"Variável de ambiente ausente: defina {joined}.")
    return v

def parse_csv_env(key: str) -> List[str]:
    raw = os.getenv(key, "")
    items = [s.strip() for s in raw.split(",") if s.strip()]
    return items

# ------------------- Carrega ENV -------------------
API_ID = int(require_any("TELEGRAM_API_ID", "API_ID"))
API_HASH = require_any("TELEGRAM_API_HASH", "API_HASH")
STRING_SESSION = getenv_any("TELEGRAM_STRING_SESSION", "STRING_SESSION", default=None)

# Destinos: primeiro USER_DESTINATIONS (CSV), depois USER_CHAT_ID, depois DEST_CHAT_ID
dest_list = parse_csv_env("USER_DESTINATIONS")
if not dest_list:
    one = getenv_any("USER_CHAT_ID", "DEST_CHAT_ID")
    if one:
        dest_list = [one]
if not dest_list:
    raise RuntimeError("Defina USER_DESTINATIONS (CSV) ou USER_CHAT_ID/DEST_CHAT_ID.")

# Canais: MONITORED_CHANNELS (CSV). Se vazio, usa um default seguro.
CHANNELS = parse_csv_env("MONITORED_CHANNELS") or [
    "@pcbuildwizard", "@EconoMister", "@TalkPC", "@pcdorafa", "@PCMakerTopOfertas",
    "@promocoesdolock", "@HardTecPromocoes", "@canalandrwss", "@iuriindica",
    "@dantechofertas", "@mpromotech", "@mmpromo", "@promohypepcgamer",
    "@ofertaskabum", "@terabyteshopoficial", "@pichauofertas",
    "@sohardwaredorocha", "@soplacadevideo",
]

SESSION_NAME = os.getenv("SESSION_NAME", "realtime_session")

# --------------------- Regras ----------------------
PRICE_LIMITS = {
    "ram_ddr4_8gb_3200": 180.0,
    "ssd_nvme_1tb": 460.0,
    "cpu_max": 900.0,
    "psu_650w_bronze_max": 350.0,
    "gabinete_4fans_max": 180.0,
    "mobo_b550_max": 550.0,
    "mobo_lga1700_max": 680.0,
    "mobo_x570_max": 680.0,
    "redragon_superior_max": 160.0,
}

WHITELIST_ALWAYS = [
    "ps5", "playstation 5", "playstation5",
    "rtx 5060", "5060 ti", "rx 7600",
    "kumara k552", "k552", "elf pro", "k649", "surara", "k582",
    "iclamp", "iclamp energia", "iclamp 5t", "clamper", "iclamp energia 5t",
    "water cooler 120", "fonte 650w 80 plus bronze", "gabinete", "kit de fans", "ventoinhas",
]
BLACKLIST_SNIPPETS = [
    "saiu vídeo", "review no youtube", "inscreva-se no nosso canal",
    "link do vídeo", "assista no youtube", "estreia no youtube",
]
RE_PRICE = re.compile(r"R\$\s*([\d\.]{1,3}(?:\.\d{3})*(?:,\d{2})|\d+(?:,\d{2})?)", re.IGNORECASE)

AGG_WINDOW_SECONDS = 4.0  # junta mensagens relacionadas por 4s

# -------------------- Cliente TG -------------------
if STRING_SESSION:
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
else:
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

@dataclass
class Bucket:
    texts: List[str]
    timer: Optional[asyncio.Task]

buckets: Dict[int, Bucket] = {}  # channel_id -> Bucket

# -------------------- Utils texto ------------------
def get_text(msg: Message) -> str:
    return (
        getattr(msg, "text", None)
        or getattr(msg, "message", None)
        or getattr(msg, "raw_text", None)
        or getattr(msg, "caption", None)
        or ""
    ).strip()

def contains_any(text: str, needles) -> bool:
    t = text.lower()
    return any(n.lower() in t for n in needles)

def price_to_float(s: str) -> Optional[float]:
    try:
        s = s.replace(".", "")        # remove milhares
        s = s.replace(",", ".")       # vírgula -> decimal
        return float(s)
    except Exception:
        return None

def extract_first_price(text: str) -> Optional[float]:
    m = RE_PRICE.search(text)
    if not m:
        return None
    return price_to_float(m.group(1))

@dataclass
class MatchResult:
    matched: bool
    reason: str

def rule_match(text: str) -> MatchResult:
    t = text.lower()

    if contains_any(t, BLACKLIST_SNIPPETS):
        return MatchResult(False, "Post de vídeo/YouTube — ignorar")

    if contains_any(t, WHITELIST_ALWAYS):
        return MatchResult(True, "Whitelist forte (produto alvo)")

    price = extract_first_price(text)

    if ("ddr4" in t and "8gb" in t and "3200" in t):
        if price is not None and price <= PRICE_LIMITS["ram_ddr4_8gb_3200"]:
            return MatchResult(True, f"RAM DDR4 8GB 3200 ≤ {PRICE_LIMITS['ram_ddr4_8gb_3200']}")
        return MatchResult(False, "RAM DDR4 3200 fora do teto ou sem preço")

    if ("ssd" in t and "nvme" in t and ("1tb" in t or "1 tb" in t)):
        if price is not None and price <= PRICE_LIMITS["ssd_nvme_1tb"]:
            return MatchResult(True, f"SSD NVMe M.2 1TB ≤ {PRICE_LIMITS['ssd_nvme_1tb']}")
        return MatchResult(False, "SSD NVMe M.2 1TB > teto ou sem preço")

    if any(k in t for k in ["ryzen", "intel core", "i3-", "i5-", "i7-", "i9-"]):
        if price is not None and price <= PRICE_LIMITS["cpu_max"]:
            return MatchResult(True, f"CPU ≤ {PRICE_LIMITS['cpu_max']}")
        return MatchResult(False, "CPU > teto ou sem preço")

    if "ps5" in t or "playstation 5" in t:
        return MatchResult(True, "PS5 console")

    if "b550" in t:
        if price is not None and price <= PRICE_LIMITS["mobo_b550_max"]:
            return MatchResult(True, f"MOBO B550 ≤ {PRICE_LIMITS['mobo_b550_max']}")
        return MatchResult(False, "MOBO B550 > teto ou ausente")

    if ("lga1700" in t or "b660" in t or "b760" in t):
        if price is not None and price <= PRICE_LIMITS["mobo_lga1700_max"]:
            return MatchResult(True, f"MOBO LGA1700 ≤ {PRICE_LIMITS['mobo_lga1700_max']}")
        return MatchResult(False, "MOBO LGA1700 > teto ou ausente")

    if "x570" in t:
        if price is not None and price <= PRICE_LIMITS["mobo_x570_max"]:
            return MatchResult(True, f"MOBO X570 ≤ {PRICE_LIMITS['mobo_x570_max']}")
        return MatchResult(False, "MOBO X570 > teto ou ausente")

    if "gabinete" in t and ("4 fan" in t or "4 fans" in t):
        if price is not None and price <= PRICE_LIMITS["gabinete_4fans_max"]:
            return MatchResult(True, f"Gabinete ok: 4 fans ≤ {PRICE_LIMITS['gabinete_4fans_max']}")
        return MatchResult(False, "Gabinete bloqueado: <5 fans e preço < 150")

    if ("650w" in t and ("80 plus bronze" in t or "80+ bronze" in t)):
        if price is not None and price <= PRICE_LIMITS["psu_650w_bronze_max"]:
            return MatchResult(True, f"PSU ok: 650W 80 Plus Bronze ≤ {PRICE_LIMITS['psu_650w_bronze_max']}")
        return MatchResult(False, "PSU fora das regras")

    if "water cooler" in t and "120" in t:
        if price is not None and price < 200:
            return MatchResult(True, "Water cooler < 200")
        return MatchResult(False, "Water cooler >= 200 ou sem preço")

    if any(k in t for k in ["elf pro", "k649", "surara", "k582"]):
        if price is not None and price <= PRICE_LIMITS["redragon_superior_max"]:
            return MatchResult(True, f"Redragon superior ≤ {PRICE_LIMITS['redragon_superior_max']}")
        return MatchResult(False, "Redragon superior > teto — bloquear")

    if "kumara" in t or "k552" in t:
        return MatchResult(True, "Kumara (K552) — alertar sempre")

    if any(k in t for k in ["iclamp", "clamper", "iclamp energia", "iclamp 5t"]):
        return MatchResult(True, "iClamper")

    return MatchResult(False, "sem match")

def normalized_username(entity) -> str:
    u = getattr(entity, "username", None)
    if not u:
        return "Canal"
    return "@" + u if not u.startswith("@") else u

def build_out(full_text: str, source_username: str) -> str:
    return f"{full_text.strip()}\n\nFonte: {source_username}"

# ------------------- Aggregation --------------------
@dataclass
class Bucket:
    texts: List[str]
    timer: Optional[asyncio.Task]

buckets: Dict[int, Bucket] = {}

async def flush_bucket(chat_id: int, source: str):
    bucket = buckets.get(chat_id)
    if not bucket:
        return
    text = "\n".join(bucket.texts).strip()
    if text:
        for dest in dest_list:
            await client.send_message(dest, build_out(text, source))
        logging.info("· envio=ok → destino(s)")
    if bucket.timer:
        bucket.timer.cancel()
    buckets.pop(chat_id, None)

def schedule_flush(chat_id: int, source: str):
    async def _wait_and_flush():
        await asyncio.sleep(AGG_WINDOW_SECONDS)
        await flush_bucket(chat_id, source)
    b = buckets[chat_id]
    if b.timer:
        b.timer.cancel()
    b.timer = asyncio.create_task(_wait_and_flush())

# ------------------- Handler -----------------------
@client.on(events.NewMessage(chats=CHANNELS))
async def handler(event: events.NewMessage.Event):
    try:
        msg: Message = event.message
        channel = await event.get_chat()
        chat_id = getattr(channel, "id", None)
        source = normalized_username(channel)
        text = get_text(msg)

        if not text:
            logging.info(f"[{source:<20}] IGNORADO → (sem texto/legenda)")
            return

        # Se já existe bucket aberto para esse canal, acumula e reprograma flush
        if chat_id in buckets:
            buckets[chat_id].texts.append(text)
            schedule_flush(chat_id, source)
            logging.info(f"[{source:<20}] +ACUMULANDO → {text[:40].replace(chr(10),' ')}…")
            return

        # Primeiro contato: decide se abre bucket (MATCH) ou ignora
        mr = rule_match(text)
        if mr.matched:
            logging.info(f"[{source:<20}] MATCH    → {text[:40].replace(chr(10),' ')}… reason={mr.reason}")
            buckets[chat_id] = Bucket(texts=[text], timer=None)
            schedule_flush(chat_id, source)
        else:
            logging.info(f"[{source:<20}] IGNORADO → {text[:40].replace(chr(10),' ')}… reason={mr.reason}")

    except Exception as e:
        logging.exception(f"Erro no handler: {e}")

# -------------------- Main -------------------------
async def main():
    log.info("Conectando ao Telegram…")
    await client.start()
    log.info("Conectado.")
    resolved = []
    for ch in CHANNELS:
        try:
            ent = await client.get_entity(ch)
            resolved.append(normalized_username(ent))
        except Exception:
            resolved.append(ch)
    log.info("▶️ Canais resolvidos: " + ", ".join(resolved))
    log.info(f"✅ Logado — monitorando {len(CHANNELS)} canais…")
    log.info("▶️ Rodando. Pressione Ctrl+C para sair.")
    await asyncio.Future()

if __name__ == "__main__":
    try:
        with client:
            client.loop.run_until_complete(main())
    except KeyboardInterrupt:
        log.info("Encerrado pelo usuário.")
