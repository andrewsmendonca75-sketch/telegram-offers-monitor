# realtime.py
# -*- coding: utf-8 -*-
import os
import re
import time
import logging
from typing import List, Optional, Tuple, Dict

import requests
from telethon import events
from telethon.sessions import StringSession
from telethon.sync import TelegramClient
from telethon.errors.rpcerrorlist import AuthKeyDuplicatedError

# ---------------------------------------------
# LOGGING
# ---------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

def _pad_channel(ch: str, width: int = 20) -> str:
    if not ch:
        ch = "(desconhecido)"
    if ch != "(desconhecido)" and not ch.startswith("@"):
        ch = "@" + ch
    return f"{ch:<{width}}"

def _log_event(channel: str, action: str, label: str, price: Optional[float], reason: str):
    """
    action: 'MATCH' | 'IGNORADO' | 'BLOQUEADO'
    """
    ch = _pad_channel(channel)
    p = f"{price:.2f}" if isinstance(price, (int, float)) else "None"
    if action == "MATCH":
        logging.info(f"· [{ch}] MATCH    → {label:<22} price={p} reason={reason}")
    elif action == "BLOQUEADO":
        logging.info(f"· [{ch}] BLOQUEADO → {label:<22} price={p} reason={reason}")
    else:
        logging.info(f"· [{ch}] IGNORADO → {label:<22} price={p} reason={reason}")

# ---------------------------------------------
# ENV
# ---------------------------------------------
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
STRING_SESSION = os.environ["TELEGRAM_STRING_SESSION"]
BOT_TOKEN = os.environ["TELEGRAM_TOKEN"]

# Canais a monitorar (usernames separados por vírgula)
MONITORED_CHANNELS_RAW = os.getenv("MONITORED_CHANNELS", "")

# Destinos para envio do alerta (chat_id numérico e/ou @canal onde o bot é admin)
USER_DESTINATIONS_RAW = os.getenv("USER_DESTINATIONS", os.getenv("USER_CHAT_ID", ""))

# Retries no envio do bot
BOT_RETRY = int(os.getenv("BOT_RETRY", "2"))

# ---------------------------------------------
# HELPERS — normalização de listas do ENV
# ---------------------------------------------
def _split_list(val: str) -> List[str]:
    if not val:
        return []
    return [p.strip() for p in val.split(",") if p.strip()]

def _norm_username(u: str) -> Optional[str]:
    if not u:
        return None
    u = u.strip()
    if not u or re.fullmatch(r"\d+", u):
        return None
    u = u.lower()
    if not u.startswith("@"):
        u = "@" + u
    return u

MONITORED_USERNAMES: List[str] = []
for x in _split_list(MONITORED_CHANNELS_RAW):
    nu = _norm_username(x)
    if nu:
        MONITORED_USERNAMES.append(nu)

if not MONITORED_USERNAMES:
    log.warning("MONITORED_CHANNELS vazio — nada será filtrado (handler ouvirá tudo, mas filtrará por 0 canais).")
    log.info("▶️ Canais: (nenhum)")
else:
    log.info(f"▶️ Canais: {', '.join(MONITORED_USERNAMES)}")

USER_DESTINATIONS: List[str] = _split_list(USER_DESTINATIONS_RAW)
if not USER_DESTINATIONS:
    log.warning("USER_DESTINATIONS/USER_CHAT_ID não definido; nada será enviado.")
else:
    log.info(f"📬 Destinos: {', '.join(USER_DESTINATIONS)}")

# ---------------------------------------------
# BOT API
# ---------------------------------------------
BOT_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

