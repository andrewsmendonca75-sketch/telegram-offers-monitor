# -*- coding: utf-8 -*-
"""
Realtime Telegram monitor (Telethon) - versão revisada (ajustada conforme solicitado)
Mudanças principais:
- Placas-mãe: match apenas até R$600 (exceto A520/H610 bloqueadas)
- SSD Kingston M.2 1TB: limite reduzido para R$400
- RAM: aceitar qualquer DDR4 16GB 3200MHz (não só Geil Orion)
- Removida categoria "mala de bordo"
- Adicionada detecção de TVs com limite até R$1000
"""

import os
import re
import time
import json
import atexit
import signal
import logging
import threading
import sqlite3
import random
from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime
import requests
from telethon import events
from telethon.sessions import StringSession
from telethon.sync import TelegramClient
from logging.handlers import RotatingFileHandler

# ---------------------------------------------
# CONFIG/ENV helpers
# ---------------------------------------------
def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name, default)
    if v is None:
        return None
    return v

def get_int_env(name: str, default: Optional[int] = None) -> Optional[int]:
    v = os.getenv(name)
    if not v:
        return default
    try:
        return int(v)
    except Exception:
        raise RuntimeError(f"Invalid int for {name}: {v}")

START_TS = datetime.utcnow().isoformat() + "Z"
PID = os.getpid()

RETRY_SEND_ATTEMPTS = get_int_env("RETRY_SEND_ATTEMPTS", 4)
RETRY_SEND_BACKOFF = float(os.getenv("RETRY_SEND_BACKOFF", "1.0"))

PERSIST_SEEN_DB = os.getenv("PERSIST_SEEN_DB", "/tmp/monitor_seen.sqlite")
PERSIST_MATCH_LOG = os.getenv("PERSIST_MATCH_LOG", "/tmp/monitor_matches.log")
HEALTH_FILE = os.getenv("HEALTH_FILE", "/tmp/monitor_health")
LOG_FILE = os.getenv("LOG_FILE", "")  # optional

# Required envs (validated)
API_ID = get_int_env("TELEGRAM_API_ID")
API_HASH = get_env("TELEGRAM_API_HASH", "")
STRING_SESSION = get_env("TELEGRAM_STRING_SESSION", "")
BOT_TOKEN = get_env("TELEGRAM_TOKEN", "")

MONITORED_CHANNELS_RAW = os.getenv("MONITORED_CHANNELS", "")
USER_DESTINATIONS_RAW = os.getenv("USER_DESTINATIONS", os.getenv("USER_CHAT_ID", ""))

missing = []
if not API_ID: missing.append("TELEGRAM_API_ID")
if not API_HASH: missing.append("TELEGRAM_API_HASH")
if not STRING_SESSION: missing.append("TELEGRAM_STRING_SESSION")
if not BOT_TOKEN: missing.append("TELEGRAM_TOKEN")
if missing:
    raise RuntimeError("Missing required envs: " + ", ".join(missing))

# ---------------------------------------------
# LOGGING
# ---------------------------------------------
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logger = logging.getLogger("monitor")
logger.setLevel(getattr(logging, log_level, logging.INFO))
fmt = logging.Formatter("%(asctime)s | %(levelname)5s | %(message)s")

if LOG_FILE:
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
else:
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

logger.info("▶️ Starting realtime monitor pid=%s ts=%s", PID, START_TS)

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
    logger.warning("MONITORED_CHANNELS vazio — nada será filtrado por username.")
else:
    logger.info("▶️ Canais: %s", ", ".join(MONITORED_USERNAMES))

USER_DESTINATIONS: List[str] = _split_csv(USER_DESTINATIONS_RAW)
if not USER_DESTINATIONS:
    logger.warning("USER_DESTINATIONS/USER_CHAT_ID não definido; nada será enviado.")
else:
    logger.info("📬 Destinos: %s", ", ".join(USER_DESTINATIONS))

BOT_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ---------------------------------------------
# BOT SEND WITH RETRIES (requests.Session)
# ---------------------------------------------
_send_lock = threading.Lock()
_session = requests.Session()
_session.headers.update({"User-Agent": "monitor-bot/1.0"})

