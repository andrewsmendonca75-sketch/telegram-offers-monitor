# realtime.py
import os
import re
import json
import asyncio
import logging
from typing import List, Union, Optional, Tuple

from telethon import TelegramClient, events
from telethon.sessions import StringSession

import aiohttp  # para enviar via Bot API

# ----------------- LOGGING -----------------
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ----------------- ENV -----------------
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
STRING_SESSION = os.environ["TELEGRAM_STRING_SESSION"]

BOT_TOKEN = os.environ["TELEGRAM_TOKEN"]           # bot que enviará a mensagem
TARGET_CHAT = os.getenv("USER_CHAT_ID")            # seu chat id numérico (ex.: 1818469361)
if TARGET_CHAT and TARGET_CHAT.strip().isdigit():
    TARGET_CHAT = int(TARGET_CHAT.strip())

# Lista de canais/grupos monitorados via ENV CSV (usernames ou IDs):
# Ex.: MONITORED_CHANNELS="@TalkPC,@pcdorafa,id:1465877129"
RAW_CHANNELS = os.getenv("MONITORED_CHANNELS", "").split(",")
MONITORED_CHANNELS = [c for c in (x.strip() for x in RAW_CHANNELS) if c]

# ---------- REGRAS / LIMITES ----------
PRICE_MAX_SANE = 100_000.0  # teto de sanidade para números absurdos

# CPUs: apenas alertar i5-14400F (ou superior) <= 900; 12600F/KF incluídos como ">="
#       Ryzen 7 5700/5700X (ou superior) <= 900
CPU_PRICE_LIMIT = 900.0

# Motherboards: Intel LGA1700 H610/B660/B760/Z690/Z790 <= 680
#               AMD B550 (AM4) <= 680; (NÃO alertar A520; X570 pode alertar se <= 680? É "acima da B550" no sentido da família AM4)
MOBO_PRICE_LIMIT = 680.0

# PSUs: apenas 80+ Bronze ou Gold, >= 600W, <= 350
PSU_PRICE_LIMIT = 350.0
PSU_MIN_WATTS = 600
PSU_ALLOWED_EFF = ("bronze", "gold")

# Water Cooler: somente se <= 200 (qualquer tamanho, mas o preço manda)
WATER_LIMIT = 200.0

# Gabinete:
# - Bloquear se preço < 150 e tiver 0-4 fans (ou “sem fans”)
# - Alertar se preço <= 230 e tiver 5+ fans
CASE_ALERT_PRICE = 230.0
CASE_BLOCK_PRICE = 150.0
CASE_MIN_FANS_FOR_ALERT = 5

# Kits de ventoinhas: alertar kits (3 a 9 fans) – sem limite específico
# Filtro de Linha iCLAMPER: alertar sempre que aparecer
# PS5: alertar CONSOLE; bloquear acessórios/jogos
# GPUs: RTX 5060 e RX 7600 (sem limite de preço definido por você; apenas alertar)
# RAM DDR4: alertar (sem limite específico)
# Placas-mãe B550: “qualquer B550” já coberta pela regra de mobo AMD <= 680


# ============== MONITOR/TELETHON HELPERS =================
def parse_monitored_chats(raw_list: List[str]) -> List[Union[int, str]]:
    """
    Aceita itens como:
      '@canal', 'canal'  => '@canal'
      'id:1234567890' ou '1234567890' => 1234567890 (int)
    """
    out: List[Union[int, str]] = []
    if not raw_list:
        return out
    for item in raw_list:
        if not item:
            continue
        s = str(item).strip()
        if not s:
            continue
        if s.lower().startswith('id:'):
            s = s.split(':', 1)[1].strip()
        if s.isdigit():
            out.append(int(s))
        else:
            out.append(s if s.startswith('@') else '@' + s)
    return out


async def warm_entity_cache(client: TelegramClient):
    """Populate dialogs para o Telethon ter access hash em cache (IDs numéricos funcionarem melhor)."""
    try:
        await client.get_dialogs(limit=None)
        logger.info("Cache de entidades aquecido (get_dialogs).")
    except Exception as e:
        logger.warning(f"Falha ao aquecer cache de entidades: {e}")


