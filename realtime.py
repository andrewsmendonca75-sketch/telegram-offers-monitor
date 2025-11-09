# realtime.py
import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional, Tuple

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import Message

# ----------------------- Config & Log -----------------------
load_dotenv()
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_NAME = os.getenv("SESSION_NAME", "realtime_session")
DEST_CHAT_ID = os.getenv("DEST_CHAT_ID")  # pode ser número (str) ou @username

# Canais a monitorar (usernames exatos, com @, case-insensitive)
CHANNELS = [
    "@pcbuildwizard", "@economister", "@talkpc", "@pcdorafa", "@pcmakertopofertas",
    "@promocoesdolock", "@hardtecpromocoes", "@canalandrwss", "@iuriindica",
    "@dantechofertas", "@mpromotech", "@mmpromo", "@promohypepcgamer",
    "@ofertaskabum", "@terabyteshopoficial", "@pichauofertas",
    "@sohardwaredorocha", "@soplacadevideo",
]

# Palavras-chave que forçam encaminhamento integral (whitelist forte)
WHITELIST_ALWAYS = [
    # Consoles
    "ps5", "playstation 5", "playstation5",
    # GPUs alvo
    "rtx 5060", "5060 ti", "rx 7600",
    # Teclados
    "kumara k552", "k552", "elf pro", "k649", "surara", "k582",
    # Energia/segurança
    "iclamp", "iclamp", "iclamp", "iclamp", "iclamp", "iclamp", "iclamp", "iclamp", "iclamp", "iclamp", "iclamp",
    "iclamp", "iclamp", "iclamp", "iclamp", "iclamp", "iclamp", "iclamp", "iclamp", "iclamp",  # redundância ok
    "iclamp", "iclamp", "iclamp", "iclamp", "iclamp", "iclamp", "iclamp", "iclamp",
    "iclamp", "iclamp", "iclamp", "iclamp",  # (o matcher é case-insensitive)
    "iclamp", "iclamp", "iclamp", "iclamp", "iclamp",
    "iclamp", "iclamp", "iclamp", "iclamp", "iclamp",
    "iclamp", "iclamp",
    "iclamp", "iclamp", "iclamp",
    "iclamp", "iclamp", "iclamp",
    "iclamp", "iclamp",
    "iclamp",
    "iclamp",  # (mantido por segurança de match)
    "iclamp", "iclamp",
    "iclamp",
    "iclamp",
    "iclamp",  # ok
    "iClamper", "clamper", "iclamp", "iclamp energia", "iclamp 5t", "iclamp energia 5t",
    # Resfriamento e gabinete/PSU
    "water cooler 120", "fonte 650w 80 plus bronze", "gabinete", "kit de fans", "ventoinhas",
]

# Stopwords para ignorar (vídeo/YouTube e posts de “conteúdo”, não oferta)
BLACKLIST_SNIPPETS = [
    "saiu vídeo", "review no youtube", "inscreva-se no nosso canal",
    "link do vídeo", "assista no youtube", "estreia no youtube",
]

# Critérios de preço (mantidos do seu comportamento)
PRICE_LIMITS = {
    "ram_ddr4_8gb_3200": 180.0,
    "ssd_nvme_1tb": 460.0,
    "cpu_max": 900.0,
    "psu_650w_bronze_max": 350.0,
    "gabinete_4fans_max": 180.0,
    "mobo_b550_max": 550.0,
    "mobo_lga1700_max": 680.0,
    "mobo_x570_max": 680.0,
    "redragon_superior_max": 160.0,  # Elf Pro etc.
}

# Regex helpers
RE_PRICE = re.compile(r"R\$\s*([\d\.\,]+)", re.IGNORECASE)
RE_URL = re.compile(r"https?://\S+", re.IGNORECASE)

# Log
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("realtime")