def bot_send_text(dest: str, text: str) -> Tuple[bool, str]:
    payload = {
        "chat_id": dest,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(f"{BOT_BASE}/sendMessage", json=payload, timeout=20)
        if r.status_code == 200 and r.json().get("ok"):
            return True, "ok"
        return False, r.text
    except Exception as e:
        return False, repr(e)

def send_alert_to_all(text: str):
    for dest in USER_DESTINATIONS:
        ok, msg = bot_send_text(dest, text)
        if ok:
            log.info("· envio=ok → destino=bot")
        else:
            log.error(f"· ERRO envio via bot: {msg}")
            for _ in range(BOT_RETRY):
                time.sleep(0.6)
                ok, msg = bot_send_text(dest, text)
                if ok:
                    log.info("· envio=ok (retry) → destino=bot")
                    break
            if not ok:
                log.error(f"· Falha ao enviar via bot (depois de retry): {msg}")

# ---------------------------------------------
# PREÇOS — parser BR (ultrarrobusto)
# ---------------------------------------------
# Remove horários (12:34 / 01:02:03) que viram preço 12.34 por engano
TIME_LIKE_RX = re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b")

# Espaços especiais entre "R$" e o número: NBSP, Narrow NBSP, Thin/Hair Space
_CURRENCY_WS = "\u00A0\u202F\u2009\u200A\\s"

PRICE_REGEX = re.compile(
    rf"(?:r\${_CURRENCY_WS}*)?"                              # R$ (opcional) + espaços
    r"("                                                    # captura apenas o número
    r"\d{1,3}(?:[.\s\u00A0\u202F\u2009\u200A]\d{3})*(?:,\d{2})?"  # 1.234,56 com espaços/nbsp
    r"|\d+(?:,\d{2})?"                                      # 179 ou 179,90
    r")",
    re.IGNORECASE
)

def _to_float(num: str) -> Optional[float]:
    s = num
    s = re.sub(r"[.\s\u00A0\u202F\u2009\u200A]", "", s)  # remove milhar e espaços
    s = s.replace(",", ".")
    try:
        v = float(s)
        if 0 < v < 100000:
            return v
    except:
        return None
    return None

def parse_lowest_price_brl(text: str) -> Optional[float]:
    if not text:
        return None
    text = TIME_LIKE_RX.sub("", text)
    matches = list(PRICE_REGEX.finditer(text))
    if not matches:
        return None

    scored = []
    for m in matches:
        full = m.group(0)  # pode conter "R$"
        num = m.group(1)   # só os dígitos
        val = _to_float(num)
        if val is None or val < 5:
            continue
        score = 1
        if full.strip().lower().startswith("r$"):
            score += 2
        scored.append((score, val))

    if not scored:
        return None

    scored.sort(key=lambda x: (-x[0], x[1]))  # melhor score, depois menor valor
    return scored[0][1]

# ---------------------------------------------
# SPLIT — divide mensagens com vários itens
# ---------------------------------------------
SPLIT_BLANKS = re.compile(r"(?:\r?\n\s*\r?\n)+", re.UNICODE)

def split_items(msg: str) -> List[str]:
    if not msg:
        return []
    parts = [p.strip() for p in SPLIT_BLANKS.split(msg) if p.strip()]
    out = []
    for p in parts:
        price_hits = list(PRICE_REGEX.finditer(p))
        if len(price_hits) >= 2:
            current = []
            for line in p.splitlines():
                line = line.strip()
                if not line:
                    continue
                current.append(line)
                if "r$" in line.lower() or PRICE_REGEX.search(line):
                    out.append("\n".join(current).strip())
                    current = []
            if current:
                out.append("\n".join(current).strip())
        else:
            out.append(p)
    return out

# ---------------------------------------------
# REGEX — categorias/produtos
# ---------------------------------------------
# GPU
GPU_TARGET_RE = re.compile(r"\b(?:rtx\s*5060|rx\s*7600)\b", re.IGNORECASE)
GPU_EXCLUDE_RE = re.compile(r"\brtx\s*5050\b", re.IGNORECASE)

# CPU Intel
INTEL_CPU_OK = re.compile(r"""\b(?:i5[-\s]*12(?:600|700)k?f?|i5[-\s]*13(?:400|500|600)k?f?|i5[-\s]*14(?:400|500|600)k?f?|i7[-\s]*1[234](?:700|900)k?f?|i9[-\s]*\d{4,5}k?f?)\b""", re.IGNORECASE)
INTEL_12400F = re.compile(r"\bi5[-\s]*12400f\b", re.IGNORECASE)
INTEL_12600F_KF = re.compile(r"\bi5[-\s]*12600k?f?\b", re.IGNORECASE)
INTEL_14400F = re.compile(r"\bi5[-\s]*14400k?f?\b", re.IGNORECASE)

# CPU AMD (AM4 high-end)
AMD_CPU_OK = re.compile(r"""\b(?:ryzen\s*7\s*5700x?|ryzen\s*7\s*5800x3?d?|ryzen\s*9\s*5900x|ryzen\s*9\s*5950x)\b""", re.IGNORECASE)

# MOBOS
MB_INTEL_RE = re.compile(r"\b(?:h610m?|b660m?|b760m?|z690|z790)\b", re.IGNORECASE)
MB_AMD_B550_RE = re.compile(r"\bb550m?\b", re.IGNORECASE)
MB_AMD_X570_RE = re.compile(r"\bx570\b", re.IGNORECASE)
MB_A520_RE = re.compile(r"\ba520m?\b", re.IGNORECASE)

# Gabinete
GABINETE_RE = re.compile(r"\bgabinete\b", re.IGNORECASE)
FAN_COUNT_RE = re.compile(
    r"""(?:
        (?:(\d+)\s*(?:fans?|coolers?|ventoinhas?)) |
        (?:(\d+)\s*x\s*120\s*mm) |
        (?:(\d+)\s*x\s*fan)
    )""",
    re.IGNORECASE | re.VERBOSE
)

# PSU
PSU_RE = re.compile(r"\b(?:fonte|psu)\b", re.IGNORECASE)
PSU_CERT_RE = re.compile(r"\b(?:80\s*\+?\s*plus\s*)?(?:bronze|gold)\b", re.IGNORECASE)
PSU_WATTS_RE = re.compile(r"\b(\d{3,4})\s*w\b", re.IGNORECASE)

# Water cooler
WATER_COOLER_RE = re.compile(r"\bwater\s*cooler\b", re.IGNORECASE)

# PS5
PS5_CONSOLE_RE = re.compile(r"\bplaystation\s*5\b|\bps5\b", re.IGNORECASE)

# iClamper
ICLAMPER_RE = re.compile(r"\biclamper\b|\bclamp(?:er)?\b", re.IGNORECASE)

# Kit de fans
KIT_FANS_RE = re.compile(r"\b(?:kit\s*(?:de\s*)?(?:fans?|ventoinhas?)|ventoinhas?\s*kit)\b", re.IGNORECASE)

# RAM
RAM_RE = re.compile(r"\b(?:mem[oó]ria\s*ram|ram)\b", re.IGNORECASE)
DDR4_RE = re.compile(r"\bddr4\b", re.IGNORECASE)
GB8_RE = re.compile(r"\b8\s*gb\b", re.IGNORECASE)
GB16_RE = re.compile(r"\b16\s*gb\b", re.IGNORECASE)
MHZ_3200_RE = re.compile(r"\b(?:3200\s*mhz|3200mhz|3200)\b", re.IGNORECASE)

# RAM notebook (bloqueio)
NOTEBOOK_RAM_BLOCK_RE = re.compile(r"\b(?:notebook|laptop|so-?dimm|sodimm|260\s*pin|260p)\b", re.IGNORECASE)

# SSD NVMe M.2 1TB
SSD_RE = re.compile(r"\bssd\b", re.IGNORECASE)
M2_RE = re.compile(r"\b(?:m\.?2|m2|nvme|nv3)\b", re.IGNORECASE)
TB1_RE = re.compile(r"\b1\s*tb\b", re.IGNORECASE)

# Posts não-produto (vídeo / redes)
NON_PRODUCT_HINTS = [
    "saiu vídeo", "saiu video", "vídeo novo", "video novo",
    "assista", "review no youtube", "canal no youtube",
]
BLOCK_URL_SUBSTRS = ["youtube.com", "youtu.be", "shorts/", "tiktok.com", "instagram.com/reel"]

def is_non_product_post(text: str) -> bool:
    blob = (text or "").lower()
    if any(h in blob for h in NON_PRODUCT_HINTS):
        return True
    if any(bad in blob for bad in BLOCK_URL_SUBSTRS):
        return True
    return False

# Teclado Redragon — Kumara + superiores
REDRAGON_RE = re.compile(r"\bredragon\b|\brdgm?\b", re.IGNORECASE)
KUMARA_RE = re.compile(r"\bkumara\b|\bk-?552\b|\bk-?552rgb\b|\bk-?552rgb-?1\b|\bk-?552rgb-?pro\b|\bkumara\s*pro\b", re.IGNORECASE)
REDRAGON_SUPERIOR_RE = re.compile(
    r"(\bdevarajas\b|\bk-?556\b|"
    r"\bsurara\b|\bk-?582\b|"
    r"\bdraconic\b|\bk-?530\b|"
    r"\bcastor\b|\bk-?631\b|\bk-?631\s*pro\b|"
    r"\bpollux\b|\bk-?628\b|"
    r"\bel[fv]\b|\bel[fv]\s*pro\b|\bk-?649\b|"
    r"\bk-?632\b|\bk-?633\b|\bk-?617\b|\bsindri\b)",
    re.IGNORECASE
)

# ---------------------------------------------
# Utils
# ---------------------------------------------
def count_fans(text: str) -> int:
    count = 0
    for m in FAN_COUNT_RE.finditer(text):
        nums = [n for n in m.groups() if n]
        for n in nums:
            try:
                v = int(n)
                count = max(count, v)
            except:
                pass
    return count

def product_label(text: str) -> str:
    t = text.lower()

    if GPU_TARGET_RE.search(t):
        return "GPU (RTX 5060 / RX 7600)"
    if INTEL_14400F.search(t) or INTEL_12600F_KF.search(t) or INTEL_12400F.search(t) or INTEL_CPU_OK.search(t):
        return "CPU Intel"
    if AMD_CPU_OK.search(t):
        return "CPU AMD"
    if MB_AMD_B550_RE.search(t):
        return "MOBO B550"
    if MB_AMD_X570_RE.search(t):
        return "MOBO X570"
    if MB_INTEL_RE.search(t):
        return "MOBO LGA1700"
    if GABINETE_RE.search(t):
        return "Gabinete"
    if PSU_RE.search(t):
        return "Fonte (PSU)"
    if WATER_COOLER_RE.search(t):
        return "Water Cooler"
    if PS5_CONSOLE_RE.search(t):
        return "Console PS5"
    if ICLAMPER_RE.search(t):
        return "Filtro de linha (iClamper)"
    if KIT_FANS_RE.search(t):
        return "Kit de Fans"
    if RAM_RE.search(t) and DDR4_RE.search(t) and MHZ_3200_RE.search(t):
        if NOTEBOOK_RAM_BLOCK_RE.search(t):
            return "RAM DDR4 3200 (notebook)"
        if GB16_RE.search(t):
            return "RAM DDR4 16GB 3200"
        if GB8_RE.search(t):
            return "RAM DDR4 8GB 3200"
        return "RAM DDR4 3200"
    if SSD_RE.search(t) and M2_RE.search(t) and TB1_RE.search(t):
        return "SSD NVMe M.2 1TB"
    if REDRAGON_RE.search(t):
        return "Teclado Redragon"
    return "Produto"

# ---------------------------------------------
# MATCH LOGIC — retorna ação
# ---------------------------------------------
def classify(text: str) -> Tuple[str, str, str, Optional[float]]:
    """
    Retorna (action, reason, label, price)
    action: 'ALERT' | 'BLOQUEADO' | 'IGNORADO'
    """
    t = text or ""
    price = parse_lowest_price_brl(t)

    # 0) Corta posts de vídeo/redes
    if is_non_product_post(t):
        return "IGNORADO", "Post de vídeo/YouTube — ignorar", "Produto", price

    # 1) Teclados Redragon — Kumara e superiores
    if REDRAGON_RE.search(t):
        if KUMARA_RE.search(t):
            return "ALERT", "Kumara (K552) — alertar sempre", "Redragon Kumara", price

        if REDRAGON_SUPERIOR_RE.search(t):
            if price is not None and price <= 160.0:
                return "ALERT", "Redragon superior ≤ R$160", "Redragon superior", price
            else:
                return "BLOQUEADO", "Redragon superior > R$160 — bloquear", "Redragon superior", price

        if price is not None and price > 160.0:
            return "BLOQUEADO", "Redragon não-Kumara > R$160 — bloquear", "Teclado Redragon", price
        else:
            return "IGNORADO", "Redragon fora dos critérios", "Teclado Redragon", price

    # 2) GPU alvo (RTX 5060 / RX 7600), excluir 5050, preço plausível
    if GPU_EXCLUDE_RE.search(t):
        return "IGNORADO", "RTX 5050 explicitamente ignorado", "Produto", price
    if GPU_TARGET_RE.search(t):
        if price is None or price < 500 or price > 8000:
            return "IGNORADO", f"Preço inválido/ausente para GPU alvo (price={price})", "GPU (RTX 5060 / RX 7600)", price
        return "ALERT", "GPU match (RTX 5060 / RX 7600)", "GPU (RTX 5060 / RX 7600)", price

    # 3) CPU Intel ≤ 900
    if INTEL_14400F.search(t) or INTEL_12600F_KF.search(t) or INTEL_12400F.search(t) or INTEL_CPU_OK.search(t):
        if price is not None and price <= 900:
            return "ALERT", f"CPU <= 900 (R$ {price:.2f})", "CPU Intel", price
        return "IGNORADO", "CPU Intel, mas preço > 900 ou ausente", "CPU Intel", price

    # 4) CPU AMD ≤ 900
    if AMD_CPU_OK.search(t):
        if price is not None and price <= 900:
            return "ALERT", f"CPU <= 900 (R$ {price:.2f})", "CPU AMD", price
        return "IGNORADO", "CPU AMD, mas preço > 900 ou ausente", "CPU AMD", price

    # 5) MOBOS
    if MB_A520_RE.search(t):
        return "IGNORADO", "A520 bloqueada", "MOBO A520", price

    if MB_AMD_B550_RE.search(t):
        if price is not None and price <= 550:
            return "ALERT", f"MOBO B550 ≤ 550 (R$ {price:.2f})", "MOBO B550", price
        return "IGNORADO", "MOBO B550, mas preço > 550 ou ausente", "MOBO B550", price

    if MB_AMD_X570_RE.search(t):
        if price is not None and price <= 680:
            return "ALERT", f"MOBO X570 ≤ 680 (R$ {price:.2f})", "MOBO X570", price
        return "IGNORADO", "MOBO X570, mas preço > 680 ou ausente", "MOBO X570", price

    if MB_INTEL_RE.search(t):
        if price is not None and price <= 680:
            return "ALERT", f"MOBO LGA1700 ≤ 680 (R$ {price:.2f})", "MOBO LGA1700", price
        return "IGNORADO", "MOBO LGA1700, mas preço > 680 ou ausente", "MOBO LGA1700", price

    # 6) Gabinete
    if GABINETE_RE.search(t):
        fans = count_fans(t)
        if price is None:
            return "IGNORADO", "Gabinete sem preço", "Gabinete", price
        if fans >= 4 and price <= 180:
            return "ALERT", f"Gabinete ok: {fans} fans ≤ R$ 180 (R$ {price:.2f})", "Gabinete", price
        if fans >= 5 and price <= 230:
            return "ALERT", f"Gabinete ok: {fans} fans ≤ R$ 230 (R$ {price:.2f})", "Gabinete", price
        if fans < 5 and price < 150:
            return "IGNORADO", "Gabinete bloqueado: <5 fans e preço < 150", "Gabinete", price
        return "IGNORADO", "Gabinete fora das regras", "Gabinete", price

    # 7) PSU — Bronze/Gold, ≥600W, ≤350
    if PSU_RE.search(t):
        cert_ok = PSU_CERT_RE.search(t) is not None
        watts = None
        m = PSU_WATTS_RE.search(t)
        if m:
            try:
                watts = int(m.group(1))
            except:
                watts = None
        if cert_ok and watts and watts >= 600 and price is not None and price <= 350:
            cert = PSU_CERT_RE.search(t).group(0)
            return "ALERT", f"PSU ok: {watts}W {cert} ≤ R$ 350 (R$ {price:.2f})", "Fonte (PSU)", price
        return "IGNORADO", "PSU fora das regras", "Fonte (PSU)", price

    # 8) Water cooler < 200
    if WATER_COOLER_RE.search(t):
        if price is not None and price < 200:
            return "ALERT", f"Water cooler < 200 (R$ {price:.2f})", "Water Cooler", price
        return "IGNORADO", "Water cooler >= 200 ou sem preço", "Water Cooler", price

    # 9) PS5
    if PS5_CONSOLE_RE.search(t):
        return "ALERT", "PS5 console", "Console PS5", price

    # 10) iClamper
    if ICLAMPER_RE.search(t):
        return "ALERT", "iClamper", "Filtro de linha (iClamper)", price

    # 11) Kit de fans 3..9
    if KIT_FANS_RE.search(t):
        nums = re.findall(r"\b([3-9])\b", t)
        if nums:
            return "ALERT", f"Kit de fans ({'/'.join(nums)} un.)", "Kit de Fans", price
        fans = count_fans(t)
        if 3 <= fans <= 9:
            return "ALERT", f"Kit de fans ({fans} un.)", "Kit de Fans", price
        return "IGNORADO", "Kit de fans sem quantidade clara (3-9)", "Kit de Fans", price

    # 12) RAM DDR4 3200 — Desktop only
    if RAM_RE.search(t) and DDR4_RE.search(t) and MHZ_3200_RE.search(t):
        if NOTEBOOK_RAM_BLOCK_RE.search(t):
            return "IGNORADO", "RAM notebook/SODIMM — ignorar", "RAM DDR4 3200 (notebook)", price
        if price is not None:
            if GB8_RE.search(t) and price <= 180:
                return "ALERT", f"RAM DDR4 8GB 3200 ≤ 180 (R$ {price:.2f})", "RAM DDR4 8GB 3200", price
            if GB16_RE.search(t) and price <= 300:
                return "ALERT", f"RAM DDR4 16GB 3200 ≤ 300 (R$ {price:.2f})", "RAM DDR4 16GB 3200", price
        return "IGNORADO", "RAM DDR4 3200 fora do teto ou sem preço", "RAM DDR4 3200", price

    # 13) SSD NVMe M.2 1TB ≤ 460
    if SSD_RE.search(t) and M2_RE.search(t) and TB1_RE.search(t):
        if price is not None and price <= 460:
            return "ALERT", f"SSD NVMe M.2 1TB ≤ 460 (R$ {price:.2f})", "SSD NVMe M.2 1TB", price
        return "IGNORADO", "SSD NVMe M.2 1TB > 460 ou sem preço", "SSD NVMe M.2 1TB", price

    return "IGNORADO", "sem match", "Produto", price

# ---------------------------------------------
# Mensagem enviada ao bot (inclui rodapé com canal)
# ---------------------------------------------
def format_bot_message(raw_text: str, channel: str) -> str:
    ch = channel or ""
    if ch and not ch.startswith("@"):
        ch = "@" + ch
    footer = f"\n\nFonte: {ch}" if ch else ""
    return f"{raw_text}{footer}"

# ---------------------------------------------
# ANTI-DUP / LOCKFILE
# ---------------------------------------------
class LRUSeen:
    def __init__(self, maxlen: int = 400):
        self.maxlen = maxlen
        self.set: Dict[int, float] = {}

    def seen(self, msg_id: int) -> bool:
        if msg_id in self.set:
            return True
        if len(self.set) > self.maxlen:
            items = sorted(self.set.items(), key=lambda kv: kv[1])[: self.maxlen // 2]
            for k, _ in items:
                self.set.pop(k, None)
        self.set[msg_id] = time.time()
        return False

seen_cache = LRUSeen(400)

# lockfile simples para evitar 2 instâncias simultâneas
LOCK_PATH = "/tmp/realtime_session.lock"
def acquire_lock() -> Optional[int]:
    try:
        import fcntl
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except Exception:
        return None

# ---------------------------------------------
# MAIN
# ---------------------------------------------
def main():
    # Evita 2 processos simultâneos usando a MESMA sessão
    lock_fd = acquire_lock()
    if lock_fd is None:
        log.error("Outra instância já está rodando com esta sessão. Abortando para evitar AuthKeyDuplicatedError.")
        return

    log.info("Conectando ao Telegram...")
    try:
        with TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH) as client:
            log.info("Conectado.")

            # Warm cache
            dialogs = client.get_dialogs()
            username_to_entity = {}
            for d in dialogs:
                try:
                    if d.entity and getattr(d.entity, "username", None):
                        username_to_entity["@{}".format(d.entity.username.lower())] = d.entity
                except Exception:
                    pass

            # Resolve canais (com fallback get_entity, mesmo sem estar inscrito)
            resolved_entities = []
            for uname in MONITORED_USERNAMES:
                ent = username_to_entity.get(uname)
                if ent is None:
                    try:
                        ent = client.get_entity(uname)
                    except Exception:
                        ent = None
                if ent is None:
                    log.warning(f"Canal não encontrado (ignorado): {uname}")
                else:
                    resolved_entities.append(ent)

            if resolved_entities:
                log.info("▶️ Canais resolvidos: " + ", ".join(
                    f"@{getattr(e, 'username', '')}" for e in resolved_entities if getattr(e, "username", None)
                ))
            else:
                log.info("▶️ Canais resolvidos: ")
            log.info(f"✅ Logado — monitorando {len(resolved_entities)} canais…")
            log.info("▶️ Rodando. Pressione Ctrl+C para sair.")

            @client.on(events.NewMessage(chats=resolved_entities if resolved_entities else None))
            async def handler(event):
                try:
                    if seen_cache.seen(event.id):
                        return

                    raw_text = (event.raw_text or "").strip()
                    if not raw_text:
                        return

                    chan = getattr(event.chat, "username", None)
                    chan_disp = f"@{chan}" if chan else "(desconhecido)"

                    # Divide a mensagem em itens e processa cada um
                    for chunk in split_items(raw_text):
                        action, reason, label, price = classify(chunk)

                        # LOG por item
                        _log_event(
                            chan_disp,
                            "MATCH" if action == "ALERT" else ("BLOQUEADO" if action == "BLOQUEADO" else "IGNORADO"),
                            label, price, reason
                        )

                        # Envia somente nos itens que deram ALERT
                        if action == "ALERT":
                            msg = format_bot_message(chunk, chan or "")
                            send_alert_to_all(msg)

                except Exception as e:
                    log.exception(f"Handler error: {e}")

            client.run_until_disconnected()

    except AuthKeyDuplicatedError:
        log.error("AuthKeyDuplicatedError: a mesma StringSession está ativa em outro host/instância. Encerre a outra ou gere nova STRING_SESSION.")
        return

if __name__ == "__main__":
    main()
