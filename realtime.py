# realtime.py
import os
import re
import asyncio
import logging
from typing import Optional, Tuple, List

import requests
from telethon import events
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

# --------------------------------------
# Logging
# --------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# --------------------------------------
# ENV
# --------------------------------------
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
STRING_SESSION = os.getenv("TELEGRAM_STRING_SESSION", "")
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
USER_CHAT_ID = os.getenv("USER_CHAT_ID", "").strip()
MONITORED_CHANNELS_RAW = os.getenv("MONITORED_CHANNELS", "")

if not (API_ID and API_HASH and STRING_SESSION and BOT_TOKEN and USER_CHAT_ID):
    logger.error("Variáveis de ambiente faltando: verifique TELEGRAM_API_ID, TELEGRAM_API_HASH, "
                 "TELEGRAM_STRING_SESSION, TELEGRAM_TOKEN e USER_CHAT_ID.")
    # não encerramos aqui para permitir logs de diagnóstico em ambiente

# --------------------------------------
# Utils — envio via Bot (sem parse_mode)
# --------------------------------------
def send_via_bot(text: str) -> bool:
    token = BOT_TOKEN
    chat_id = USER_CHAT_ID
    if not token or not chat_id:
        logger.error("TELEGRAM_TOKEN/USER_CHAT_ID ausentes.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": int(chat_id),
            "text": text  # sem parse_mode (evita 'unsupported parse_mode')
        }, timeout=15)
        if not resp.ok:
            logger.error(f"Falha ao enviar via bot ({resp.status_code}): {resp.text}")
            return False
        return True
    except Exception as e:
        logger.exception(f"Exceção ao enviar via bot: {e}")
        return False

# --------------------------------------
# Utils — canais do ENV
# --------------------------------------
def parse_monitored_channels(raw: str) -> List[str]:
    """
    Recebe a string do ENV e devolve lista de @usernames válidos.
    Remove IDs numéricos e entradas inválidas.
    """
    if not raw:
        return []
    out = []
    for c in [x.strip() for x in raw.split(",") if x.strip()]:
        c = c if c.startswith("@") else f"@{c}"
        name = c[1:]
        # aceita letras, números, underscores e hifens
        cleaned = name.replace("_", "").replace("-", "")
        if cleaned and cleaned.isalnum():
            out.append(c.lower())
        else:
            logger.warning(f"Canal inválido ignorado: {c}")
    # remove valores obviamente errados (IDs numéricos soltos)
    out = [c for c in out if not c[1:].isdigit()]
    return list(dict.fromkeys(out))  # únicos, preservando ordem

# --------------------------------------
# Extração de preço — robusto para PT-BR
# --------------------------------------
_PRICE_RE = re.compile(
    r"""
    (?:
        (?:\bR\$\s*|\br\$\s*)          # prefixo R$
        (?P<rval>\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d+(?:,\d{2})?)
    )
    |
    (?:
        (?P<plain>\d{1,3}(?:\.\d{3})*(?:,\d{2})?)   # número com separadores
        \s*(?:reais|no\s+pix|pix|parcelado|à\s+vista|avista|cart[aã]o|no\s+cart[aã]o)\b
    )
    """,
    re.IGNORECASE | re.VERBOSE
)

# Para filtrar números de URL
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

def _br_to_float(s: str) -> Optional[float]:
    s = s.strip()
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        val = float(s)
        # faixa plausível para "preço" (evita 2189916 etc)
        if 1 <= val <= 20000:
            return val
    except:
        pass
    return None

def extract_price(text: str) -> Optional[float]:
    """Extrai o menor preço plausível do texto, ignorando URLs/IDs."""
    # remove URLs inteiras antes
    text_wo_urls = _URL_RE.sub(" ", text)
    candidates = []
    for m in _PRICE_RE.finditer(text_wo_urls):
        g = m.group("rval") or m.group("plain")
        val = _br_to_float(g)
        if val is not None:
            candidates.append(val)
    if not candidates:
        return None
    # usar o menor preço encontrado geralmente corresponde ao "preço à vista"
    return min(candidates)

# --------------------------------------
# Regras de produto
# --------------------------------------
# Palavras-chave (lowercase)
KW_GPU_RTX5060 = re.compile(r"\brtx\s*5060\b", re.IGNORECASE)
KW_GPU_RX7600 = re.compile(r"\brx\s*7600\b", re.IGNORECASE)

KW_CPU_I5_BASE = re.compile(r"\bi5[-\s]?(\d{4,5})([a-z]{0,3})\b", re.IGNORECASE)
KW_CPU_RYZEN = re.compile(r"\bryzen\s*([579])\s*(\d{3,4})(x3d|x|g|gt|xt)?\b", re.IGNORECASE)

# Placas-mãe Intel e AMD para os alvos definidos
KW_MB_INTEL = re.compile(r"\b(h610m?|b660|b760|z690|z790)\b", re.IGNORECASE)
KW_MB_AMD_ALLOWED = re.compile(r"\b(b550|x570)\b", re.IGNORECASE)
KW_MB_AMD_BLOCK = re.compile(r"\b(a520)\b", re.IGNORECASE)

