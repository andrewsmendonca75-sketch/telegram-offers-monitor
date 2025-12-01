# monitor_updated_full_with_debug.py
# -*- coding: utf-8 -*-
"""
Realtime Telegram monitor (Telethon) - versão ajustada com debug
- Logs detalhados de candidatos de preço e avaliação de regras
- TV apenas 40" ou maior (>=40) e <= 1000
- TV Box separado (<=200)
- SSD Kingston M.2 1TB <=400
- RAM 16GB DDR4 3200 (qualquer marca) <=300
- Placas-mãe LGA1700/B760 <=600 (>=300 sanity)
- Cupom tratado com cuidado para não invalidar preços legítimos
"""

import os
import re
import time
import json
import atexit
import signal
import logging
import threading
from typing import List, Optional, Tuple, Dict
from datetime import datetime
import requests
from telethon import events
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

# ---------------------------------------------
# CONFIG / ENV
# ---------------------------------------------
START_TS = datetime.utcnow().isoformat() + "Z"
PID = os.getpid()

RETRY_SEND_ATTEMPTS = 3
RETRY_SEND_BACKOFF = 1.0  # seconds, will multiply

PERSIST_SEEN_FILE = os.getenv("PERSIST_SEEN_FILE", "/tmp/monitor_seen.json")
PERSIST_MATCH_LOG = os.getenv("PERSIST_MATCH_LOG", "/tmp/monitor_matches.log")
HEALTH_FILE = os.getenv("HEALTH_FILE", "/tmp/monitor_health")

# Required envs
missing = []
try:
    API_ID = int(os.environ.get("TELEGRAM_API_ID", ""))
except Exception:
    API_ID = None
if not API_ID: missing.append("TELEGRAM_API_ID")

API_HASH = os.environ.get("TELEGRAM_API_HASH") or ""
if not API_HASH: missing.append("TELEGRAM_API_HASH")

STRING_SESSION = os.environ.get("TELEGRAM_STRING_SESSION") or ""
if not STRING_SESSION: missing.append("TELEGRAM_STRING_SESSION")

BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN") or ""
if not BOT_TOKEN: missing.append("TELEGRAM_TOKEN")

MONITORED_CHANNELS_RAW = os.getenv("MONITORED_CHANNELS", "")
USER_DESTINATIONS_RAW = os.getenv("USER_DESTINATIONS", os.getenv("USER_CHAT_ID", ""))

if missing:
    raise RuntimeError("Missing required envs: " + ", ".join(missing))

# ---------------------------------------------
# LOGGING
# ---------------------------------------------
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s | %(levelname)5s | %(message)s",
)
log = logging.getLogger("monitor")

log.info("▶️ Starting realtime monitor pid=%s ts=%s", PID, START_TS)

# ---------------------------------------------
# UTIL: CSV / Normalização
# ---------------------------------------------
def _split_csv(val: str) -> List[str]:
    if not val: return []
    return [p.strip() for p in val.split(",") if p and p.strip()]

def _norm_username(u: str) -> Optional[str]:
    if not u: return None
    u = u.strip()
    if not u: return None
    if re.fullmatch(r"-?\d+", u):
        return None
    u = u.lower()
    if not u.startswith("@"):
        u = "@" + u
    return u

MONITORED_USERNAMES: List[str] = []
for x in _split_csv(MONITORED_CHANNELS_RAW):
    nu = _norm_username(x)
    if nu:
        MONITORED_USERNAMES.append(nu)

if not MONITORED_USERNAMES:
    log.warning("MONITORED_CHANNELS vazio — nada será filtrado por username.")
else:
    log.info("▶️ Canais: %s", ", ".join(MONITORED_USERNAMES))

USER_DESTINATIONS: List[str] = _split_csv(USER_DESTINATIONS_RAW)
if not USER_DESTINATIONS:
    log.warning("USER_DESTINATIONS/USER_CHAT_ID não definido; nada será enviado.")
else:
    log.info("📬 Destinos: %s", ", ".join(USER_DESTINATIONS))

BOT_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ---------------------------------------------
# BOT SEND WITH RETRIES (requests)
# ---------------------------------------------
_send_lock = threading.Lock()