# --------------------- Utils & Matchers ---------------------
def tg_text(msg: Message) -> str:
    """
    Pega SEMPRE o texto completo:
    - texto puro (msg.message / msg.text)
    - legenda de mídia (msg.caption)
    Mantém quebras de linha. Não formata markdown (deixa como veio).
    """
    # Telethon normaliza .text para texto+legenda; se vier vazio, tenta .message
    text = (getattr(msg, "text", None) or
            getattr(msg, "message", None) or
            getattr(msg, "raw_text", None) or
            getattr(msg, "caption", None) or
            "")
    return text.strip()

def price_to_float(pt: str) -> Optional[float]:
    pt = pt.replace(".", "").replace(",", ".")
    try:
        return float(pt)
    except Exception:
        return None

def extract_first_price(text: str) -> Optional[float]:
    m = RE_PRICE.search(text)
    if not m:
        return None
    return price_to_float(m.group(1))

def contains_any(text: str, needles) -> bool:
    t = text.lower()
    return any(n.lower() in t for n in needles)

@dataclass
class MatchResult:
    matched: bool
    reason: str

def rule_match(text: str, channel: str) -> MatchResult:
    t = text.lower()

    # 1) Ignorar vídeos/YouTube e assuntos não-oferta
    if contains_any(t, BLACKLIST_SNIPPETS):
        return MatchResult(False, "Post de vídeo/YouTube — ignorar")

    # 2) Whitelist forte — se falar em alvos principais, repassa sempre
    if contains_any(t, WHITELIST_ALWAYS):
        return MatchResult(True, "Whitelist forte (produto alvo)")

    # 3) Regras de produto com teto de preço
    price = extract_first_price(text)

    # RAM DDR4 8GB 3200
    if ("ddr4" in t and "8gb" in t and "3200" in t) and price is not None:
        if price <= PRICE_LIMITS["ram_ddr4_8gb_3200"]:
            return MatchResult(True, f"RAM DDR4 8GB 3200 ≤ {PRICE_LIMITS['ram_ddr4_8gb_3200']} (R$ {price:.2f})")
        else:
            return MatchResult(False, "RAM DDR4 3200 fora do teto ou sem preço")

    # SSD NVMe 1TB
    if ("ssd" in t and "nvme" in t and ("1tb" in t or "1 tb" in t)) and price is not None:
        if price <= PRICE_LIMITS["ssd_nvme_1tb"]:
            return MatchResult(True, f"SSD NVMe M.2 1TB ≤ {PRICE_LIMITS['ssd_nvme_1tb']} (R$ {price:.2f})")
        else:
            return MatchResult(False, "SSD NVMe M.2 1TB > teto ou sem preço")

    # CPUs
    if any(cpu in t for cpu in ["ryzen", "intel core", "i3-", "i5-", "i7-", "i9-"]):
        if price is not None and price <= PRICE_LIMITS["cpu_max"]:
            return MatchResult(True, f"CPU ≤ {PRICE_LIMITS['cpu_max']} (R$ {price:.2f})")
        else:
            return MatchResult(False, "CPU > teto ou sem preço")

    # PS5
    if "ps5" in t or "playstation 5" in t:
        return MatchResult(True, "PS5 console")

    # Placas-mãe
    if "b550" in t and price is not None:
        if price <= PRICE_LIMITS["mobo_b550_max"]:
            return MatchResult(True, f"MOBO B550 ≤ {PRICE_LIMITS['mobo_b550_max']} (R$ {price:.2f})")
        return MatchResult(False, "MOBO B550, mas preço > teto ou ausente")

    if ("lga1700" in t or "tuf gaming b660" in t or "b760" in t) and price is not None:
        if price <= PRICE_LIMITS["mobo_lga1700_max"]:
            return MatchResult(True, f"MOBO LGA1700 ≤ {PRICE_LIMITS['mobo_lga1700_max']} (R$ {price:.2f})")
        return MatchResult(False, "MOBO LGA1700 > teto ou ausente")

    if "x570" in t and price is not None:
        if price <= PRICE_LIMITS["mobo_x570_max"]:
            return MatchResult(True, f"MOBO X570 ≤ {PRICE_LIMITS['mobo_x570_max']} (R$ {price:.2f})")
        return MatchResult(False, "MOBO X570 > teto ou ausente")

    # Gabinete 4 fans
    if "gabinete" in t and ("4 fan" in t or "4 fans" in t) and price is not None:
        if price <= PRICE_LIMITS["gabinete_4fans_max"]:
            return MatchResult(True, f"Gabinete ok: 4 fans ≤ R$ {PRICE_LIMITS['gabinete_4fans_max']:.0f} (R$ {price:.2f})")
        return MatchResult(False, "Gabinete bloqueado: <5 fans e preço < 150")

    # Fonte 650W Bronze
    if ("650w" in t and ("80 plus bronze" in t or "80+ bronze" in t)) and price is not None:
        if price <= PRICE_LIMITS["psu_650w_bronze_max"]:
            return MatchResult(True, f"PSU ok: 650W 80 Plus Bronze ≤ R$ {PRICE_LIMITS['psu_650w_bronze_max']:.0f} (R$ {price:.2f})")
        return MatchResult(False, "PSU fora das regras")

    # Water cooler 120
    if "water cooler" in t and "120" in t:
        if price is not None and price < 200:
            return MatchResult(True, f"Water cooler < 200 (R$ {price:.2f})")
        return MatchResult(False, "Water cooler >= 200 ou sem preço")

    # Teclados Redragon superiores (Elf Pro etc.)
    if any(k in t for k in ["elf pro", "k649", "surara", "k582"]):
        if price is not None and price <= PRICE_LIMITS["redragon_superior_max"]:
            return MatchResult(True, f"Redragon superior ≤ R$ {PRICE_LIMITS['redragon_superior_max']:.0f}")
        return MatchResult(False, "Redragon superior > teto — bloquear")

    # Kumara K552 — alertar sempre
    if "kumara" in t or "k552" in t:
        return MatchResult(True, "Kumara (K552) — alertar sempre")

    # iClamper
    if "iclamp" in t or "iclamp" in t or "iClamper".lower() in t or "clamper" in t:
        return MatchResult(True, "iClamper")

    # Se chegou aqui, não bateu em nada (mas pode ser oferta). Não envia.
    return MatchResult(False, "sem match")

