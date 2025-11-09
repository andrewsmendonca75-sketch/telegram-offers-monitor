# realtime.py
import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional, Dict, List

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import Message

# ----------------------- Config & Log -----------------------
load_dotenv()  # carrega .env local; no Render, usa ENV VARS

def require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Variável de ambiente '{name}' ausente. "
            f"Defina no .env local ou em Environment Variables do Render."
        )
    return val

try:
    API_ID = int(require_env("API_ID"))
    API_HASH = require_env("API_HASH")
    DEST_CHAT_ID = require_env("DEST_CHAT_ID")  # aceita ID numérico (str) ou @username
except Exception as e:
    # Mensagem clara no Render
    print("ERRO DE CONFIGURAÇÃO:", e)
    raise

SESSION_NAME = os.getenv("SESSION_NAME", "realtime_session")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("realtime")

# --------------------- Lista de canais ---------------------
CHANNELS = [
    "@pcbuildwizard", "@economister", "@talkpc", "@pcdorafa", "@pcmakertopofertas",
    "@promocoesdolock", "@hardtecpromocoes", "@canalandrwss", "@iuriindica",
    "@dantechofertas", "@mpromotech", "@mmpromo", "@promohypepcgamer",
    "@ofertaskabum", "@terabyteshopoficial", "@pichauofertas",
    "@sohardwaredorocha", "@soplacadevideo",
]

# --------------------- Regras/Keywords ---------------------
WHITELIST_ALWAYS = [
    # Consoles
    "ps5", "playstation 5", "playstation5",
    # GPUs alvo
    "rtx 5060", "5060 ti", "rx 7600",
    # Teclados
    "kumara k552", "k552", "elf pro", "k649", "surara", "k582",
    # Energia
    "iclamp", "iclamp energia", "iclamp 5t", "iclamp energia 5t", "iclamp 5 t",
    "iclamp", "iClamper", "clamper",
    # Outros que você costuma acompanhar
    "water cooler 120", "fonte 650w 80 plus bronze", "gabinete", "kit de fans", "ventoinhas",
]

BLACKLIST_SNIPPETS = [
    "saiu vídeo", "review no youtube", "inscreva-se no nosso canal",
    "link do vídeo", "assista no youtube", "estreia no youtube",
]

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

RE_PRICE = re.compile(r"R\$\s*([\d\.]{1,3}(?:\.\d{3})*(?:,\d{2})|\d+(?:,\d{2})?)", re.IGNORECASE)

# --------------------- Agrupador por canal -----------------
AGG_WINDOW_SECONDS = 4.0  # junta msgs por 4s após o primeiro MATCH
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

@dataclass
class Bucket:
    texts: List[str]
    timer: Optional[asyncio.Task]

buckets: Dict[int, Bucket] = {}  # channel_id -> Bucket

def get_text(msg: Message) -> str:
    text = (getattr(msg, "text", None) or
            getattr(msg, "message", None) or
            getattr(msg, "raw_text", None) or
            getattr(msg, "caption", None) or "")
    return text.strip()

def contains_any(text: str, needles) -> bool:
    t = text.lower()
    return any(n.lower() in t for n in needles)