# Filtro de linha iCLAMPER
KW_ICLAMPER = re.compile(r"\biclamp(?:er)?\b", re.IGNORECASE)

# Fonte (PSU): 80 Plus Bronze/Gold e potência
KW_PSU_80 = re.compile(r"\b(80\s*\+|80\s*plus)\s*(bronze|gold)\b", re.IGNORECASE)
KW_PSU_WATT = re.compile(r"\b(\d{3,4})\s*w\b", re.IGNORECASE)

# Water cooler
KW_WATER = re.compile(r"\bwater\s*cooler\b", re.IGNORECASE)

# Kits de fans
KW_FANS_KIT = re.compile(r"\b(kit\s+de\s+fans?|kit\s+ventoinhas?|fans?\s+kit|ventoinhas?\s+kit)\b", re.IGNORECASE)
KW_FANS_COUNT = re.compile(r"\b(\d+)\s*(?:fans?|ventoinhas?)\b", re.IGNORECASE)

# Gabinete e contagem de fans
KW_GABINETE = re.compile(r"\bgabine?te\b", re.IGNORECASE)

# Bloquear itens PS5 que não sejam console
KW_PS5_ACCESSORY = re.compile(r"\bps5\b.*\b(capa|case|jogo|m[áa]scara|grip|pel[ií]cula|dock|base|carregador|suporte|cover)\b", re.IGNORECASE)

def is_cpu_match_lower_price(text: str, price: Optional[float]) -> bool:
    """
    CPUs:
    - Intel: i5-14400F ou superior; i5-12600F/KF também entram. Valor <= 900.
    - AMD: Ryzen 7 5700X ou 5700 e superiores. Valor <= 900.
    """
    if price is None or price > 900:
        return False

    # Intel
    for m in KW_CPU_I5_BASE.finditer(text):
        model = m.group(1)  # "14400" etc
        suffix = (m.group(2) or "").lower()  # f, kf, k, etc
        try:
            num = int(model)
        except:
            continue
        # i5-12600F/KF entram
        if num == 12600 and (suffix in ("f", "kf", "")):
            return True
        # >= 14400 qualquer sufixo (mas geralmente 14400F)
        if num >= 14400:
            return True

    # AMD
    for m in KW_CPU_RYZEN.finditer(text):
        gen_main = int(m.group(1))  # 5/7/9
        series = int(m.group(2))    # 5700 etc
        # alvo Ryzen 7 5700/5700X e superiores (inclui 5800X, 5800X3D, 7000...)
        if gen_main >= 7:
            if series >= 5700:
                return True
    return False

def is_motherboard_match(text: str, price: Optional[float]) -> bool:
    """
    Placas-mãe:
    - Intel: H610/H610M, B660, B760, Z690, Z790 — preço <= 680
    - AMD: B550, X570 — preço <= 680
    - Bloquear A520
    """
    if price is None or price > 680:
        return False
    if KW_MB_AMD_BLOCK.search(text):
        return False
    if KW_MB_INTEL.search(text):
        return True
    if KW_MB_AMD_ALLOWED.search(text):
        return True
    return False

def is_gpu_match(text: str, price: Optional[float]) -> bool:
    """
    GPUs: RTX 5060, RX 7600 (sem limite específico de preço).
    """
    return bool(KW_GPU_RTX5060.search(text) or KW_GPU_RX7600.search(text))

def is_psu_match(text: str, price: Optional[float]) -> bool:
    """
    Fontes:
    - 80 Plus Bronze ou Gold
    - Potência >= 600W
    - Preço <= 350
    """
    if price is None or price > 350:
        return False
    if not KW_PSU_80.search(text):
        return False
    # potência
    watts = 0
    for m in KW_PSU_WATT.finditer(text):
        try:
            w = int(m.group(1))
            watts = max(watts, w)
        except:
            continue
    if watts >= 600:
        return True
    return False

def is_watercooler_match(text: str, price: Optional[float]) -> bool:
    """
    Water cooler: somente se preço <= 200
    """
    if not KW_WATER.search(text):
        return False
    return price is not None and price <= 200

def is_fans_kit_match(text: str) -> bool:
    """
    Kits de fans/ventoinhas: 3 a 9 unidades.
    (Sem limite de preço especificado)
    """
    if not KW_FANS_KIT.search(text):
        return False
    # tenta identificar contagem explícita
    counts = [int(m.group(1)) for m in KW_FANS_COUNT.finditer(text) if m.group(1).isdigit()]
    if counts:
        return any(3 <= c <= 9 for c in counts)
    # se não especifica, ainda é kit: alertar (ajuste se preferir)
    return True

