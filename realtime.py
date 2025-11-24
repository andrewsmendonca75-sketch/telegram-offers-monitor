# -*- coding: utf-8 -*-
"""
Realtime Telegram monitor (Telethon) - versão ajustada

Melhorias incluídas:
- validação de ENV no startup (evita falha silenciosa)
- logs mais detalhados e consistentes
- proteção DUP melhorada (chat_id + message_id) com persistência em arquivo
- retries e backoff no envio via Bot API (requests)
- captura de exceções dentro do handler (não deixa o bot morrer)
- dump periódico / on-exit do seen cache e do histórico de matches para .log (para arquivar)
- gravação de um arquivo 'health' com timestamp para monitoramento externo
- limitações em env vars e parsing defensivo de mensagens
- mantém compatibilidade com Telethon StringSession
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
    # if pure digits, keep as-is (chat id) but we return None for username path
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
# PRICE PARSER (BRL) - Versão Ultra Otimizada
# ---------------------------------------------
# Padrão principal: captura preços em vários contextos
PRICE_MAIN_RE = re.compile(
    r"(?i)(?:r\$|por|preço|valor|price)\s*r?\$?\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d{3,}(?:,\d{2})?)",
    re.I
)

# Padrão "no pix" / "à vista" (mais confiável)
PRICE_PIX_RE = re.compile(
    r"(?i)r\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*(?:no\s*pix|à\s*vista|a\s*vista)",
    re.I
)

# Fallback: qualquer R$ seguido de valor
PRICE_FALLBACK_RE = re.compile(
    r"(?i)r\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d{3,}(?:,\d{2})?)",
    re.I
)

# Palavras que indicam que o número NÃO é um preço de produto
NOT_PRICE_WORDS = re.compile(
    r"(?i)\b(moedas?|pontos?|cashback|reembolso|de\s*volta|parcelas?|x\s*de|frete)\b",
    re.I
)

def _to_float_brl(raw: str) -> Optional[float]:
    s = raw.strip().replace(".", "").replace(",", ".")
    try:
        v = float(s)
        return v if 10 < v < 1_000_000 else None
    except Exception:
        return None

def find_lowest_price(text: str) -> Optional[float]:
    """
    Busca o menor preço válido no texto com múltiplas estratégias.
    Agora mais cirúrgico: analisa palavra por palavra, não linha por linha.
    """
    vals: List[float] = []
    
    # Estratégia 1: "no pix" / "à vista" (mais confiável)
    for m in PRICE_PIX_RE.finditer(text):
        v = _to_float_brl(m.group(1))
        if v and v >= 50:
            vals.append(v)
    
    # Estratégia 2: Padrão principal
    if not vals:
        for m in PRICE_MAIN_RE.finditer(text):
            # Pega contexto ao redor (20 chars antes e depois)
            start = max(0, m.start() - 20)
            end = min(len(text), m.end() + 20)
            context = text[start:end]
            
            # Se o contexto tem palavras de "não é preço", pula
            if NOT_PRICE_WORDS.search(context):
                continue
                
            v = _to_float_brl(m.group(1))
            if v and v >= 50:
                vals.append(v)
    
    # Estratégia 3: Fallback (procura qualquer R$)
    if not vals:
        for m in PRICE_FALLBACK_RE.finditer(text):
            # Pega contexto ao redor
            start = max(0, m.start() - 20)
            end = min(len(text), m.end() + 20)
            context = text[start:end]
            
            # Se tem palavras de "não é preço", pula
            if NOT_PRICE_WORDS.search(context):
                continue
            
            v = _to_float_brl(m.group(1))
            if v and v >= 50:
                vals.append(v)
    
    return min(vals) if vals else None

# ---------------------------------------------
# REGEX RULES
# ---------------------------------------------
BLOCK_CATS = re.compile(r"\b(celular|smartphone|iphone|android|notebook|laptop|macbook|geladeira|refrigerador|smart\s*tv|televisão|televisao|tv\s+\d+|m[aá]quina\s*de\s*lavar|lavadora|lava\s*e\s*seca)\b", re.I)
PC_GAMER_RE = re.compile(r"\b(pc\s*gamer|setup\s*completo|kit\s*completo)\b", re.I)

# GPUs - REMOVIDAS: RTX 5050 e RX 7600
RTX5060_RE   = re.compile(r"\brtx\s*5060(?!\s*ti)\b", re.I)
RTX5060TI_RE = re.compile(r"\brtx\s*5060\s*ti\b", re.I)
RTX5070_FAM  = re.compile(r"\brtx\s*5070(\s*ti)?\b", re.I)

# CPUs
INTEL_SUP = re.compile(r"\b(i5[-\s]*14[4-9]\d{2}[kf]*|i5[-\s]*145\d{2}[kf]*|i7[-\s]*14\d{3}[kf]*|i9[-\s]*14\d{3}[kf]*)\b", re.I)
AMD_SUP   = re.compile(r"\b(ryzen\s*7\s*5700x[3d]*|ryzen\s*7\s*5800x[3d]*|ryzen\s*9\s*5900x|ryzen\s*9\s*5950x)\b", re.I)
AMD_BLOCK = re.compile(r"\b(ryzen\s*(?:3|5)\s|5600g?t?|5500|5700(?!x))\b", re.I)

# Mobos
A520_RE     = re.compile(r"\ba520m?\b", re.I)
H610_RE     = re.compile(r"\bh610m?\b", re.I)  # Bloqueada
LGA1700_RE  = re.compile(r"\b(b660m?|b760m?|z690|z790)\b", re.I)  # Removido H610

SPECIFIC_B760M_RE = re.compile(r"\bb760m\b", re.I)
INTEL_14600K_RE   = re.compile(r"\bi5[-\s]*14600k\b", re.I)

# Gabinete - Sem restrição de fans
GAB_RE     = re.compile(r"\bgabinete\b", re.I)

# Coolers - APENAS Water Cooler 240mm
WATER_240MM_RE = re.compile(r"\bwater\s*cooler\b.*\b240\s*mm\b|\b240\s*mm\b.*\bwater\s*cooler\b", re.I)

# SSD - APENAS Kingston
SSD_RE  = re.compile(r"\bssd\b.*\bkingston\b|\bkingston\b.*\bssd\b", re.I)
M2_RE   = re.compile(r"\bm\.?2\b|\bnvme\b", re.I)
TB1_RE  = re.compile(r"\b1\s*tb\b", re.I)

# RAM
RAM_RE  = re.compile(r"\bddr4\b", re.I)
GB16_RE = re.compile(r"\b16\s*gb\b", re.I)
GB8_RE  = re.compile(r"\b8\s*gb\b", re.I)
GB8_RE  = re.compile(r"\b8\s*gb\b", re.I)

# NOVAS CATEGORIAS
CADEIRA_RE = re.compile(r"\bcadeira\b", re.I)

# DualSense - agora com "Corre!🔥"
DUALSENSE_RE = re.compile(r"\b(dualsense|controle\s*ps5|controle\s*playstation\s*5)\b", re.I)

# RAM - APENAS Geil Orion específica
RAM_GEIL_ORION_RE = re.compile(r"\bgeil\s*orion\b.*\b16\s*gb\b|\b16\s*gb\b.*\bgeil\s*orion\b", re.I)

AR_CONDICIONADO_RE = re.compile(r"\b(ar\s*condicionado|split|inverter)\b", re.I)

# Ar-condicionados PREMIUM específicos (com Oportunidade🔥)
AR_PREMIUM_RE = re.compile(
    r"\b(daikin\s+ecoswing|fujitsu\s+premium|samsung\s+windfree|elgin\s+eco\s+ii|gree\s+g[-\s]*top)\b",
    re.I
)

TENIS_NIKE_RE = re.compile(r"\b(tênis|tenis)\s*(nike|air\s*max|air\s*force|jordan)\b", re.I)
WEBCAM_4K_RE = re.compile(r"\bwebcam\b.*\b4k\b|\b4k\b.*\bwebcam\b", re.I)

# Mala de bordo
MALA_BORDO_RE = re.compile(r"\bmala\b.*\bbordo\b|\bbordo\b.*\bmala\b", re.I)

# Kindle
KINDLE_RE = re.compile(r"\bkindle\b", re.I)

# Monitores
MONITOR_RE = re.compile(r"\bmonitor\b", re.I)

# BLOQUEIO: Monitores 24", 25", 26" (qualquer menção) - EXPANDIDO
MONITOR_SMALL_RE = re.compile(
    r"\b(22|23|24|25|26)\s*[\"\'']?\s*(pol|polegadas?|\"|\')?\b|"
    r"\b(22|23|24|25|26)[\"\'']|"
    r"(?:monitor|display|tela)\s+(?:gamer\s+)?(?:de\s+)?(22|23|24|25|26)",
    re.I
)

# Monitor LG UltraGear 27" 180Hz FHD específico (com Corre!🔥) - ULTRA ESPECÍFICO
MONITOR_LG_27_RE = re.compile(
    r"\b27gs60f\b|"  # Modelo exato
    r"(?=.*\blg\b)(?=.*\bultragear\b)(?=.*\b27\s*[\"\'']?)(?=.*\b180\s*hz\b)(?=.*\b(?:fhd|full\s*hd)\b)",  # LG + UltraGear + 27" + 180Hz + FHD
    re.I
)

# Monitores 27" ou maior - APENAS 144Hz+
MONITOR_SIZE_RE = re.compile(r"\b(27|28|29|30|31|32|34|35|38|40|42|43|45|48|49|50|55)\s*[\"\'']?\s*(pol|polegadas?|\"|\')?\b", re.I)
MONITOR_144HZ_RE = re.compile(r"\b(14[4-9]|1[5-9]\d|[2-9]\d{2})\s*hz\b", re.I)

# ---------------------------------------------
# HELPERS
# ---------------------------------------------
def needs_header(product_key: str, price: Optional[float]) -> bool:
    """Define quando usar cabeçalho 'Corre!🔥' ou 'Oportunidade🔥'"""
    if not price: return False
    if product_key == "gpu:rtx5060" and price < 1900: return True
    if product_key == "gpu:rtx5060ti" and price < 2000: return True  # RTX 5060 Ti com Corre!🔥
    if product_key.startswith("cpu:") and price < 900: return True
    if product_key == "ar_premium" and price < 1850: return True
    if product_key == "dualsense" and price < 300: return True
    if product_key == "monitor:lg27" and price < 700: return True
    return False

def get_header_text(product_key: str) -> str:
    """Retorna o texto correto do cabeçalho"""
    if product_key == "ar_premium":
        return "Oportunidade🔥 "
    return "Corre!🔥 "

# ---------------------------------------------
# CORE MATCHER
# ---------------------------------------------
def classify_and_match(text: str):
    t = text or ""
    if BLOCK_CATS.search(t): return False, "block:cat", "Categoria bloqueada", None, "celular/notebook etc."
    if PC_GAMER_RE.search(t): return False, "block:pcgamer", "PC Gamer bloqueado", None, "setup completo"

    price = find_lowest_price(t)

    # PRIORIDADES ESPECÍFICAS
    if SPECIFIC_B760M_RE.search(t):
        if price and price < 1000:
            return True, "mobo:b760m", "B760M", price, "< 1000"
        return False, "mobo:b760m", "B760M", price, ">= 1000 ou sem preço"

    if INTEL_14600K_RE.search(t):
        if not price: return False, "cpu:i5-14600k", "i5-14600K", None, "sem preço"
        if price < 400: return False, "cpu:i5-14600k", "i5-14600K", price, "preço irreal (< 400)"
        if price < 1000: return True, "cpu:i5-14600k", "i5-14600K", price, "< 1000"
        return False, "cpu:i5-14600k", "i5-14600K", price, ">= 1000"

    # GPUs - REFORÇADO: RTX 5060 e 5060 Ti com validação
    if RTX5060TI_RE.search(t):
        if not price: return False, "gpu:rtx5060ti", "RTX 5060 Ti", None, "sem preço"
        if price < 1500: return False, "gpu:rtx5060ti", "RTX 5060 Ti", price, "preço irreal (< 1500)"
        if price < 2000: return True, "gpu:rtx5060ti", "RTX 5060 Ti", price, "< 2000"
        return False, "gpu:rtx5060ti", "RTX 5060 Ti", price, ">= 2000"
    
    if RTX5060_RE.search(t):
        if not price: return False, "gpu:rtx5060", "RTX 5060", None, "sem preço"
        if price < 1500: return False, "gpu:rtx5060", "RTX 5060", price, "preço irreal (< 1500)"
        if price < 1900: return True, "gpu:rtx5060", "RTX 5060", price, "< 1900"
        return False, "gpu:rtx5060", "RTX 5060", price, ">= 1900"
    
    if RTX5070_FAM.search(t):
        if not price: return False, "gpu:rtx5070", "RTX 5070/5070 Ti", None, "sem preço"
        if price < 2500: return False, "gpu:rtx5070", "RTX 5070/5070 Ti", price, "preço irreal (< 2500)"
        if price < 3700: return True, "gpu:rtx5070", "RTX 5070/5070 Ti", price, "< 3700"
        return False, "gpu:rtx5070", "RTX 5070/5070 Ti", price, ">= 3700"

    # CPUs - REFORÇADO: i5-14400F ou superior, Ryzen 7 5700X ou superior
    if AMD_BLOCK.search(t): return False, "cpu:amd:block", "CPU AMD inferior", price, "Ryzen 3/5 bloqueado"
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

    # MOBOS - REMOVIDO AMD (B550/X570), BLOQUEADO H610
    if A520_RE.search(t): return False, "mobo:a520", "A520 bloqueada", price, "A520 bloqueada"
    if H610_RE.search(t): return False, "mobo:h610", "H610 bloqueada", price, "H610 bloqueada"
    
    if LGA1700_RE.search(t):
        if not price: return False, "mobo:lga1700", "LGA1700", None, "sem preço"
        if price < 300: return False, "mobo:lga1700", "LGA1700", price, "preço irreal (< 300)"
        if price < 550: return True, "mobo:lga1700", "LGA1700 (B660/B760/Z690/Z790)", price, "< 550"
        return False, "mobo:lga1700", "LGA1700", price, ">= 550"

    # GABINETE - Apenas abaixo de 120, sem restrição de fans
    if GAB_RE.search(t):
        if not price: return False, "case", "Gabinete", None, "sem preço"
        if price < 120: return True, "case", "Gabinete", price, "< 120"
        return False, "case", "Gabinete", price, ">= 120"

    # WATER COOLER - APENAS 240mm abaixo de 200
    if WATER_240MM_RE.search(t):
        if not price: return False, "cooler:water240", "Water Cooler 240mm", None, "sem preço"
        if price < 50: return False, "cooler:water240", "Water Cooler 240mm", price, "preço irreal (< 50)"
        if price < 200: return True, "cooler:water240", "Water Cooler 240mm", price, "< 200"
        return False, "cooler:water240", "Water Cooler 240mm", price, ">= 200"

    # SSD - APENAS Kingston M.2 1TB
    if SSD_RE.search(t) and M2_RE.search(t) and TB1_RE.search(t):
        if not price: return False, "ssd:kingston:m2:1tb", "SSD Kingston M.2 1TB", None, "sem preço"
        if price <= 460: return True, "ssd:kingston:m2:1tb", "SSD Kingston M.2 1TB", price, "<= 460"
        return False, "ssd:kingston:m2:1tb", "SSD Kingston M.2 1TB", price, "> 460"

    # RAM - APENAS Geil Orion 16GB
    if RAM_GEIL_ORION_RE.search(t):
        if not price: return False, "ram:geil_orion", "Memória Geil Orion 16GB", None, "sem preço"
        if price <= 300: return True, "ram:geil_orion", "Memória Geil Orion 16GB DDR4 3200MHz", price, "<= 300"
        return False, "ram:geil_orion", "Memória Geil Orion 16GB", price, "> 300"

    # NOVAS CATEGORIAS
    if CADEIRA_RE.search(t):
        if price and price < 500: return True, "cadeira", "Cadeira Gamer", price, "< 500"
        return False, "cadeira", "Cadeira Gamer", price, ">= 500 ou sem preço"

    # DualSense - AGORA COM "Corre!🔥"
    if DUALSENSE_RE.search(t):
        if not price: return False, "dualsense", "Controle PS5 DualSense", None, "sem preço"
        if price < 200: return False, "dualsense", "Controle PS5 DualSense", price, "preço irreal (< 200)"
        if price < 300: return True, "dualsense", "Controle PS5 DualSense", price, "< 300"
        return False, "dualsense", "Controle PS5 DualSense", price, ">= 300"

    # Ar-condicionados PREMIUM - verifica PRIMEIRO
    if AR_PREMIUM_RE.search(t):
        if not price: return False, "ar_premium", "Ar Condicionado Premium", None, "sem preço"
        if price < 1000: return False, "ar_premium", "Ar Condicionado Premium", price, "preço irreal (< 1000)"
        if price < 1850: return True, "ar_premium", "Ar Condicionado Premium", price, "< 1850"
        return False, "ar_premium", "Ar Condicionado Premium", price, ">= 1850"

    # Ar-condicionados GERAIS (outros modelos)
    if AR_CONDICIONADO_RE.search(t):
        if price and price < 1850: return True, "ar_condicionado", "Ar Condicionado", price, "< 1850"
        return False, "ar_condicionado", "Ar Condicionado", price, ">= 1850 ou sem preço"

    if TENIS_NIKE_RE.search(t):
        if price and price < 250: return True, "tenis_nike", "Tênis Nike", price, "< 250"
        return False, "tenis_nike", "Tênis Nike", price, ">= 250 ou sem preço"

    if WEBCAM_4K_RE.search(t):
        if price and price < 250: return True, "webcam_4k", "Webcam 4K", price, "< 250"
        return False, "webcam_4k", "Webcam 4K", price, ">= 250 ou sem preço"

    # MALA DE BORDO
    if MALA_BORDO_RE.search(t):
        if not price: return False, "mala_bordo", "Mala de Bordo", None, "sem preço"
        if price < 50: return False, "mala_bordo", "Mala de Bordo", price, "preço irreal (< 50)"
        if price < 125: return True, "mala_bordo", "Mala de Bordo", price, "< 125"
        return False, "mala_bordo", "Mala de Bordo", price, ">= 125"

    # MONITOR LG UltraGear 27" 180Hz - ESPECÍFICO COM "Corre!🔥"
    if MONITOR_LG_27_RE.search(t):
        if not price: return False, "monitor:lg27", "Monitor LG UltraGear 27\" 180Hz", None, "sem preço"
        if price < 200: return False, "monitor:lg27", "Monitor LG UltraGear 27\" 180Hz", price, "preço irreal (< 200)"
        if price < 700: return True, "monitor:lg27", "Monitor LG UltraGear 27\" 180Hz", price, "< 700"
        return False, "monitor:lg27", "Monitor LG UltraGear 27\" 180Hz", price, ">= 700"

    # MONITORES - 27"+ APENAS com 144Hz ou superior
    if MONITOR_RE.search(t) and MONITOR_SIZE_RE.search(t) and MONITOR_144HZ_RE.search(t):
        if not price: return False, "monitor", "Monitor 27\"+ 144Hz+", None, "sem preço"
        if price < 200: return False, "monitor", "Monitor 27\"+ 144Hz+", price, "preço irreal (< 200)"
        if price < 700: return True, "monitor", "Monitor 27\"+ 144Hz+", price, "< 700"
        return False, "monitor", "Monitor 27\"+ 144Hz+", price, ">= 700"

    return False, "none", "sem match", price, "sem match"

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
                # keep the newest half
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
            # resolve dialogs to mapping username -> entity for faster chats param
            dialogs = client.get_dialogs()
            uname2ent = {}
            for d in dialogs:
                ent = getattr(d, "entity", None)
                uname = getattr(ent, "username", None)
                if uname:
                    uname2ent[f"@{uname.lower()}"] = ent
            resolved = [uname2ent[u] for u in MONITORED_USERNAMES if u in uname2ent]
            log.info("✅ Monitorando %d canais…", len(resolved))

            # health touch file writer
            def touch_health():
                try:
                    with open(HEALTH_FILE, "w", encoding="utf-8") as hf:
                        hf.write(json.dumps({"pid": PID, "ts": time.time(), "start": START_TS}))
                except Exception:
                    log.exception("Erro ao escrever HEALTH file")

            # initial touch
            touch_health()

            # periodic health updater thread
            def health_loop():
                while True:
                    touch_health()
                    time.sleep(30)
            t = threading.Thread(target=health_loop, daemon=True)
            t.start()

            @client.on(events.NewMessage(chats=resolved or None))
            async def handler(event):
                try:
                    # Some messages are forwarded or contain entities; favor raw_text
                    msg_text = (event.raw_text or "").strip()
                    if not msg_text:
                        # sometimes caption exists
                        msg_text = getattr(event.message, "message", "") or ""
                        msg_text = (msg_text or "").strip()
                    if not msg_text:
                        return

                    chat_id = getattr(event.chat, "id", getattr(event.message, "peer_id", None))
                    msg_id = getattr(event.message, "id", getattr(event, "id", None))

                    if chat_id is None or msg_id is None:
                        # defensive: if missing ids, compute fallback key hash
                        chat_id = "unknown"
                        msg_id = hash(msg_text)

                    if seen.is_dup(chat_id, msg_id):
                        log.debug("Duplicated message ignored chat=%s id=%s", chat_id, msg_id)
                        return

                    ok, key, title, price, reason = classify_and_match(msg_text)
                    chan = getattr(event.chat, "username", "(desconhecido)")
                    chan_disp = f"@{chan}" if chan and chan != "(desconhecido)" else "(desconhecido)"

                    # log common info
                    price_disp = f"{price:.2f}" if isinstance(price, (int, float)) else "None"

                    if ok:
                        header = get_header_text(key) if needs_header(key, price) else ""
                        msg = f"{header}{msg_text}\n\n— via {chan_disp}"
                        log.info("[%-18s] MATCH → %s | price=%s | key=%s | reason=%s | header=%s",
                                 chan_disp, title, price_disp, key, reason, "YES" if header else "NO")
                        # send to destinations (guard exceptions)
                        try:
                            notify_all(msg)
                        except Exception:
                            log.exception("Erro ao notificar destinos")
                        # append match for archive
                        append_match_log({
                            "ts": time.time(),
                            "chan": chan_disp,
                            "title": title,
                            "key": key,
                            "price": price,
                            "reason": reason,
                            "text": msg_text[:4000]  # truncate to avoid huge lines
                        })
                    else:
                        log.info("[%-18s] IGNORADO → %s | price=%s | key=%s | reason=%s",
                                 chan_disp, title, price_disp, key, reason)

                except Exception as e:
                    # do NOT let handler crash the client
                    log.exception("Handler exception: %s", e)

            # run until disconnected
            client.run_until_disconnected()

        except Exception as e:
            log.exception("Erro fatal no main: %s", e)
        finally:
            # on exit persist seen and matches
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
        # touch health file to mark shutdown
        with open(HEALTH_FILE, "w", encoding="utf-8") as hf:
            hf.write(json.dumps({"pid": PID, "ts": time.time(), "shutdown": True}))
    except Exception:
        pass
    # not calling sys.exit() here because Render/runner will stop process after signal

atexit.register(_on_exit)
signal.signal(signal.SIGTERM, _on_exit)
signal.signal(signal.SIGINT, _on_exit)

# ---------------------------------------------
# Entrypoint
# ---------------------------------------------
if __name__ == "__main__":
    main()