def price_to_float(s: str) -> Optional[float]:
    """
    Converte '2.970,90' -> 2970.90 ; '2955' -> 2955.0
    """
    try:
        # tira separador de milhar
        s = s.replace(".", "")
        # vírgula vira decimal
        s = s.replace(",", ".")
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

    # Whitelist forte – repassa sempre
    if contains_any(t, WHITELIST_ALWAYS):
        return MatchResult(True, "Whitelist forte (produto alvo)")

    price = extract_first_price(text)

    # RAM DDR4 8GB 3200
    if ("ddr4" in t and "8gb" in t and "3200" in t):
        if price is not None and price <= PRICE_LIMITS["ram_ddr4_8gb_3200"]:
            return MatchResult(True, f"RAM DDR4 8GB 3200 ≤ {PRICE_LIMITS['ram_ddr4_8gb_3200']} (R$ {price:.2f})")
        return MatchResult(False, "RAM DDR4 3200 fora do teto ou sem preço")

    # SSD NVMe 1TB
    if ("ssd" in t and "nvme" in t and ("1tb" in t or "1 tb" in t)):
        if price is not None and price <= PRICE_LIMITS["ssd_nvme_1tb"]:
            return MatchResult(True, f"SSD NVMe M.2 1TB ≤ {PRICE_LIMITS['ssd_nvme_1tb']} (R$ {price:.2f})")
        return MatchResult(False, "SSD NVMe M.2 1TB > teto ou sem preço")

    # CPU
    if any(k in t for k in ["ryzen", "intel core", "i3-", "i5-", "i7-", "i9-"]):
        if price is not None and price <= PRICE_LIMITS["cpu_max"]:
            return MatchResult(True, f"CPU ≤ {PRICE_LIMITS['cpu_max']} (R$ {price:.2f})")
        return MatchResult(False, "CPU > teto ou sem preço")

    # PS5
    if "ps5" in t or "playstation 5" in t:
        return MatchResult(True, "PS5 console")

    # Placas-mãe
    if "b550" in t:
        if price is not None and price <= PRICE_LIMITS["mobo_b550_max"]:
            return MatchResult(True, f"MOBO B550 ≤ {PRICE_LIMITS['mobo_b550_max']} (R$ {price:.2f})")
        return MatchResult(False, "MOBO B550, mas preço > teto ou ausente")

    if ("lga1700" in t or "b660" in t or "b760" in t):
        if price is not None and price <= PRICE_LIMITS["mobo_lga1700_max"]:
            return MatchResult(True, f"MOBO LGA1700 ≤ {PRICE_LIMITS['mobo_lga1700_max']} (R$ {price:.2f})")
        return MatchResult(False, "MOBO LGA1700 > teto ou ausente")

    if "x570" in t:
        if price is not None and price <= PRICE_LIMITS["mobo_x570_max"]:
            return MatchResult(True, f"MOBO X570 ≤ {PRICE_LIMITS['mobo_x570_max']} (R$ {price:.2f})")
        return MatchResult(False, "MOBO X570 > teto ou ausente")

    # Gabinete
    if "gabinete" in t and ("4 fan" in t or "4 fans" in t):
        if price is not None and price <= PRICE_LIMITS["gabinete_4fans_max"]:
            return MatchResult(True, f"Gabinete ok: 4 fans ≤ R$ {PRICE_LIMITS['gabinete_4fans_max']:.0f} (R$ {price:.2f})")
        return MatchResult(False, "Gabinete bloqueado: <5 fans e preço < 150")

    # Fonte 650W Bronze
    if ("650w" in t and ("80 plus bronze" in t or "80+ bronze" in t)):
        if price is not None and price <= PRICE_LIMITS["psu_650w_bronze_max"]:
            return MatchResult(True, f"PSU ok: 650W 80 Plus Bronze ≤ R$ {PRICE_LIMITS['psu_650w_bronze_max']:.0f} (R$ {price:.2f})")
        return MatchResult(False, "PSU fora das regras")

    # Water cooler 120
    if "water cooler" in t and "120" in t:
        if price is not None and price < 200:
            return MatchResult(True, f"Water cooler < 200 (R$ {price:.2f})")
        return MatchResult(False, "Water cooler >= 200 ou sem preço")

    # Redragon superiores
    if any(k in t for k in ["elf pro", "k649", "surara", "k582"]):
        if price is not None and price <= PRICE_LIMITS["redragon_superior_max"]:
            return MatchResult(True, f"Redragon superior ≤ R$ {PRICE_LIMITS['redragon_superior_max']:.0f}")
        return MatchResult(False, "Redragon superior > teto — bloquear")

    # Kumara K552 — sempre
    if "kumara" in t or "k552" in t:
        return MatchResult(True, "Kumara (K552) — alertar sempre")

    # iClamper
    if "iclamp" in t or "iclamp energia" in t or "iClamper".lower() in t or "clamper" in t:
        return MatchResult(True, "iClamper")

    return MatchResult(False, "sem match")

def normalized_username(entity) -> str:
    u = getattr(entity, "username", None)
    if not u:
        return "Canal"
    return "@" + u if not u.startswith("@") else u

def build_out(full_text: str, source_username: str) -> str:
    return f"{full_text.strip()}\n\nFonte: {source_username}"

async def flush_bucket(chat_id: int, source: str):
    bucket = buckets.get(chat_id)
    if not bucket:
        return
    text = "\n".join(bucket.texts).strip()
    if text:
        await client.send_message(DEST_CHAT_ID, build_out(text, source))
        logging.info("· envio=ok → destino=bot")
    # limpa
    if bucket.timer:
        bucket.timer.cancel()
    buckets.pop(chat_id, None)

def schedule_flush(chat_id: int, source: str):
    # cancela timer anterior e agenda novo
    async def _wait_and_flush():
        await asyncio.sleep(AGG_WINDOW_SECONDS)
        await flush_bucket(chat_id, source)
    b = buckets[chat_id]
    if b.timer:
        b.timer.cancel()
    b.timer = asyncio.create_task(_wait_and_flush())

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
            # abre bucket e espera msgs seguintes do mesmo post (preço/cupom/link)
            buckets[chat_id] = Bucket(texts=[text], timer=None)
            schedule_flush(chat_id, source)
        else:
            logging.info(f"[{source:<20}] IGNORADO → {text[:40].replace(chr(10),' ')}… reason={mr.reason}")

    except Exception as e:
        logging.exception(f"Erro no handler: {e}")

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
    await asyncio.Future()  # run forever

if __name__ == "__main__":
    try:
        with client:
            client.loop.run_until_complete(main())
    except KeyboardInterrupt:
        log.info("Encerrado pelo usuário.")
