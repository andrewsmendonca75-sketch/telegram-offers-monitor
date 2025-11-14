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
# PRICE PARSER (BRL) - Versão Otimizada Final
# ---------------------------------------------
# Padrões principais de preço
PRICE_MAIN_RE = re.compile(
    r"(?i)(?:r\$|por|preço|valor|de)\s*r?\$?\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d{3,}(?:,\d{2})?)",
    re.I
)

# Padrão fallback (qualquer R$ seguido de número)
PRICE_FALLBACK_RE = re.compile(
    r"(?i)r\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d{3,}(?:,\d{2})?)",
    re.I
)

# Contextos a ignorar
IGNORE_PRICE_CONTEXT = re.compile(
    r"(?i)(cupom|desconto|off|cashback|moedas?|pontos?|em\s+\d+x|parcelas?|frete|resgate)",
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
    Busca o menor preço válido no texto com múltiplas estratégias
    """
    vals: List[float] = []
    lines = text.split('\n')
    
    # Estratégia 1: Busca com padrão principal (mais rigoroso)
    for line in lines:
        if IGNORE_PRICE_CONTEXT.search(line):
            continue
        for m in PRICE_MAIN_RE.finditer(line):
            v = _to_float_brl(m.group(1))
            if v and v >= 50:  # Preços realistas mínimo 50
                vals.append(v)
    
    # Estratégia 2: Se não achou, tenta fallback mais flexível
    if not vals:
        for line in lines:
            # Pula linhas com contexto de cupom/desconto
            if IGNORE_PRICE_CONTEXT.search(line):
                continue
            for m in PRICE_FALLBACK_RE.finditer(line):
                v = _to_float_brl(m.group(1))
                if v and v >= 50:
                    vals.append(v)
    
    # Estratégia 3: Busca "à vista" ou "no pix" (mais preciso)
    if not vals:
        VISTA_RE = re.compile(r"(?i)r\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*(?:à vista|no pix)", re.I)
        for m in VISTA_RE.finditer(text):
            v = _to_float_brl(m.group(1))
            if v:
                vals.append(v)
    
    return min(vals) if vals else None

# ---------------------------------------------
# REGEX RULES
# ---------------------------------------------
BLOCK_CATS = re.compile(r"\b(celular|smartphone|iphone|android|notebook|laptop|macbook)\b", re.I)
PC_GAMER_RE = re.compile(r"\b(pc\s*gamer|setup\s*completo|kit\s*completo)\b", re.I)

# GPUs - REMOVIDAS: RTX 5050 e RX 7600
RTX5060_RE   = re.compile(r"\brtx\s*5060(?!\s*ti)\b", re.I)
RTX5070_FAM  = re.compile(r"\brtx\s*5070(\s*ti)?\b", re.I)

# CPUs
INTEL_SUP = re.compile(r"\b(i(?:5|7|9)[-\s]*(?:12|13|14)\d{2,3}k?f?)\b", re.I)
AMD_SUP   = re.compile(r"\b(ryzen\s*(?:7\s*5700x?|7\s*5800x3?d?|9\s*5900x|9\s*5950x))\b", re.I)
AMD_BLOCK = re.compile(r"\b(ryzen\s*(?:3|5)|5600g?t?)\b", re.I)

# Mobos
A520_RE     = re.compile(r"\ba520m?\b", re.I)
B550_FAM_RE = re.compile(r"\bb550m?\b|\bx570\b", re.I)
LGA1700_RE  = re.compile(r"\b(h610m?|b660m?|b760m?|z690|z790)\b", re.I)

SPECIFIC_B760M_RE = re.compile(r"\bb760m\b", re.I)
INTEL_14600K_RE   = re.compile(r"\bi5[-\s]*14600k\b", re.I)

# Gabinete
GAB_RE     = re.compile(r"\bgabinete\b", re.I)
FANS_HINT  = re.compile(r"(?:(\d+)\s*(?:fans?|coolers?|ventoinhas?)|(\d+)\s*x\s*120\s*mm|(\d+)\s*x\s*fan)", re.I)

# Coolers
WATER_RE   = re.compile(r"\bwater\s*cooler\b", re.I)
AIR_COOLER = re.compile(r"\bcooler\b", re.I)

# SSD
SSD_RE  = re.compile(r"\bssd\b", re.I)
M2_RE   = re.compile(r"\bm\.?2\b|\bnvme\b", re.I)
TB1_RE  = re.compile(r"\b1\s*tb\b", re.I)

# RAM
RAM_RE  = re.compile(r"\bddr4\b", re.I)
GB16_RE = re.compile(r"\b16\s*gb\b", re.I)
GB8_RE  = re.compile(r"\b8\s*gb\b", re.I)

# NOVAS CATEGORIAS
CADEIRA_RE = re.compile(r"\bcadeira\b", re.I)
DUALSENSE_RE = re.compile(r"\b(dualsense|controle\s*ps5|controle\s*playstation\s*5)\b", re.I)
WIFI_BT_RE = re.compile(r"\b(adaptador\s*wifi|adaptador\s*bluetooth|wifi\s*bluetooth|placa\s*wifi)\b", re.I)
AR_CONDICIONADO_RE = re.compile(r"\b(ar\s*condicionado|split|inverter)\b", re.I)

# Ar-condicionados PREMIUM específicos (com Oportunidade🔥)
AR_PREMIUM_RE = re.compile(
    r"\b(daikin\s+ecoswing|fujitsu\s+premium|samsung\s+windfree|elgin\s+eco\s+ii|gree\s+g[-\s]*top)\b",
    re.I
)

TENIS_NIKE_RE = re.compile(r"\b(tênis|tenis)\s*(nike|air\s*max|air\s*force|jordan)\b", re.I)
WEBCAM_4K_RE = re.compile(r"\bwebcam\b.*\b4k\b|\b4k\b.*\bwebcam\b", re.I)

# ---------------------------------------------
# HELPERS
# ---------------------------------------------
def count_fans(text: str) -> int:
    n = 0
    for m in FANS_HINT.finditer(text):
        for g in m.groups():
            if g and g.isdigit():
                n = max(n, int(g))
    return n

def needs_header(product_key: str, price: Optional[float]) -> bool:
    """Define quando usar cabeçalho 'Corre!🔥' ou 'Oportunidade🔥'"""
    if not price: return False
    if product_key == "gpu:rtx5060" and price < 1900: return True
    if product_key.startswith("cpu:") and price < 900: return True
    if product_key == "ar_premium" and price < 1850: return True  # Ar-condicionados premium
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
        if price and price < 1000:
            return True, "cpu:i5-14600k", "i5-14600K", price, "< 1000"
        return False, "cpu:i5-14600k", "i5-14600K", price, ">= 1000 ou sem preço"

    # GPUs - AJUSTADAS: removido 5050 e 7600, validação de preço realista
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

    # CPUs
    if AMD_BLOCK.search(t): return False, "cpu:amd:block", "CPU AMD inferior", price, "Ryzen 3/5 bloqueado"
    if INTEL_SUP.search(t):
        if not price: return False, "cpu:intel", "CPU Intel sup.", None, "sem preço"
        if price < 400: return False, "cpu:intel", "CPU Intel sup.", price, "preço irreal (< 400)"
        if price < 900: return True, "cpu:intel", "CPU Intel sup.", price, "< 900"
        return False, "cpu:intel", "CPU Intel sup.", price, ">= 900"
    if AMD_SUP.search(t):
        if not price: return False, "cpu:amd", "CPU AMD sup.", None, "sem preço"
        if price < 400: return False, "cpu:amd", "CPU AMD sup.", price, "preço irreal (< 400)"
        if price < 900: return True, "cpu:amd", "CPU AMD sup.", price, "< 900"
        return False, "cpu:amd", "CPU AMD sup.", price, ">= 900"

    # MOBOS
    if A520_RE.search(t): return False, "mobo:a520", "A520 bloqueada", price, "A520 bloqueada"
    if B550_FAM_RE.search(t):
        if not price: return False, "mobo:am4", "B550/X570", None, "sem preço"
        if price < 300: return False, "mobo:am4", "B550/X570", price, "preço irreal (< 300)"
        if price < 550: return True, "mobo:am4", "B550/X570", price, "< 550"
        return False, "mobo:am4", "B550/X570", price, ">= 550"
    if LGA1700_RE.search(t):
        if not price: return False, "mobo:lga1700", "LGA1700", None, "sem preço"
        if price < 300: return False, "mobo:lga1700", "LGA1700", price, "preço irreal (< 300)"
        if price < 550: return True, "mobo:lga1700", "LGA1700", price, "< 550"
        return False, "mobo:lga1700", "LGA1700", price, ">= 550"

    # GABINETE
    if GAB_RE.search(t):
        fans = count_fans(t)
        if not price: return False, "case", "Gabinete", price, "sem preço"
        if (fans == 3 and price <= 160) or (fans >= 4 and price <= 220):
            return True, "case", "Gabinete", price, f"{fans} fans ok"
        return False, "case", "Gabinete", price, "fora das regras"

    # COOLERS
    if WATER_RE.search(t):
        if not price: return False, "cooler:water", "Water Cooler", None, "sem preço"
        if price < 50: return False, "cooler:water", "Water Cooler", price, "preço irreal (< 50)"
        if price <= 150: return True, "cooler:water", "Water Cooler", price, "<= 150"
        return False, "cooler:water", "Water Cooler", price, "> 150"
    
    if AIR_COOLER.search(t) and not WATER_RE.search(t):
        if not price: return False, "cooler:air", "Cooler (ar)", None, "sem preço"
        if price < 50: return False, "cooler:air", "Cooler (ar)", price, "preço irreal (< 50)"
        if price <= 150: return True, "cooler:air", "Cooler (ar)", price, "<= 150"
        return False, "cooler:air", "Cooler (ar)", price, "> 150"

    # SSD
    if SSD_RE.search(t) and M2_RE.search(t) and TB1_RE.search(t):
        if price and price <= 460: return True, "ssd:m2:1tb", "SSD M.2 1TB", price, "<= 460"
        return False, "ssd:m2:1tb", "SSD M.2 1TB", price, "> 460 ou sem preço"

    # RAM
    if RAM_RE.search(t):
        if GB16_RE.search(t) and price and price <= 300: return True, "ram:16", "DDR4 16GB", price, "<= 300"
        if GB8_RE.search(t) and price and price <= 150: return True, "ram:8", "DDR4 8GB", price, "<= 150"

    # NOVAS CATEGORIAS
    if CADEIRA_RE.search(t):
        if price and price < 500: return True, "cadeira", "Cadeira Gamer", price, "< 500"
        return False, "cadeira", "Cadeira Gamer", price, ">= 500 ou sem preço"

    if DUALSENSE_RE.search(t):
        if not price: return False, "dualsense", "Controle PS5 DualSense", None, "sem preço"
        if price < 200: return False, "dualsense", "Controle PS5 DualSense", price, "preço irreal (< 200)"
        if price < 300: return True, "dualsense", "Controle PS5 DualSense", price, "< 300"
        return False, "dualsense", "Controle PS5 DualSense", price, ">= 300"

    if WIFI_BT_RE.search(t):
        if price and price < 250: return True, "wifi_bt", "Adaptador WiFi/Bluetooth", price, "< 250"
        return False, "wifi_bt", "Adaptador WiFi/Bluetooth", price, ">= 250 ou sem preço"

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
                        log.info("[%-18s] MATCH → %s | price=%s | key=%s | reason=%s",
                                 chan_disp, title, price_disp, key, reason)
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