def parse_fans_in_case(text: str) -> int:
    """
    Tenta detectar contagem de fans em gabinetes.
    """
    # exemplos: "com 5 fans", "vem 6 ventoinhas", "4x 120mm (fans)"
    counts = [int(m.group(1)) for m in KW_FANS_COUNT.finditer(text) if m.group(1).isdigit()]
    if counts:
        return max(counts)
    # heurística adicional: "5x120mm" etc
    hx = re.findall(r"\b(\d+)\s*x\s*120\s*mm\b", text, flags=re.IGNORECASE)
    if hx:
        try:
            return max(int(x) for x in hx)
        except:
            pass
    return 0

def is_gabinete_match(text: str, price: Optional[float]) -> bool:
    """
    Gabinetes:
      - Se <= 150 e < 5 fans (ou sem fans) => bloquear (não alertar).
      - Se <= 230 e >= 5 fans => alertar.
      - Caso contrário => ignorar.
    """
    if not KW_GABINETE.search(text):
        return False
    if price is None:
        return False
    fans = parse_fans_in_case(text)

    if price <= 150:
        if fans >= 5:
            return True  # raro, mas válido
        return False     # bloqueia gabinetes pelados/baratos

    if price <= 230 and fans >= 5:
        return True

    return False

def is_iclmaper_match(text: str) -> bool:
    return bool(KW_ICLAMPER.search(text))

def is_ps5_accessory(text: str) -> bool:
    return bool(KW_PS5_ACCESSORY.search(text))

def should_alert(text: str) -> Tuple[bool, str]:
    """
    Avalia o texto e decide se deve alertar + motivo.
    """
    if is_ps5_accessory(text):
        return False, "ignorado (acessório/jogo PS5)"

    price = extract_price(text)

    # Ordem de checagem: se qualquer regra bater, alertamos.
    if is_cpu_match_lower_price(text, price):
        return True, f"CPU <= 900 (R$ {price:.2f})" if price is not None else "CPU (sem preço)"

    if is_motherboard_match(text, price):
        return True, f"MB Intel/AMD (<= 680) (R$ {price:.2f})" if price is not None else "MB (sem preço)"

    if is_gpu_match(text, price):
        return True, "GPU alvo (RTX 5060 / RX 7600)"

    if is_psu_match(text, price):
        return True, f"Fonte Bronze/Gold >=600W (<= 350) (R$ {price:.2f})"

    if is_watercooler_match(text, price):
        return True, f"Water cooler <= 200 (R$ {price:.2f})"

    if is_fans_kit_match(text):
        return True, "Kit de fans (3–9)"

    if is_gabinete_match(text, price):
        return True, f"Gabinete OK (<= 230 e >=5 fans) (R$ {price:.2f})"

    return False, "sem match"

# --------------------------------------
# Main (Telethon)
# --------------------------------------
async def resolve_monitored_entities(client: TelegramClient, raw: str):
    usernames = parse_monitored_channels(raw)
    if not usernames:
        logger.warning("MONITORED_CHANNELS vazio — nada será filtrado (handler ouvirá tudo e aplicará regras).")
        return []

    # aquece cache e resolve
    await client.get_dialogs()
    resolved = []
    for uname in usernames:
        try:
            ent = await client.get_entity(uname)
            resolved.append(ent)
        except Exception as e:
            logger.warning(f"Não consegui resolver {uname}: {e}")

    if resolved:
        pretty = []
        for e in resolved:
            uname = getattr(e, "username", None)
            pretty.append(f"@{uname}" if uname else str(getattr(e, "id", "?")))
        logger.info("▶️ Canais resolvidos: " + ", ".join(pretty))
    else:
        logger.info("▶️ Nenhum canal resolvido a partir de MONITORED_CHANNELS.")

    return resolved

async def main():
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.start()
    logger.info("Conectado.")

    monitored_entities = await resolve_monitored_entities(client, MONITORED_CHANNELS_RAW)

    # Handler — se lista vazia, escuta tudo; caso contrário, apenas os canais resolvidos
    @client.on(events.NewMessage(chats=monitored_entities if monitored_entities else None))
    async def handler(event):
        try:
            # pega texto “como veio” (sem adornos)
            raw_text = event.message.message or ""
            if not raw_text.strip():
                return

            # Normalização auxiliar p/ regexes
            text = raw_text
            text_low = text.lower()

            # Decisão
            ok, reason = should_alert(text_low)

            # Log de decisão
            price = extract_price(text)
            price_dbg = f"{price:.2f}" if price is not None else "None"
            src = f"@{getattr(event.chat, 'username', '')}" if getattr(event.chat, 'username', None) else str(getattr(event.chat, 'id', '?'))
            logger.info(f"· [{src:<20}] {'match' if ok else 'ignorado (sem match)'} → price={price_dbg} reason={reason}")

            if not ok:
                return

            # Envia exatamente como está no canal (sem prefixo/cabeçalho adicional)
            sent = send_via_bot(raw_text)
            if not sent:
                logger.error("Falha ao enviar a mensagem via bot.")
        except Exception as e:
            logger.exception(f"Erro no handler: {e}")

    logger.info("▶️ Rodando. Pressione Ctrl+C para sair.")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("✅ Encerrado.")