# ===================== PREÇO / PARSER =====================
_CURRENCY = r"(?:R\$\s*|\b(?:por|preço|valor|apenas|somente)\s*:?\s*)?"
# Número com . milhar e , decimal, ou sem separadores
_NUM = r"(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{2})?"
PRICE_REGEX = re.compile(
    rf"(?<![%\d]){_CURRENCY}({_NUM})\b",
    flags=re.IGNORECASE
)

def _to_float_br(num_str: str) -> float:
    """'2.029,99' -> 2029.99 | '89' -> 89.0"""
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
    """
    Vasculha diversos preços e tenta achar um plausível:
    - ignora porcentagens (%), cupons, números soltos de URL/ID
    - dá preferência a números precedidos por 'R$'
    - retorna o MENOR preço plausível (em promo é comum ter preço à vista + parcelado)
    """
    candidates: List[Tuple[float, int, bool]] = []  # (price, start_idx, has_r$)
    for m in PRICE_REGEX.finditer(text):
        raw = m.group(1)
        price = _to_float_br(raw)
        if not (0 < price <= PRICE_MAX_SANE):
            continue

        ctx = _near(text, m.start())
        if "%" in ctx:               # ex: 15%
            continue
        if "cupom" in ctx:           # ex: CUPOM: AGORA15
            continue
        if "cashback" in ctx:
            continue

        # Checa se há 'R$' explicitamente alguns chars antes
        pre = text[max(0, m.start()-4):m.start()]
        has_rsym = "R$" in pre

        candidates.append((price, m.start(), has_rsym))

    if not candidates:
        return None

    # Prefira com R$; depois menor preço
    with_rs = [c for c in candidates if c[2]]
    pool = with_rs if with_rs else candidates

    best = sorted(pool, key=lambda t: (t[0], t[1]))[0]
    return best[0]


# ===================== CLASSIFICADORES =====================
_RE_GAB = re.compile(r"\b(gabinete)\b", re.IGNORECASE)
_RE_FAN_KIT = re.compile(r"\b(?:kit\s*(?:de)?\s*)?(?:fan|fans|ventoinha|ventoinhas)s?\b", re.IGNORECASE)
_RE_FAN_COUNT = re.compile(r"\b([3-9])\s*(?:x|un|fans?|ventoinhas?)\b", re.IGNORECASE)

_RE_RAM_DDR4 = re.compile(r"\bddr4\b", re.IGNORECASE)

# CPU Intel i5 14400F+ (inclui 12600F/KF, 13400F etc. como “>=” para você)
_RE_I5_TARGETS = re.compile(
    r"\b(i\d[-\s]*1[2-9]\d{2}(?:[fk]|f|kf)?|i5[-\s]*14400(?:f|kf)?)\b",
    re.IGNORECASE
)
# Ryzen 7 5700/5700X+ (considerar 5800X/5800X3D/5900X/5950X)
_RE_R7_TARGETS = re.compile(
    r"\b(ryzen\s*7\s*(?:5700x|5700|5800x3d|5800x|5900x|5950x))\b",
    re.IGNORECASE
)

# Placas-mãe Intel LGA1700: H610/B660/B760/Z690/Z790
_RE_MOBO_INTEL = re.compile(r"\b(h610|b660|b760|z690|z790)\b", re.IGNORECASE)
# AMD AM4: B550 ou superiores (X570)
_RE_MOBO_AMD_OK = re.compile(r"\b(b550|x570)\b", re.IGNORECASE)
_RE_MOBO_AMD_BLOCK = re.compile(r"\b(a520)\b", re.IGNORECASE)

# GPU
_RE_RTX5060 = re.compile(r"\brtx\s*5060\b", re.IGNORECASE)
_RE_RX7600  = re.compile(r"\brx\s*7600\b", re.IGNORECASE)