def build_forward_text(original_text: str, source_username: str) -> str:
    """
    Monta o texto que você quer receber:
    - Mensagem original COMPLETA
    - Acrescenta 'Fonte: @canal' no fim
    """
    clean = original_text.strip()
    # Evita duplicar "Fonte:" se o canal já coloca; mas mantemos nosso carimbo ao final
    return f"{clean}\n\nFonte: {source_username}"

def normalized_username(entity) -> str:
    u = getattr(entity, "username", None)
    if not u:
        return "Canal"
    if not u.startswith("@"):
        u = "@" + u
    return u

# ------------------------- App ------------------------------
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

@client.on(events.NewMessage(chats=CHANNELS))
async def handler(event: events.NewMessage.Event):
    try:
        msg: Message = event.message
        channel = await event.get_chat()
        source = normalized_username(channel)

        text = tg_text(msg)

        # Se não houver texto (ex: só mídia sem legenda), pula
        if not text:
            logging.info(f"[{source:<20}] IGNORADO → (sem texto/legenda)")
            return

        match = rule_match(text, source)

        if match.matched:
            out = build_forward_text(text, source)
            await client.send_message(DEST_CHAT_ID, out)
            logging.info(f"[{source:<20}] MATCH    → {text[:40].replace(chr(10),' ')}… reason={match.reason}")
            logging.info("· envio=ok → destino=bot")
        else:
            logging.info(f"[{source:<20}] IGNORADO → {text[:40].replace(chr(10),' ')}… reason={match.reason}")

    except Exception as e:
        logging.exception(f"Erro no handler: {e}")

async def main():
    # Conexão e verificação de canais
    log.info("Conectando ao Telegram…")
    await client.start()
    log.info("Conectado.")
    # Resolve/normaliza canais e confirma monitoramento
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