def _backoff_sleep(attempt: int, base: float = RETRY_SEND_BACKOFF):
    backoff = base * (2 ** attempt)
    jitter = random.uniform(0, backoff * 0.25)
    time.sleep(backoff + jitter)

def bot_send_text(dest: str, text: str) -> Tuple[bool, str]:
    payload = {"chat_id": dest, "text": text, "disable_web_page_preview": True}
    last_err = None
    for attempt in range(RETRY_SEND_ATTEMPTS):
        try:
            with _send_lock:
                r = _session.post(f"{BOT_BASE}/sendMessage", json=payload, timeout=20)
            if r.status_code == 200:
                try:
                    j = r.json()
                except Exception:
                    return True, "ok (non-json)"
                if j.get("ok"):
                    return True, "ok"
                last_err = f"api-error: {j}"
            else:
                if r.status_code in (429, 500, 502, 503, 504):
                    last_err = f"transient-http-{r.status_code}"
                else:
                    last_err = f"status={r.status_code} text={r.text}"
        except Exception as e:
            last_err = repr(e)
        logger.debug("bot_send_text retry %d/%d -> %s", attempt+1, RETRY_SEND_ATTEMPTS, last_err)
        if attempt + 1 < RETRY_SEND_ATTEMPTS:
            _backoff_sleep(attempt)
    return False, last_err or "unknown-error"

def notify_all(text: str):
    for d in USER_DESTINATIONS:
        ok, msg = bot_send_text(d, text)
        if ok:
            logger.info("· envio=ok → %s", d)
        else:
            logger.error("· envio=ERRO → %s | motivo=%s", d, msg)

# ---------------------------------------------
# PRICE PARSER (BRL) - revisado
# ---------------------------------------------
PRICE_PIX_RE = re.compile(
    r"(?i)r\$\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{1,2})?)\s*(?:no\s*pix|à\s*vista|a\s*vista)",
    re.I
)
PRICE_FALLBACK_RE = re.compile(r"(?i)r\$\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{1,2})?)", re.I)
NOT_PRICE_WORDS = re.compile(r"(?i)\b(moedas?|pontos?|cashback|reembolso|de\s*volta|parcelas?|x\s*de|frete|km|m2)\b", re.I)
URL_RE = re.compile(r"https?://\S+", re.I)

def _to_float_brl(raw: str) -> Optional[float]:
    s = raw.strip().replace(".", "").replace(",", ".")
    try:
        v = float(s)
        if v <= 0:
            return None
        if v < 5 or v > 5_000_000:
            return None
        return v
    except Exception:
        return None

def find_lowest_price(text: str) -> Optional[float]:
    if not text:
        return None
    text_nourl = URL_RE.sub(" ", text)
    vals: List[float] = []
    for m in PRICE_PIX_RE.finditer(text_nourl):
        v = _to_float_brl(m.group(1))
        if v:
            vals.append(v)
    if not vals:
        for m in PRICE_FALLBACK_RE.finditer(text_nourl):
            start = max(0, m.start() - 40)
            end = min(len(text_nourl), m.end() + 40)
            context = text_nourl[start:end]
            if NOT_PRICE_WORDS.search(context):
                continue
            v = _to_float_brl(m.group(1))
            if v:
                vals.append(v)
    return min(vals) if vals else None

# ---------------------------------------------
# REGEX RULES (ajustados)
# ---------------------------------------------
BLOCK_CATS = re.compile(
    r"\b(celular|smartphone|iphone|android|notebook|laptop|macbook|geladeira|refrigerador|m[aá]quina\s*de\s*lavar|lavadora|lava\s*e\s*seca)\b",
    re.I
)
PC_GAMER_RE = re.compile(r"\b(pc\s*gamer|setup\s*completo|kit\s*completo)\b", re.I)