def bot_send_text(dest: str, text: str) -> Tuple[bool, str]:
    """Synchronous send via Bot API with retries and backoff."""
    payload = {"chat_id": dest, "text": text, "disable_web_page_preview": True}
    attempt = 0
    backoff = RETRY_SEND_BACKOFF
    last_err = None
    while attempt < RETRY_SEND_ATTEMPTS:
        try:
            with _send_lock:
                r = requests.post(f"{BOT_BASE}/sendMessage", json=payload, timeout=20)
            if r.status_code == 200:
                j = r.json()
                if j.get("ok"):
                    return True, "ok"
                last_err = f"api-error: {r.text}"
            else:
                last_err = f"status={r.status_code} text={r.text}"
        except Exception as e:
            last_err = repr(e)
        attempt += 1
        log.debug("bot_send_text retry %d/%d -> %s", attempt, RETRY_SEND_ATTEMPTS, last_err)
        time.sleep(backoff)
        backoff *= 2
    return False, last_err or "unknown-error"

def notify_all(text: str):
    for d in USER_DESTINATIONS:
        ok, msg = bot_send_text(d, text)
        if ok:
            log.info("· envio=ok → %s", d)
        else:
            log.error("· envio=ERRO → %s | motivo=%s", d, msg)

# ---------------------------------------------
# PRICE PARSER (BRL) - robust context-aware + debug
# ---------------------------------------------
PRICE_PIX_RE = re.compile(
    r"(?i)r\$\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{1,2})?)\s*(?:no\s*pix|à\s*vista|a\s*vista|à\s*vista:|avista)",
    re.I
)
PRICE_FALLBACK_RE = re.compile(r"(?i)r\$\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{1,2})?)", re.I)
URL_RE = re.compile(r"https?://\S+", re.I)

# two-level negative indicators:
SMALL_NEG_RE = re.compile(
    r"(?i)\b(off|off:|desconto|desconto:|cupom|cupom:|resgate|x\s*de|parcelas?|parcelado|parcelamento)\b"
)
BIG_NEG_RE = re.compile(r"(?i)\b(cashback|pontos?|reembolso|voucher)\b", re.I)

def _to_float_brl(raw: str) -> Optional[float]:
    s = raw.strip().replace(".", "").replace(",", ".")
    try:
        v = float(s)
        if v <= 0 or v < 0.5 or v > 5_000_000:
            return None
        return v
    except Exception:
        return None

def find_lowest_price(text: str) -> Optional[float]:
    """Find plausible lowest price in text, ignoring coupon/off values when they are adjacent.

    Additional behavior: logs candidate prices and reasons for debugging.
    """
    if not text:
        return None
    txt = URL_RE.sub(" ", text)
    vals: List[float] = []
    candidates = []  # tuples: (raw_string, span_start, span_end, parsed_value_or_None, reason)

    def valid_context(m):
        start = m.start(); end = m.end()
        s_start = max(0, start - 12); s_end = min(len(txt), end + 12)
        small_ctx = txt[s_start:s_end]
        small_bad = SMALL_NEG_RE.search(small_ctx)
        if small_bad:
            token = small_bad.group(0).lower()
            # treat 'cupom' specially: only reject if it appears BEFORE the number (adjacent)
            if "cupom" in token:
                pos = txt.find(small_bad.group(0), s_start, s_end)
                if pos != -1 and pos < start:
                    return False
                # if 'cupom' is after the number, do not reject here
            else:
                return False
        b_start = max(0, start - 80); b_end = min(len(txt), end + 80)
        big_ctx = txt[b_start:b_end]
        if BIG_NEG_RE.search(big_ctx):
            return False
        return True

    # explicit à vista / pix first
    for m in PRICE_PIX_RE.finditer(txt):
        raw = m.group(1)
        parsed = _to_float_brl(raw)
        ok_ctx = valid_context(m)
        if parsed is not None and parsed >= 10 and ok_ctx:
            vals.append(parsed)
            candidates.append((raw, m.start(), m.end(), parsed, "pix-accepted"))
        else:
            reason = "pix-rejected"
            if parsed is None:
                reason += ":no-parse"
            elif parsed < 10:
                reason += ":too-small"
            elif not ok_ctx:
                reason += ":ctx-reject"
            candidates.append((raw, m.start(), m.end(), parsed, reason))

    # fallback any R$
    if not vals:
        for m in PRICE_FALLBACK_RE.finditer(txt):
            raw = m.group(1)
            parsed = _to_float_brl(raw)
            ok_ctx = valid_context(m)
            if parsed is not None and parsed >= 10 and ok_ctx:
                vals.append(parsed)
                candidates.append((raw, m.start(), m.end(), parsed, "fallback-accepted"))
            else:
                rej = "no-parse" if parsed is None else ("too-small" if parsed is not None and parsed < 10 else "ctx-reject")
                candidates.append((raw, m.start(), m.end(), parsed, "fallback-rejected:" + rej))

    # Log candidates for debugging
    try:
        if os.getenv("LOG_PRICE_CANDIDATES", "1") == "1":
            for raw, s, e, parsed, reason in candidates:
                log.info("PRICE_CANDIDATE | raw=%s | span=(%d-%d) | parsed=%s | reason=%s", raw, s, e, str(parsed), reason)
    except Exception:
        log.exception("Erro ao logar price candidates")

    return min(vals) if vals else None

