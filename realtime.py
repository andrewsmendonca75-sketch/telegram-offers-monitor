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

# ---------------------------------------------
# LOGGING
# ---------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

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
    """Envia texto via Bot API, sem parse_mode para evitar 'unsupported parse_mode'."""
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
# PREÇOS — parser BR
# ---------------------------------------------
PRICE_REGEX = re.compile(
    r"""
    (?:
        (?:r\$\s*)?
        (?:
            \d{1,3}(?:\.\d{3})+(?:,\d{2})? |
            \d+(?:,\d{2})? |
            \d+\.\d{2}
        )
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

def _to_float(num: str) -> Optional[float]:
    s = re.sub(r"\s+", "", num.strip())
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        if "." in s:
            parts = s.split(".")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() and len(parts[1]) == 3:
                s = parts[0] + parts[1]
            elif len(parts) > 2:
                s = s.replace(".", "")
    try:
        v = float(s)
        if 0 < v < 100000:
            return v
    except:
        return None
    return None

def parse_lowest_price_brl(text: str) -> Optional[float]:
    vals: List[float] = []
    for m in PRICE_REGEX.finditer(text):
        raw = m.group(0)
        raw_num = re.sub(r"(?i)^r\$\s*", "", raw).strip()
        v = _to_float(raw_num)
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    cleaned = [v for v in vals if v >= 5.0]
    if not cleaned:
        cleaned = vals
    return min(cleaned) if cleaned else None

# ---------------------------------------------
# REGEX — categorias/produtos
# ---------------------------------------------
# GPU
GPU_RE = re.compile(r"\b(?:rtx\s*5060|rx\s*7600)\b", re.IGNORECASE)

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

# Teclado Redragon
REDRAGON_KB_RE = re.compile(r"\bredragon\b", re.IGNORECASE)
KUMARA_RE = re.compile(r"\bkumara\b", re.IGNORECASE)

# RAM
RAM_RE = re.compile(r"\b(?:mem[oó]ria\s*ram|ram)\b", re.IGNORECASE)
DDR4_RE = re.compile(r"\bddr4\b", re.IGNORECASE)
GB8_RE = re.compile(r"\b8\s*gb\b", re.IGNORECASE)
GB16_RE = re.compile(r"\b16\s*gb\b", re.IGNORECASE)
MHZ_3200_RE = re.compile(r"\b(?:3200\s*mhz|3200mhz|3200)\b", re.IGNORECASE)

# SSD NVMe M.2 1TB
SSD_RE = re.compile(r"\bssd\b", re.IGNORECASE)
M2_RE = re.compile(r"\b(?:m\.?2|m2|nvme|nvme|nv3)\b", re.IGNORECASE)
TB1_RE = re.compile(r"\b1\s*tb\b", re.IGNORECASE)

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

    # ordem por prioridade
    if GPU_RE.search(t):
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
        if GB16_RE.search(t):
            return "RAM DDR4 16GB 3200"
        if GB8_RE.search(t):
            return "RAM DDR4 8GB 3200"
        return "RAM DDR4 3200"
    if SSD_RE.search(t) and M2_RE.search(t) and TB1_RE.search(t):
        return "SSD NVMe M.2 1TB"
    return "Produto"

# ---------------------------------------------
# MATCH LOGIC
# ---------------------------------------------
def should_alert(text: str) -> Tuple[bool, str, str]:
    """
    Retorna (ok, reason, label)
    """
    t = text
    price = parse_lowest_price_brl(t)

    # GPU
    if GPU_RE.search(t):
        return True, "GPU match (RTX 5060 / RX 7600)", "GPU (RTX 5060 / RX 7600)"

    # CPU Intel <= 900
    if INTEL_14400F.search(t) or INTEL_12600F_KF.search(t) or INTEL_12400F.search(t) or INTEL_CPU_OK.search(t):
        if price is not None and price <= 900:
            return True, f"CPU <= 900 (R$ {price:.2f})", "CPU Intel"
        return False, "CPU Intel, mas preço > 900 ou ausente", "CPU Intel"

    # CPU AMD <= 900
    if AMD_CPU_OK.search(t):
        if price is not None and price <= 900:
            return True, f"CPU <= 900 (R$ {price:.2f})", "CPU AMD"
        return False, "CPU AMD, mas preço > 900 ou ausente", "CPU AMD"

    # MOBOS
    # Bloqueio A520
    if MB_A520_RE.search(t):
        return False, "A520 bloqueada", "MOBO A520"

    # B550 ≤ 550 (pedido)
    if MB_AMD_B550_RE.search(t):
        if price is not None and price <= 550:
            return True, f"MOBO B550 ≤ 550 (R$ {price:.2f})", "MOBO B550"
        return False, "MOBO B550, mas preço > 550 ou ausente", "MOBO B550"

    # X570 ≤ 680 (mantido)
    if MB_AMD_X570_RE.search(t):
        if price is not None and price <= 680:
            return True, f"MOBO X570 ≤ 680 (R$ {price:.2f})", "MOBO X570"
        return False, "MOBO X570, mas preço > 680 ou ausente", "MOBO X570"

    # Intel LGA1700 ≤ 680 (mantido)
    if MB_INTEL_RE.search(t):
        if price is not None and price <= 680:
            return True, f"MOBO LGA1700 ≤ 680 (R$ {price:.2f})", "MOBO LGA1700"
        return False, "MOBO LGA1700, mas preço > 680 ou ausente", "MOBO LGA1700"

    # Gabinete
    if GABINETE_RE.search(t):
        fans = count_fans(t)
        if price is None:
            return False, "Gabinete sem preço", "Gabinete"
        # Novo: ≥4 fans ≤ 180
        if fans >= 4 and price <= 180:
            return True, f"Gabinete ok: {fans} fans ≤ R$ 180 (R$ {price:.2f})", "Gabinete"
        # Antigo: ≥5 fans ≤ 230
        if fans >= 5 and price <= 230:
            return True, f"Gabinete ok: {fans} fans ≤ R$ 230 (R$ {price:.2f})", "Gabinete"
        # Bloqueio: <5 fans e <150
        if fans < 5 and price < 150:
            return False, "Gabinete bloqueado: <5 fans e preço < 150", "Gabinete"
        return False, "Gabinete fora das regras", "Gabinete"

    # PSU — Bronze/Gold, ≥600W, ≤350
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
            return True, f"PSU ok: {watts}W {cert} ≤ R$ 350 (R$ {price:.2f})", "Fonte (PSU)"
        return False, "PSU fora das regras", "Fonte (PSU)"

    # Water cooler < 200
    if WATER_COOLER_RE.search(t):
        if price is not None and price < 200:
            return True, f"Water cooler < 200 (R$ {price:.2f})", "Water Cooler"
        return False, "Water cooler >= 200 ou sem preço", "Water Cooler"

    # PS5
    if PS5_CONSOLE_RE.search(t):
        return True, "PS5 console", "Console PS5"

    # iClamper
    if ICLAMPER_RE.search(t):
        return True, "iClamper", "Filtro de linha (iClamper)"

    # Kit de fans 3..9
    if KIT_FANS_RE.search(t):
        nums = re.findall(r"\b([3-9])\b", t)
        if nums:
            return True, f"Kit de fans ({'/'.join(nums)} un.)", "Kit de Fans"
        fans = count_fans(t)
        if 3 <= fans <= 9:
            return True, f"Kit de fans ({fans} un.)", "Kit de Fans"
        return False, "Kit de fans sem quantidade clara (3-9)", "Kit de Fans"

    # RAM DDR4 3200 — 8GB ≤ 180; 16GB ≤ 300
    if RAM_RE.search(t) and DDR4_RE.search(t) and MHZ_3200_RE.search(t):
        if price is not None:
            if GB8_RE.search(t) and price <= 180:
                return True, f"RAM DDR4 8GB 3200 ≤ 180 (R$ {price:.2f})", "RAM DDR4 8GB 3200"
            if GB16_RE.search(t) and price <= 300:
                return True, f"RAM DDR4 16GB 3200 ≤ 300 (R$ {price:.2f})", "RAM DDR4 16GB 3200"
        return False, "RAM DDR4 3200 fora do teto ou sem preço", "RAM DDR4 3200"

    # SSD NVMe M.2 1TB ≤ 460
    if SSD_RE.search(t) and M2_RE.search(t) and TB1_RE.search(t):
        if price is not None and price <= 460:
            return True, f"SSD NVMe M.2 1TB ≤ 460 (R$ {price:.2f})", "SSD NVMe M.2 1TB"
        return False, "SSD NVMe M.2 1TB > 460 ou sem preço", "SSD NVMe M.2 1TB"

    return False, "sem match", "Produto"

# ---------------------------------------------
# ANTI-DUP
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

# ---------------------------------------------
# MAIN
# ---------------------------------------------
def main():
    log.info("Conectando ao Telegram...")
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

                ok, reason, label = should_alert(raw_text)
                chan = getattr(event.chat, "username", None)
                chan_disp = f"@{chan}" if chan else "(desconhecido)"

                price = parse_lowest_price_brl(raw_text)

                if ok:
                    if price is not None:
                        log.info(f"· [{chan_disp: <20}] MATCH    → {label: <22} price={price:.2f} reason={reason}")
                    else:
                        log.info(f"· [{chan_disp: <20}] MATCH    → {label: <22} price=None reason={reason}")
                    # envia exatamente o texto do post
                    send_alert_to_all(raw_text)
                else:
                    if price is not None:
                        log.info(f"· [{chan_disp: <20}] IGNORADO → {label: <22} price={price:.2f} reason={reason}")
                    else:
                        log.info(f"· [{chan_disp: <20}] IGNORADO → {label: <22} price=None reason={reason}")

            except Exception as e:
                log.exception(f"Handler error: {e}")

        client.run_until_disconnected()

if __name__ == "__main__":
    main()