RTX5060_2FAN_RE = re.compile(r"\brtx\s*5060(?!\s*ti)\b.*\b(2\s*(?:fans?|oc|x)|dual\s*fan)\b|\b(2\s*(?:fans?|oc|x)|dual\s*fan)\b.*\brtx\s*5060(?!\s*ti)\b", re.I)
RTX5060_3FAN_RE = re.compile(r"\brtx\s*5060(?!\s*ti)\b.*\b(3\s*(?:fans?|oc|x)|triple\s*fan)\b|\b(3\s*(?:fans?|oc|x)|triple\s*fan)\b.*\brtx\s*5060(?!\s*ti)\b", re.I)
RTX5060_RE   = re.compile(r"\brtx\s*5060(?!\s*ti)\b", re.I)
RTX5060TI_RE = re.compile(r"\brtx\s*5060\s*ti\b", re.I)
RTX5070_FAM  = re.compile(r"\brtx\s*5070(\s*ti)?\b", re.I)

RYZEN_7_5700X_RE = re.compile(r"\bryzen\s*7\s*5700x\b", re.I)
I5_14400F_RE = re.compile(r"\bi5[-\s]*14400f\b", re.I)

INTEL_SUP = re.compile(r"\b(i5[-\s]*14[4-9]\d{2}[kf]*|i5[-\s]*145\d{2}[kf]*|i7[-\s]*14\d{3}[kf]*|i9[-\s]*14\d{3}[kf]*)\b", re.I)
AMD_SUP   = re.compile(r"\b(ryzen\s*7\s*5700x[3d]*|ryzen\s*7\s*5800x[3d]*|ryzen\s*9\s*5900x|ryzen\s*9\s*5950x)\b", re.I)
AMD_BLOCK = re.compile(r"\b(ryzen\s*(?:3|5)\s|5600g?t?|5500|5700(?!x))\b", re.I)

A520_RE     = re.compile(r"\ba520m?\b", re.I)
H610_RE     = re.compile(r"\bh610m?\b", re.I)
LGA1700_RE  = re.compile(r"\b(b660m?|b760m?|z690|z790)\b", re.I)

SPECIFIC_B760M_RE = re.compile(r"\bb760m\b", re.I)
INTEL_14600K_RE   = re.compile(r"\bi5[-\s]*14600k\b", re.I)

WATER_240MM_ARGB_RE = re.compile(
    r"\bwater\s*cooler\b.*\b240\s*mm\b.*\bargb\b|"
    r"\bargb\b.*\bwater\s*cooler\b.*\b240\s*mm\b|"
    r"\b240\s*mm\b.*\bwater\s*cooler\b.*\bargb\b",
    re.I
)

SSD_RE  = re.compile(r"\bssd\b.*\bkingston\b|\bkingston\b.*\bssd\b", re.I)
M2_RE   = re.compile(r"\bm\.?2\b|\bnvme\b", re.I)
TB1_RE  = re.compile(r"\b1\s*tb\b", re.I)

# RAM: qualquer DDR4 16GB 3200
RAM_16GB_3200_RE = re.compile(r"\b(ddr4)\b.*\b16\s*gb\b.*\b3200\b|\b16\s*gb\b.*\b(ddr4)\b.*\b3200\b", re.I)

CADEIRA_RE = re.compile(r"\bcadeira\b", re.I)
DUALSENSE_RE = re.compile(r"\b(dualsense|controle\s*ps5|controle\s*playstation\s*5)\b", re.I)

AR_INVERTER_RE = re.compile(r"\bar\s*condicionado\b.*\binverter\b|\binverter\b.*\bar\s*condicionado\b", re.I)
KINDLE_RE = re.compile(r"\bkindle\b", re.I)
CAFETEIRA_PROG_RE = re.compile(r"\bcafeteira\b.*\bprogr[aá]m[aá]vel\b|\bprogr[aá]m[aá]vel\b.*\bcafeteira\b", re.I)
TENIS_NIKE_RE = re.compile(r"\b(tênis|tenis)\s*(nike|air\s*max|air\s*force|jordan)\b", re.I)
WEBCAM_4K_RE = re.compile(r"\bwebcam\b.*\b4k\b|\b4k\b.*\bwebcam\b", re.I)