# ---------------------------------------------
# REGEX RULES (updated)
# ---------------------------------------------
BLOCK_CATS = re.compile(r"\b(celular|smartphone|iphone|android|notebook|laptop|macbook|geladeira|refrigerador|m[aá]quina\s*de\s*lavar|lavadora|lava\s*e\s*seca)\b", re.I)
PC_GAMER_RE = re.compile(r"\b(pc\s*gamer|setup\s*completo|kit\s*completo)\b", re.I)

# TV box: specific boxes only
TVBOX_RE = re.compile(r"\b(?:tv\s*box|xiaomi\s*box|mi\s*box|mi-box|android\s*tv\s*box)\b", re.I)
# TV generic mentions
TV_RE = re.compile(r"\b(?:tv|smart\s*tv|televis(?:ão|ao))\b", re.I)
# TV sizes — only 40 or larger
TV_SIZE_RE = re.compile(r"\b(40|41|42|43|44|45|48|49|50|55|58|60|65|70|75|77|80)\s*(?:\"|\'|pol|polegadas?)\b", re.I)

# Monitors
MONITOR_SMALL_RE = re.compile(r"\b(19|20|21|22|23|24|25|26)\s*(?:\"|\'|pol|polegadas?)\b|\bmonitor\b.*\b(19|20|21|22|23|24|25|26)\b", re.I)
MONITOR_RE = re.compile(r"\bmonitor\b", re.I)
MONITOR_SIZE_RE = re.compile(r"\b(27|28|29|30|31|32|34|35|38|40|42|43|45|48|49|50|55)\s*(?:\"|\'|pol|polegadas?)\b", re.I)
MONITOR_144HZ_RE = re.compile(r"\b(14[4-9]|1[5-9]\d|[2-9]\d{2})\s*hz\b", re.I)
MONITOR_LG_27_RE = re.compile(r"\b27gs60f\b|(?=.*\blg\b)(?=.*\bultragear\b)(?=.*\b27\s*(?:\"|')?)(?=.*\b180\s*hz\b)(?=.*\b(?:fhd|full\s*hd)\b)", re.I)

# Mobos
A520_RE     = re.compile(r"\ba520m?\b", re.I)
H610_RE     = re.compile(r"\bh610m?\b", re.I)
LGA1700_RE  = re.compile(r"\b(?:b660m?|b760m?|z690|z790)\b", re.I)
SPECIFIC_B760M_RE = re.compile(r"\bb760m\b", re.I)

# SSD
SSD_RE  = re.compile(r"\bssd\b.*\bkingston\b|\bkingston\b.*\bssd\b", re.I)
M2_RE   = re.compile(r"\bm\.?2\b|\bnvme\b", re.I)
TB1_RE  = re.compile(r"\b1\s*tb\b", re.I)