# PS5 console e NEGATIVOS (acessórios/jogos)
_RE_PS5_CONSOLE = re.compile(r"\bps5\b|playstation\s*5|console\s*ps5", re.IGNORECASE)
_NEG_PS5 = re.compile(r"\b(capa|case|jogo|midia|mídia|digital|código|code|gift\s*card|dualsense|controle|dock)\b",
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
    # tenta achar números tipo “5 fans”, “6x ventoinhas”
    counts = [int(m.group(1)) for m in _RE_FAN_COUNT.finditer(text)]
    if counts:
        return max(counts)
    # fallback: heurística para “com 5 cooler/fans”
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

    # CPUs:
    # Intel i5 14400F+ (com 12600F/KF incluídos como alvo) <= 900
    if _RE_I5_TARGETS.search(t):
        return (price is not None) and (price <= CPU_PRICE_LIMIT)

    # Ryzen 7 5700/5700X+ <= 900
    if _RE_R7_TARGETS.search(t):
        return (price is not None) and (price <= CPU_PRICE_LIMIT)

    # Motherboards:
    # Intel LGA1700 H610/B660/B760/Z690/Z790 <= 680
    if _RE_MOBO_INTEL.search(t):
        return (price is not None) and (price <= MOBO_PRICE_LIMIT)

    # AMD AM4: B550/X570 <= 680, bloquear A520
    if _RE_MOBO_AMD_BLOCK.search(t):
        return False
    if _RE_MOBO_AMD_OK.search(t):
        return (price is not None) and (price <= MOBO_PRICE_LIMIT)

    # Water Cooler <= 200
    if _RE_WC.search(t):
        return (price is not None) and (price <= WATER_LIMIT)

    # PSU: somente Bronze/Gold, >=600W, <= 350
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

    # Gabinetes:
    if _RE_GAB.search(t):
        fans = count_fans(text)
        # bloquear se < R$150 e tiver 0-4 fans
        if price is not None and price < CASE_BLOCK_PRICE and fans < CASE_MIN_FANS_FOR_ALERT:
            return False
        # alertar se <= 230 e 5+ fans
        if price is not None and price <= CASE_ALERT_PRICE and fans >= CASE_MIN_FANS_FOR_ALERT:
            return True
        return False  # demais casos não alertar

    return False


# ===================== ENVIO VIA BOT API =====================
async def send_via_bot(token: str, chat_id: Union[int, str], text: str):
    """
    Envia o texto exatamente como veio do grupo, sem cabeçalho extra.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": False,
        "parse_mode": None  # não formatar; queremos exatamente o texto
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
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        raise RuntimeError("Sessão inválida: gere nova TELEGRAM_STRING_SESSION.")

    await warm_entity_cache(client)

    monitored = parse_monitored_chats(MONITORED_CHANNELS)
    logger.info(f"▶️ Canais resolvidos: {', '.join(str(x) for x in monitored)}")
    logger.info("✅ Logado — monitorando %d canais…", len(monitored))

    # Pré-resolve para logar problemas sem derrubar
    for peer in monitored:
        try:
            await client.get_input_entity(peer)
        except Exception as e:
            logger.warning(f'Não consegui resolver entidade "{peer}": {e}')

    @client.on(events.NewMessage(chats=monitored))
    async def handler(event: events.NewMessage.Event):
        try:
            text = event.raw_text or ""
            if not text.strip():
                return

            price = extract_reasonable_price(text)

            decision = should_alert(text, price)
            reason = "decision=send" if decision else "ignorado (sem match)"
            logger.info(f"· [{(getattr((await event.get_chat()), 'username', '') or getattr((await event.get_chat()), 'title', '??'))}] "
                        f"{'match' if decision else 'ignorado'} → "
                        f"price={price if price is not None else 'None'} {reason}")

            if decision:
                await send_via_bot(BOT_TOKEN, TARGET_CHAT, text)

        except Exception as e:
            logger.exception(f"Erro no handler: {e}")

    logger.info("▶️ Rodando. Pressione Ctrl+C para sair.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