# TV detection (nova)
TV_RE = re.compile(r"\b(tv|smart\s*tv|televis(ão|ao))\b", re.I)

MONITOR_LG_27_RE = re.compile(
    r"\b27gs60f\b|(?=.*\blg\b)(?=.*\bultragear\b)(?=.*\b27\s*[\"']?)(?=.*\b180\s*hz\b)(?=.*\b(?:fhd|full\s*hd)\b)",
    re.I
)
MONITOR_RE = re.compile(r"\bmonitor\b", re.I)
MONITOR_SIZE_RE = re.compile(r"\b(27|28|29|30|31|32|34|35|38|40|42|43|45|48|49|50|55)\s*[\"']?\b", re.I)
MONITOR_144HZ_RE = re.compile(r"\b(14[4-9]|1[5-9]\d|[2-9]\d{2})\s*hz\b", re.I)

# ---------------------------------------------
# HELPERS
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
# CORE MATCHER (com as alterações solicitadas)
# ---------------------------------------------
def classify_and_match(text: str):
    t = (text or "")
    if BLOCK_CATS.search(t): return False, "block:cat", "Categoria bloqueada", None, "celular/notebook etc."
    if PC_GAMER_RE.search(t): return False, "block:pcgamer", "PC Gamer bloqueado", None, "setup completo"

    price = find_lowest_price(t)

    # MOBOS – somente até 600 reais (exceto modelos bloqueados)
    if A520_RE.search(t):
        return False, "mobo:a520", "A520 bloqueada", price, "A520 bloqueada"

    if H610_RE.search(t):
        return False, "mobo:h610", "H610 bloqueada", price, "H610 bloqueada"

    if LGA1700_RE.search(t) or SPECIFIC_B760M_RE.search(t):
        if not price:
            return False, "mobo:lga1700", "Placa-mãe LGA1700/B760", None, "sem preço"
        if price < 300:
            return False, "mobo:lga1700", "Placa-mãe LGA1700/B760", price, "preço irreal (< 300)"
        if price < 600:
            return True, "mobo:lga1700", "Placa-mãe LGA1700/B760", price, "< 600"
        return False, "mobo:lga1700", "Placa-mãe LGA1700/B760", price, ">= 600"

    # CPUs - prioridades e checagens como antes
    if INTEL_14600K_RE.search(t):
        if not price: return False, "cpu:i5-14600k", "i5-14600K", None, "sem preço"
        if price < 400: return False, "cpu:i5-14600k", "i5-14600K", price, "preço irreal (< 400)"
        if price < 1000: return True, "cpu:i5-14600k", "i5-14600K", price, "< 1000"
        return False, "cpu:i5-14600k", "i5-14600K", price, ">= 1000"

    if RTX5060_3FAN_RE.search(t):
        if not price: return False, "gpu:rtx5060:3fan", "RTX 5060 3 Fans", None, "sem preço"
        if price < 1500: return False, "gpu:rtx5060:3fan", "RTX 5060 3 Fans", price, "preço irreal (< 1500)"
        if price < 1950: return True, "gpu:rtx5060:3fan", "RTX 5060 Triple Fan", price, "< 1950"
        return False, "gpu:rtx5060:3fan", "RTX 5060 3 Fans", price, ">= 1950"

    if RTX5060_2FAN_RE.search(t):
        if not price: return False, "gpu:rtx5060:2fan", "RTX 5060 2 Fans", None, "sem preço"
        if price < 1500: return False, "gpu:rtx5060:2fan", "RTX 5060 2 Fans", price, "preço irreal (< 1500)"
        if price < 1850: return True, "gpu:rtx5060:2fan", "RTX 5060 Dual Fan", price, "< 1850"
        return False, "gpu:rtx5060:2fan", "RTX 5060 2 Fans", price, ">= 1850"

    if RTX5060TI_RE.search(t):
        if not price: return False, "gpu:rtx5060ti", "RTX 5060 Ti", None, "sem preço"
        if price < 1500: return False, "gpu:rtx5060ti", "RTX 5060 Ti", price, "preço irreal (< 1500)"
        if price < 2100: return True, "gpu:rtx5060ti", "RTX 5060 Ti", price, "< 2100"
        return False, "gpu:rtx5060ti", "RTX 5060 Ti", price, ">= 2100"

    if RTX5060_RE.search(t):
        if not price: return False, "gpu:rtx5060", "RTX 5060", None, "sem preço"
        if price < 1500: return False, "gpu:rtx5060", "RTX 5060", price, "preço irreal (< 1500)"
        if price < 1900: return True, "gpu:rtx5060", "RTX 5060", price, "< 1900"
        return False, "gpu:rtx5060", "RTX 5060", price, ">= 1900"

    if RTX5070_FAM.search(t):
        if not price: return False, "gpu:rtx5070", "RTX 5070/5070 Ti", None, "sem preço"
        if price < 2500: return False, "gpu:rtx5070", "RTX 5070/5070 Ti", price, "preço irreal (< 2500)"
        if price < 3500: return True, "gpu:rtx5070", "RTX 5070/5070 Ti", price, "< 3500"
        return False, "gpu:rtx5070", "RTX 5070/5070 Ti", price, ">= 3500"

    if AMD_BLOCK.search(t): return False, "cpu:amd:block", "CPU AMD inferior", price, "Ryzen 3/5 bloqueado"

    if RYZEN_7_5700X_RE.search(t):
        if not price: return False, "cpu:ryzen7_5700x", "Ryzen 7 5700X", None, "sem preço"
        if price < 400: return False, "cpu:ryzen7_5700x", "Ryzen 7 5700X", price, "preço irreal (< 400)"
        if price < 800: return True, "cpu:ryzen7_5700x", "Ryzen 7 5700X", price, "< 800"
        return False, "cpu:ryzen7_5700x", "Ryzen 7 5700X", price, ">= 800"

    if I5_14400F_RE.search(t):
        if not price: return False, "cpu:i5_14400f", "i5-14400F", None, "sem preço"
        if price < 400: return False, "cpu:i5_14400f", "i5-14400F", price, "preço irreal (< 400)"
        if price < 750: return True, "cpu:i5_14400f", "i5-14400F", price, "< 750"
        return False, "cpu:i5_14400f", "i5-14400F", price, ">= 750"

    if INTEL_SUP.search(t):
        if not price: return False, "cpu:intel", "CPU Intel sup.", None, "sem preço"
        if price < 400: return False, "cpu:intel", "CPU Intel sup.", price, "preço irreal (< 400)"
        if price < 900: return True, "cpu:intel", "CPU Intel sup. (i5-14400F+)", price, "< 900"
        return False, "cpu:intel", "CPU Intel sup.", price, ">= 900"

    if AMD_SUP.search(t):
        if not price: return False, "cpu:amd", "CPU AMD sup.", None, "sem preço"
        if price < 400: return False, "cpu:amd", "CPU AMD sup.", price, "preço irreal (< 400)"
        if price < 900: return True, "cpu:amd", "CPU AMD sup. (Ryzen 7 5700X+)", price, "< 900"
        return False, "cpu:amd", "CPU AMD sup.", price, ">= 900"

    # Water cooler
    if WATER_240MM_ARGB_RE.search(t):
        if not price: return False, "cooler:water240argb", "Water Cooler 240mm ARGB", None, "sem preço"
        if price < 50: return False, "cooler:water240argb", "Water Cooler 240mm ARGB", price, "preço irreal (< 50)"
        if price < 200: return True, "cooler:water240argb", "Water Cooler 240mm ARGB", price, "< 200"
        return False, "cooler:water240argb", "Water Cooler 240mm ARGB", price, ">= 200"

    # SSD Kingston M.2 1TB (limite reduzido para 400)
    if SSD_RE.search(t) and M2_RE.search(t) and TB1_RE.search(t):
        if not price: return False, "ssd:kingston:m2:1tb", "SSD Kingston M.2 1TB", None, "sem preço"
        if price <= 400:
            return True, "ssd:kingston:m2:1tb", "SSD Kingston M.2 1TB", price, "<= 400"
        return False, "ssd:kingston:m2:1tb", "SSD Kingston M.2 1TB", price, "> 400"

    # RAM DDR4 16GB 3200MHz – qualquer marca
    if RAM_16GB_3200_RE.search(t):
        if not price:
            return False, "ram:16gb3200", "Memória 16GB DDR4 3200MHz", None, "sem preço"
        if price < 100:
            return False, "ram:16gb3200", "Memória 16GB DDR4 3200MHz", price, "preço irreal (< 100)"
        if price <= 300:
            return True, "ram:16gb3200", "Memória 16GB DDR4 3200MHz", price, "<= 300"
        return False, "ram:16gb3200", "Memória 16GB DDR4 3200MHz", price, "> 300"

    # Cadeira
    if CADEIRA_RE.search(t):
        if not price: return False, "cadeira", "Cadeira", None, "sem preço"
        if 300 <= (price or 0) < 500: return True, "cadeira", "Cadeira Gamer", price, "entre 300-500"
        return False, "cadeira", "Cadeira", price, "fora da faixa 300-500"

    # DualSense
    if DUALSENSE_RE.search(t):
        if not price: return False, "dualsense", "Controle PS5 DualSense", None, "sem preço"
        if price < 200: return False, "dualsense", "Controle PS5 DualSense", price, "preço irreal (< 200)"
        if price < 300: return True, "dualsense", "Controle PS5 DualSense", price, "< 300"
        return False, "dualsense", "Controle PS5 DualSense", price, ">= 300"

    # Ar-condicionado inverter
    if AR_INVERTER_RE.search(t):
        if not price: return False, "ar_inverter", "Ar Condicionado Inverter", None, "sem preço"
        if price < 1000: return False, "ar_inverter", "Ar Condicionado Inverter", price, "preço irreal (< 1000)"
        if price < 1500: return True, "ar_inverter", "Ar Condicionado Inverter", price, "< 1500"
        return False, "ar_inverter", "Ar Condicionado Inverter", price, ">= 1500"

    # Kindle
    if KINDLE_RE.search(t):
        if not price: return False, "kindle", "Kindle", None, "sem preço"
        if price < 100: return False, "kindle", "Kindle", price, "preço irreal (< 100)"
        if price <= 470: return True, "kindle", "Kindle", price, "<= 470"
        return False, "kindle", "Kindle", price, "> 470"

    # Cafeteira programável
    if CAFETEIRA_PROG_RE.search(t):
        if not price: return False, "cafeteira", "Cafeteira Programável", None, "sem preço"
        if price < 50: return False, "cafeteira", "Cafeteira Programável", price, "preço irreal (< 50)"
        if price < 500: return True, "cafeteira", "Cafeteira Programável", price, "< 500"
        return False, "cafeteira", "Cafeteira Programável", price, ">= 500"

    # Tênis Nike
    if TENIS_NIKE_RE.search(t):
        if price and price < 250: return True, "tenis_nike", "Tênis Nike", price, "< 250"
        return False, "tenis_nike", "Tênis Nike", price, ">= 250 ou sem preço"

    # Webcam 4K
    if WEBCAM_4K_RE.search(t):
        if price and price < 250: return True, "webcam_4k", "Webcam 4K", price, "< 250"
        return False, "webcam_4k", "Webcam 4K", price, ">= 250 ou sem preço"

    # TVs – nova regra: até R$1000
    if TV_RE.search(t):
        if not price: return False, "tv", "TV / Smart TV", None, "sem preço"
        if price < 200: return False, "tv", "TV / Smart TV", price, "preço irreal (<200)"
        if price <= 1000: return True, "tv", "TV / Smart TV", price, "<=1000"
        return False, "tv", "TV / Smart TV", price, "> 1000"

    # Monitor LG UltraGear 27" 180Hz
    if MONITOR_LG_27_RE.search(t):
        if not price: return False, "monitor:lg27", "Monitor LG UltraGear 27\" 180Hz", None, "sem preço"
        if price < 200: return False, "monitor:lg27", "Monitor LG UltraGear 27\" 180Hz", price, "preço irreal (< 200)"
        if price < 700: return True, "monitor:lg27", "Monitor LG UltraGear 27\" 180Hz", price, "< 700"
        return False, "monitor:lg27", "Monitor LG UltraGear 27\" 180Hz", price, ">= 700"

    # Monitores 27"+ com 144Hz+
    if MONITOR_RE.search(t) and MONITOR_SIZE_RE.search(t) and MONITOR_144HZ_RE.search(t):
        if not price: return False, "monitor", "Monitor 27\"+ 144Hz+", None, "sem preço"
        if price < 200: return False, "monitor", "Monitor 27\"+ 144Hz+", price, "preço irreal (< 200)"
        if price < 700: return True, "monitor", "Monitor 27\"+ 144Hz+", price, "< 700"
        return False, "monitor", "Monitor 27\"+ 144Hz+", price, ">= 700"

    return False, "none", "sem match", price, "sem match"