# RAM 16GB DDR4 3200 (any brand)
RAM_16GB_3200_RE = re.compile(r"\b(?:ddr4)\b.*\b16\s*gb\b.*\b3200\b|\b16\s*gb\b.*\b(?:ddr4)\b.*\b3200\b", re.I)

# Other
WATER_240MM_ARGB_RE = re.compile(r"\bwater\s*cooler\b.*\b240\s*mm\b.*\bargb\b", re.I)
DUALSENSE_RE = re.compile(r"\b(dualsense|controle\s*ps5|controle\s*playstation\s*5)\b", re.I)
AR_INVERTER_RE = re.compile(r"\bar\s*condicionado\b.*\binverter\b|\binverter\b.*\bar\s*condicionado\b", re.I)
KINDLE_RE = re.compile(r"\bkindle\b", re.I)
CAFETEIRA_PROG_RE = re.compile(r"\bcafeteira\b.*\bprogr[aá]m[aá]vel\b", re.I)
TENIS_NIKE_RE = re.compile(r"\b(tênis|tenis)\s*(nike|air\s*max|air\s*force|jordan)\b", re.I)
WEBCAM_4K_RE = re.compile(r"\bwebcam\b.*\b4k\b", re.I)

# GPUs / CPUs (kept)
RTX5060_3FAN_RE = re.compile(r"\brtx\s*5060(?!\s*ti)\b.*\b(3\s*(?:fans?|oc|x)|triple\s*fan)\b|\b(3\s*(?:fans?|oc|x)|triple\s*fan)\b.*\brtx\s*5060(?!\s*ti)\b", re.I)
RTX5060_2FAN_RE = re.compile(r"\brtx\s*5060(?!\s*ti)\b.*\b(2\s*(?:fans?|oc|x)|dual\s*fan)\b|\b(2\s*(?:fans?|oc|x)|dual\s*fan)\b.*\brtx\s*5060(?!\s*ti)\b", re.I)
RTX5060_RE   = re.compile(r"\brtx\s*5060(?!\s*ti)\b", re.I)
RTX5060TI_RE = re.compile(r"\brtx\s*5060\s*ti\b", re.I)
RTX5070_FAM  = re.compile(r"\brtx\s*5070(\s*ti)?\b", re.I)

RYZEN_7_5700X_RE = re.compile(r"\bryzen\s*7\s*5700x\b", re.I)
I5_14400F_RE = re.compile(r"\bi5[-\s]*14400f\b", re.I)
INTEL_SUP = re.compile(r"\b(i5[-\s]*14[4-9]\d{2}[kf]*|i5[-\s]*145\d{2}[kf]*|i7[-\s]*14\d{3}[kf]*|i9[-\s]*14\d{3}[kf]*)\b", re.I)
AMD_SUP   = re.compile(r"\b(ryzen\s*7\s*5700x[3d]*|ryzen\s*7\s*5800x[3d]*|ryzen\s*9\s*5900x|ryzen\s*9\s*5950x)\b", re.I)
AMD_BLOCK = re.compile(r"\b(ryzen\s*(?:3|5)\s|5600g?t?|5500|5700(?!x))\b", re.I)

# ---------------------------------------------
# HELPERS (headers / thresholds)
# ---------------------------------------------
def needs_header(product_key: str, price: Optional[float]) -> bool:
    if not price: return False
    if product_key == "gpu:rtx5060:3fan" and price < 1950: return True
    if product_key == "gpu:rtx5060:2fan" and price < 1850: return True
    if product_key == "gpu:rtx5060ti" and price < 2100: return True
    if product_key == "cpu:ryzen7_5700x" and price < 800: return True
    if product_key == "cpu:i5_14400f" and price < 750: return True
    if product_key.startswith("cpu:") and price < 900: return True
    if product_key == "dualsense" and price < 300: return True
    if product_key == "monitor:lg27" and price < 700: return True
    return False

def get_header_text(product_key: str) -> str:
    if product_key == "ar_premium":
        return "Oportunidade🔥 "
    return "Corre!🔥 "