# ---------------------------------------------
# SEEN persistence using sqlite (thread-safe-ish)
# ---------------------------------------------
class SeenDB:
    def __init__(self, path: str, maxlen: int = 25000):
        self.path = path
        self.maxlen = maxlen
        self.lock = threading.Lock()
        self._init_db()

    def _conn(self):
        return sqlite3.connect(self.path, timeout=30, check_same_thread=False)

    def _init_db(self):
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, ts REAL)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_seen_ts ON seen (ts)")
            c.commit()
        logger.info("Seen DB ready -> %s", self.path)

    def _key(self, chat_id: Any, msg_id: Any) -> str:
        return f"{chat_id}:{msg_id}"

    def is_dup(self, chat_id: Any, msg_id: Any) -> bool:
        k = self._key(chat_id, msg_id)
        now = time.time()
        with self.lock:
            conn = self._conn()
            try:
                cur = conn.execute("SELECT 1 FROM seen WHERE key = ?", (k,))
                row = cur.fetchone()
                if row:
                    return True
                conn.execute("INSERT OR REPLACE INTO seen (key, ts) VALUES (?, ?)", (k, now))
                conn.commit()
                cur = conn.execute("SELECT COUNT(*) FROM seen")
                cnt = cur.fetchone()[0]
                if cnt > self.maxlen:
                    cutoff_cur = conn.execute("SELECT key FROM seen ORDER BY ts DESC LIMIT ?", (self.maxlen // 2,))
                    keep_keys = [r[0] for r in cutoff_cur.fetchall()]
                    if keep_keys:
                        conn.execute("DELETE FROM seen WHERE key NOT IN ({})".format(",".join("?"*len(keep_keys))), keep_keys)
                        conn.commit()
                return False
            finally:
                conn.close()

    def dump_to_json(self, path: str):
        with self.lock:
            conn = self._conn()
            try:
                rows = [r[0] for r in conn.execute("SELECT key FROM seen ORDER BY ts DESC LIMIT 10000").fetchall()]
            finally:
                conn.close()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "items": rows}, f, ensure_ascii=False)
            logger.info("Persisted seen snapshot -> %s (%d items)", path, len(rows))
        except Exception:
            logger.exception("Erro ao persistir seen snapshot")

seen_db = SeenDB(PERSIST_SEEN_DB)

# ---------------------------------------------
# Match history logging (append-only, fsync)
# ---------------------------------------------
_matches_lock = threading.Lock()
def append_match_log(record: dict):
    try:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with _matches_lock:
            with open(PERSIST_MATCH_LOG, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
    except Exception:
        logger.exception("Erro ao gravar match log")

# ---------------------------------------------
# MAIN
# ---------------------------------------------
def main():
    logger.info("Conectando ao Telegram (StringSession)...")
    with TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH) as client:
        try:
            logger.info("Conectado ao Telegram.")
            try:
                dialogs = client.get_dialogs()
            except Exception:
                dialogs = []
            uname2ent = {}
            for d in dialogs:
                ent = getattr(d, "entity", None)
                uname = getattr(ent, "username", None)
                if uname:
                    uname2ent[f"@{uname.lower()}"] = ent
            resolved = [uname2ent[u] for u in MONITORED_USERNAMES if u in uname2ent]
            logger.info("✅ Monitorando %d canais (resolvidos) …", len(resolved))

            def touch_health(shutdown=False):
                try:
                    with open(HEALTH_FILE, "w", encoding="utf-8") as hf:
                        hf.write(json.dumps({
                            "pid": PID,
                            "ts": time.time(),
                            "start": START_TS,
                            "shutdown": bool(shutdown)
                        }))
                except Exception:
                    logger.exception("Erro ao escrever HEALTH file")

            touch_health()

            def health_loop():
                while True:
                    touch_health(False)
                    time.sleep(30)
            t = threading.Thread(target=health_loop, daemon=True)
            t.start()

            @client.on(events.NewMessage(chats=resolved or None))
            async def handler(event):
                try:
                    msg_text = (getattr(event, "raw_text", None) or "") or ""
                    msg_text = msg_text.strip()
                    if not msg_text:
                        msg_text = getattr(getattr(event, "message", None), "message", "") or ""
                        msg_text = (msg_text or "").strip()
                    if not msg_text:
                        return

                    chat_id = getattr(getattr(event, "chat", None), "id", None)
                    if chat_id is None:
                        peer = getattr(getattr(event, "message", None), "peer_id", None)
                        try:
                            chat_id = getattr(peer, "channel_id", None) or getattr(peer, "user_id", None) or str(peer)
                        except Exception:
                            chat_id = str(peer)

                    msg_id = getattr(getattr(event, "message", None), "id", None) or getattr(event, "id", None) or hash(msg_text)

                    if seen_db.is_dup(chat_id, msg_id):
                        logger.debug("Duplicated message ignored chat=%s id=%s", chat_id, msg_id)
                        return

                    ok, key, title, price, reason = classify_and_match(msg_text)
                    chan = getattr(getattr(event, "chat", None), "username", None) or getattr(getattr(event, "message", None), "from_id", None) or "(desconhecido)"
                    chan_disp = f"@{chan}" if isinstance(chan, str) and not str(chan).startswith("@") and chan != "(desconhecido)" else str(chan)

                    price_disp = f"{price:.2f}" if isinstance(price, (int, float)) else "None"

                    if ok:
                        header = get_header_text(key) if needs_header(key, price) else ""
                        msg = f"{header}{msg_text}\n\n— via {chan_disp}"
                        logger.info("[%-18s] MATCH → %s | price=%s | key=%s | reason=%s | header=%s",
                                    chan_disp, title, price_disp, key, reason, "YES" if header else "NO")
                        try:
                            notify_all(msg)
                        except Exception:
                            logger.exception("Erro ao notificar destinos")
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
                        logger.info("[%-18s] IGNORADO → %s | price=%s | key=%s | reason=%s",
                                    chan_disp, title, price_disp, key, reason)
                except Exception:
                    logger.exception("Handler exception")

            client.run_until_disconnected()

        except Exception as e:
            logger.exception("Erro fatal no main: %s", e)
        finally:
            logger.info("Finalizando client, persistindo estado...")
            try:
                seen_db.dump_to_json(PERSIST_SEEN_DB + ".snapshot.json")
            except Exception:
                logger.exception("Erro dump final")

# ---------------------------------------------
# Graceful shutdown hooks
# ---------------------------------------------
def _on_exit(signum=None, frame=None):
    logger.info("Sinal de parada recebido (%s). Persistindo estado e saindo...", signum)
    try:
        seen_db.dump_to_json(PERSIST_SEEN_DB + ".onexit.json")
    except Exception:
        logger.exception("Erro no dump on exit")
    try:
        with open(HEALTH_FILE, "w", encoding="utf-8") as hf:
            hf.write(json.dumps({"pid": PID, "ts": time.time(), "shutdown": True}))
    except Exception:
        pass

atexit.register(_on_exit)
signal.signal(signal.SIGTERM, _on_exit)
signal.signal(signal.SIGINT, _on_exit)

if __name__ == "__main__":
    main()