# ---------------------------------------------
# CORE MATCHER (with debug logs)
# ---------------------------------------------
def classify_and_match(text: str) -> Tuple[bool, str, str, Optional[float], str]:
    """
    Returns (ok: bool, key: str, title: str, price: Optional[float], reason: str)

    This version logs which rule attempted to match and why.
    """
    t = text or ""

    def rule_log(rule_name, ok, key, title, price, reason):
        log.info("RULE_EVAL | rule=%s | ok=%s | key=%s | title=%s | price=%s | reason=%s",
                 rule_name, str(ok), key, title, f"{price:.2f}" if isinstance(price, (int,float)) else str(price), reason)

    if BLOCK_CATS.search(t):
        rule_log("block:cat", False, "block:cat", "Categoria bloqueada", None, "categoria bloqueada")
        return False, "block:cat", "Categoria bloqueada", None, "Categoria bloqueada"
    if PC_GAMER_RE.search(t):
        rule_log("block:pcgamer", False, "block:pcgamer", "PC Gamer bloqueado", None, "PC Gamer bloqueado")
        return False, "block:pcgamer", "PC Gamer bloqueado", None, "PC Gamer bloqueado"

    price = find_lowest_price(t)

    def ret(rule_name, ok, key, title, price_val, reason):
        rule_log(rule_name, ok, key, title, price_val, reason)
        return ok, key, title, price_val, reason

    # TV Box – <= 200
    if TVBOX_RE.search(t):
        if price is None:
            return ret("tvbox", False, "tvbox", "TV Box", None, "sem preço")
        if price <= 200:
            return ret("tvbox", True, "tvbox", "TV Box", price, "<= 200")
        return ret("tvbox", False, "tvbox", "TV Box", price, "> 200")

    # TV – only 40" or bigger (user requested) and <=1000
    if TV_RE.search(t):
        # require explicit size >=40
        if not TV_SIZE_RE.search(t):
            return ret("tv", False, "tv", "TV / Smart TV", price, "tamanho <40 ou não informado")
        # if size present, check price
        if price is None:
            return ret("tv", False, "tv", "TV / Smart TV", None, "sem preço")
        if price < 200:
            return ret("tv", False, "tv", "TV / Smart TV", price, "preço irreal (<200)")
        if price <= 1000:
            return ret("tv", True, "tv", "TV / Smart TV", price, "<= 1000")
        return ret("tv", False, "tv", "TV / Smart TV", price, "> 1000")

    # Block small monitors <27"
    if MONITOR_SMALL_RE.search(t):
        return ret("monitor:block_small", False, "monitor:block_small", "Monitor < 27\"", price, "tamanho pequeno")

    # Mobos
    if A520_RE.search(t):
        return ret("mobo:a520", False, "mobo:a520", "A520 bloqueada", price, "A520 bloqueada")
    if H610_RE.search(t):
        return ret("mobo:h610", False, "mobo:h610", "H610 bloqueada", price, "H610 bloqueada")
    if LGA1700_RE.search(t) or SPECIFIC_B760M_RE.search(t):
        if price is None:
            return ret("mobo:lga1700", False, "mobo:lga1700", "Placa-mãe LGA1700/B760", None, "sem preço")
        if price < 300:
            return ret("mobo:lga1700", False, "mobo:lga1700", "Placa-mãe LGA1700/B760", price, "preço irreal (<300)")
        if price < 600:
            return ret("mobo:lga1700", True, "mobo:lga1700", "Placa-mãe LGA1700/B760", price, "<600")
        return ret("mobo:lga1700", False, "mobo:lga1700", "Placa-mãe LGA1700/B760", price, ">=600")

    # GPUs
    if RTX5060_3FAN_RE.search(t):
        if price is None:
            return ret("gpu:rtx5060:3fan", False, "gpu:rtx5060:3fan", "RTX 5060 3 Fans", None, "sem preço")
        if price < 1500:
            return ret("gpu:rtx5060:3fan", False, "gpu:rtx5060:3fan", "RTX 5060 3 Fans", price, "preço irreal (<1500)")
        if price < 1950:
            return ret("gpu:rtx5060:3fan", True, "gpu:rtx5060:3fan", "RTX 5060 3 Fans", price, "<1950")
        return ret("gpu:rtx5060:3fan", False, "gpu:rtx5060:3fan", "RTX 5060 3 Fans", price, ">=1950")
    if RTX5060_2FAN_RE.search(t):
        if price is None:
            return ret("gpu:rtx5060:2fan", False, "gpu:rtx5060:2fan", "RTX 5060 2 Fans", None, "sem preço")
        if price < 1500:
            return ret("gpu:rtx5060:2fan", False, "gpu:rtx5060:2fan", "RTX 5060 2 Fans", price, "preço irreal (<1500)")
        if price < 1850:
            return ret("gpu:rtx5060:2fan", True, "gpu:rtx5060:2fan", "RTX 5060 2 Fans", price, "<1850")
        return ret("gpu:rtx5060:2fan", False, "gpu:rtx5060:2fan", "RTX 5060 2 Fans", price, ">=1850")
    if RTX5060TI_RE.search(t):
        if price is None:
            return ret("gpu:rtx5060ti", False, "gpu:rtx5060ti", "RTX 5060 Ti", None, "sem preço")
        if price < 1500:
            return ret("gpu:rtx5060ti", False, "gpu:rtx5060ti", "RTX 5060 Ti", price, "preço irreal (<1500)")
        if price < 2100:
            return ret("gpu:rtx5060ti", True, "gpu:rtx5060ti", "RTX 5060 Ti", price, "<2100")
        return ret("gpu:rtx5060ti", False, "gpu:rtx5060ti", "RTX 5060 Ti", price, ">=2100")
    if RTX5060_RE.search(t):
        if price is None:
            return ret("gpu:rtx5060", False, "gpu:rtx5060", "RTX 5060", None, "sem preço")
        if price < 1500:
            return ret("gpu:rtx5060", False, "gpu:rtx5060", "RTX 5060", price, "preço irreal (<1500)")
        if price < 1900:
            return ret("gpu:rtx5060", True, "gpu:rtx5060", "RTX 5060", price, "<1900")
        return ret("gpu:rtx5060", False, "gpu:rtx5060", "RTX 5060", price, ">=1900")
    if RTX5070_FAM.search(t):
        if price is None:
            return ret("gpu:rtx5070", False, "gpu:rtx5070", "RTX 5070/5070 Ti", None, "sem preço")
        if price < 2500:
            return ret("gpu:rtx5070", False, "gpu:rtx5070", "RTX 5070/5070 Ti", price, "preço irreal (<2500)")
        if price < 3500:
            return ret("gpu:rtx5070", True, "gpu:rtx5070", "RTX 5070/5070 Ti", price, "<3500")
        return ret("gpu:rtx5070", False, "gpu:rtx5070", "RTX 5070/5070 Ti", price, ">=3500")

    # SSD Kingston M.2 1TB (<=400)
    if SSD_RE.search(t) and M2_RE.search(t) and TB1_RE.search(t):
        if price is None:
            return ret("ssd:kingston:m2:1tb", False, "ssd:kingston:m2:1tb", "SSD Kingston M.2 1TB", None, "sem preço")
        if price <= 400:
            return ret("ssd:kingston:m2:1tb", True, "ssd:kingston:m2:1tb", "SSD Kingston M.2 1TB", price, "<=400")
        return ret("ssd:kingston:m2:1tb", False, "ssd:kingston:m2:1tb", "SSD Kingston M.2 1TB", price, ">400")

    # RAM 16GB DDR4 3200 (any brand)
    if RAM_16GB_3200_RE.search(t):
        if price is None:
            return ret("ram:16gb3200", False, "ram:16gb3200", "Memória 16GB DDR4 3200MHz", None, "sem preço")
        if price < 100:
            return ret("ram:16gb3200", False, "ram:16gb3200", "Memória 16GB DDR4 3200MHz", price, "preço irreal (<100)")
        if price <= 300:
            return ret("ram:16gb3200", True, "ram:16gb3200", "Memória 16GB DDR4 3200MHz", price, "<=300")
        return ret("ram:16gb3200", False, "ram:16gb3200", "Memória 16GB DDR4 3200MHz", price, ">300")

    # Ar inverter
    if AR_INVERTER_RE.search(t):
        if price is None:
            return ret("ar_inverter", False, "ar_inverter", "Ar Condicionado Inverter", None, "sem preço")
        if price < 1000:
            return ret("ar_inverter", False, "ar_inverter", "Ar Condicionado Inverter", price, "preço irreal (<1000)")
        if price < 1500:
            return ret("ar_inverter", True, "ar_inverter", "Ar Condicionado Inverter", price, "<1500")
        return ret("ar_inverter", False, "ar_inverter", "Ar Condicionado Inverter", price, ">=1500")

    # Monitores 27"+ 144Hz
    if MONITOR_LG_27_RE.search(t):
        if price is None:
            return ret("monitor:lg27", False, "monitor:lg27", 'Monitor LG UltraGear 27" 180Hz', None, "sem preço")
        if price < 200:
            return ret("monitor:lg27", False, "monitor:lg27", 'Monitor LG UltraGear 27" 180Hz', price, "preço irreal (<200)")
        if price < 700:
            return ret("monitor:lg27", True, "monitor:lg27", 'Monitor LG UltraGear 27" 180Hz', price, "<700")
        return ret("monitor:lg27", False, "monitor:lg27", 'Monitor LG UltraGear 27" 180Hz', price, ">=700")
    if MONITOR_RE.search(t) and MONITOR_SIZE_RE.search(t) and MONITOR_144HZ_RE.search(t):
        if price is None:
            return ret("monitor", False, "monitor", 'Monitor 27"+ 144Hz+', None, "sem preço")
        if price < 200:
            return ret("monitor", False, "monitor", 'Monitor 27"+ 144Hz+', price, "preço irreal (<200)")
        if price < 700:
            return ret("monitor", True, "monitor", 'Monitor 27"+ 144Hz+', price, "<700")
        return ret("monitor", False, "monitor", 'Monitor 27"+ 144Hz+', price, ">=700")

    return ret("none", False, "none", "sem match", price, "sem match")

# ---------------------------------------------
# DUP GUARD (persistente)
# ---------------------------------------------
class Seen:
    def __init__(self, maxlen=2500):
        self.maxlen = maxlen
        self.data: Dict[str, float] = {}
        self.lock = threading.Lock()
        self._load()

    def _key(self, chat_id, msg_id) -> str:
        return f"{chat_id}:{msg_id}"

    def is_dup(self, chat_id, msg_id):
        key = self._key(chat_id, msg_id)
        with self.lock:
            if key in self.data:
                return True
            if len(self.data) > self.maxlen:
                items = sorted(self.data.items(), key=lambda kv: kv[1], reverse=True)[: self.maxlen // 2]
                self.data = {k: v for k, v in items}
            self.data[key] = time.time()
        return False

    def dump(self):
        try:
            with self.lock:
                with open(PERSIST_SEEN_FILE, "w", encoding="utf-8") as f:
                    json.dump({"ts": time.time(), "items": list(self.data.keys())}, f)
            log.info("Persisted seen -> %s (%d items)", PERSIST_SEEN_FILE, len(self.data))
        except Exception as e:
            log.exception("Erro ao persistir seen: %s", e)

    def _load(self):
        try:
            if os.path.exists(PERSIST_SEEN_FILE):
                with open(PERSIST_SEEN_FILE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                items = d.get("items") or []
                now = time.time()
                with self.lock:
                    for k in items[-self.maxlen:]:
                        self.data[k] = now
                log.info("Loaded seen from %s (%d items)", PERSIST_SEEN_FILE, len(self.data))
        except Exception as e:
            log.warning("Falha ao carregar seen persistido: %s", e)

seen = Seen()

# ---------------------------------------------
# Match history logging (append-only)
# ---------------------------------------------
_matches_lock = threading.Lock()
def append_match_log(record: dict):
    try:
        with _matches_lock:
            with open(PERSIST_MATCH_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        log.exception("Erro ao gravar match log")

# ---------------------------------------------
# MAIN
# ---------------------------------------------
def main():
    log.info("Conectando ao Telegram (StringSession)...")
    with TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH) as client:
        try:
            log.info("Conectado ao Telegram.")
            dialogs = client.get_dialogs()
            uname2ent = {}
            for d in dialogs:
                ent = getattr(d, "entity", None)
                uname = getattr(ent, "username", None)
                if uname:
                    uname2ent[f"@{uname.lower()}"] = ent
            resolved = [uname2ent[u] for u in MONITORED_USERNAMES if u in uname2ent]
            log.info("✅ Monitorando %d canais…", len(resolved))

            def touch_health():
                try:
                    with open(HEALTH_FILE, "w", encoding="utf-8") as hf:
                        hf.write(json.dumps({"pid": PID, "ts": time.time(), "start": START_TS}))
                except Exception:
                    log.exception("Erro ao escrever HEALTH file")

            touch_health()
            def health_loop():
                while True:
                    touch_health()
                    time.sleep(30)
            t = threading.Thread(target=health_loop, daemon=True)
            t.start()

            @client.on(events.NewMessage(chats=resolved or None))
            async def handler(event):
                try:
                    msg_text = (event.raw_text or "").strip()
                    if not msg_text:
                        msg_text = getattr(event.message, "message", "") or ""
                        msg_text = (msg_text or "").strip()
                    if not msg_text:
                        return

                    chat = getattr(event, "chat", None)
                    chat_id = getattr(chat, "id", getattr(event.message, "peer_id", None))
                    msg_id = getattr(event.message, "id", getattr(event, "id", None))

                    if chat_id is None or msg_id is None:
                        chat_id = "unknown"
                        msg_id = hash(msg_text)

                    if seen.is_dup(chat_id, msg_id):
                        log.debug("Duplicated message ignored chat=%s id=%s", chat_id, msg_id)
                        return

                    ok, key, title, price, reason = classify_and_match(msg_text)
                    chan = getattr(chat, "username", "(desconhecido)")
                    chan_disp = f"@{chan}" if chan and chan != "(desconhecido)" else "(desconhecido)"
                    price_disp = f"{price:.2f}" if isinstance(price, (int, float)) else "None"

                    if ok:
                        header = get_header_text(key) if needs_header(key, price) else ""
                        msg = f"{header}{msg_text}\n\n— via {chan_disp}"
                        log.info("[%-18s] MATCH → %s | price=%s | key=%s | reason=%s | header=%s",
                                 chan_disp, title, price_disp, key, reason, "YES" if header else "NO")
                        try:
                            notify_all(msg)
                        except Exception:
                            log.exception("Erro ao notificar destinos")
                        append_match_log({
                            "ts": time.time(),
                            "chan": chan_disp,
                            "title": title,
                            "key": key,
                            "price": price,
                            "reason": reason,
                            "text": msg_text[:4000]
                        })
                    else:
                        log.info("[%-18s] IGNORADO → %s | price=%s | key=%s | reason=%s",
                                 chan_disp, title, price_disp, key, reason)

                except Exception as e:
                    log.exception("Handler exception: %s", e)

            client.run_until_disconnected()

        except Exception as e:
            log.exception("Erro fatal no main: %s", e)
        finally:
            log.info("Finalizando client, persistindo estado...")
            seen.dump()

# ---------------------------------------------
# Graceful shutdown hooks
# ---------------------------------------------
def _on_exit(signum=None, frame=None):
    log.info("Sinal de parada recebido (%s). Persistindo estado e saindo...", signum)
    try:
        seen.dump()
    except Exception:
        log.exception("Erro no dump on exit")
    try:
        with open(HEALTH_FILE, "w", encoding="utf-8") as hf:
            hf.write(json.dumps({"pid": PID, "ts": time.time(), "shutdown": True}))
    except Exception:
        pass

atexit.register(_on_exit)
signal.signal(signal.SIGTERM, _on_exit)
signal.signal(signal.SIGINT, _on_exit)

# ---------------------------------------------
# Entrypoint
# ---------------------------------------------
if __name__ == "__main__":
    main()
