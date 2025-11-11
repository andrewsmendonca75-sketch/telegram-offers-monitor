# -*- coding: utf-8 -*-
"""
realtime.py
Monitor de canais/grupos no Telegram com filtros e alertas.

Destaques:
- Auto-bind HEALTHCHECK_PORT a partir de PORT (plataformas como Railway/Render/Heroku)
- Servidor HTTP embutido (/healthz e /metrics) para o deploy marcar como "UP"
- Fallback de envio: Bot API (se TELEGRAM_TOKEN) -> Telethon (mensagem direta)
- Suporte a @usernames (MONITORED_CHANNELS) e IDs (MONITORED_CHANNEL_IDS)
- Regras de classificação (GPU, CPU, mobo, gabinete, cooler, SSD, RAM)
- Parser de preço robusto para BRL
"""

import os
import re
import time
import json
import logging
import threading
from typing import List, Optional, Tuple, Dict, Union

import requests
from http.server import BaseHTTPRequestHandler, HTTPServer

from telethon import events
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

# -----------------------------------------------------------------------------
# LOGGING
# -----------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | [%(name)s] %(message)s",
)
log = logging.getLogger(os.getenv("APP_NAME", "monitor"))

VERSION = os.getenv("VERSION", "1.2.0")

# -----------------------------------------------------------------------------
# AUTO-BIND HEALTHCHECK_PORT <- PORT
# -----------------------------------------------------------------------------
_port_env = os.getenv("PORT", "")
if _port_env and not os.getenv("HEALTHCHECK_PORT"):
    os.environ["HEALTHCHECK_PORT"] = _port_env

HEALTHCHECK_PORT = os.getenv("HEALTHCHECK_PORT", "")
METRICS = {
    "version": VERSION,
    "start_ts": int(time.time()),
    "messages_seen": 0,
    "matches_sent": 0,
    "last_match_ts": 0,
}

# -----------------------------------------------------------------------------
# ENV OBRIGATÓRIAS
# -----------------------------------------------------------------------------
def _require(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        log.error("Variável obrigatória ausente: %s", name)
        raise SystemExit(1)
    return val

API_ID = int(_require("TELEGRAM_API_ID"))
API_HASH = _require("TELEGRAM_API_HASH")
STRING_SESSION = _require("TELEGRAM_STRING_SESSION")

# Opcional: Bot API (melhor para enviar para grupos/canais por ID)
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
BOT_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None

# Destinos de notificação (pode ser lista de IDs numéricos e/ou @usernames)
USER_DESTINATIONS_RAW = os.getenv("USER_DESTINATIONS", os.getenv("USER_CHAT_ID", "")).strip()

# Canais/Grupos monitorados
MONITORED_CHANNELS_RAW = os.getenv("MONITORED_CHANNELS", "").strip()           # "@canal1,@canal2"
MONITORED_CHANNEL_IDS_RAW = os.getenv("MONITORED_CHANNEL_IDS", "").strip()     # "-1001234567890,-1002222"

# -----------------------------------------------------------------------------
# HELPERS ENV
# -----------------------------------------------------------------------------
def _split_csv(val: str) -> List[str]:
    return [p.strip() for p in val.split(",") if p and p.strip()]

def _norm_username(u: str) -> Optional[str]:
    if not u:
        return None
    u = u.strip()
    if not u:
        return None
    # se for só dígitos, não é username
    if re.fullmatch(r"\d+", u):
        return None
    u = u.lower()
    if not u.startswith("@"):
        u = "@" + u
    return u

# montar listas normalizadas
MONITORED_USERNAMES: List[str] = []
for x in _split_csv(MONITORED_CHANNELS_RAW):
    nu = _norm_username(x)
    if nu:
        MONITORED_USERNAMES.append(nu)

MONITORED_IDS: List[int] = []
for x in _split_csv(MONITORED_CHANNEL_IDS_RAW):
    if re.fullmatch(r"-?\d+", x):
        try:
            MONITORED_IDS.append(int(x))
        except Exception:
            pass

USER_DESTINATIONS: List[str] = _split_csv(USER_DESTINATIONS_RAW)

if not MONITORED_USERNAMES and not MONITORED_IDS:
    log.warning("MONITORED_CHANNELS/IDS vazio — o handler não terá filtro de chats (rastreará todos os acessíveis).")

if not USER_DESTINATIONS:
    log.warning("USER_DESTINATIONS/USER_CHAT_ID não definido; os matches não serão enviados (apenas logados).")
else:
    log.info("📬 Destinos: %s", ", ".join(USER_DESTINATIONS))

if MONITORED_USERNAMES:
    log.info("▶️ Canais/Grupos (@): %s", ", ".join(MONITORED_USERNAMES))
if MONITORED_IDS:
    log.info("▶️ Canais/Grupos (IDs): %s", ", ".join(str(i) for i in MONITORED_IDS))

# -----------------------------------------------------------------------------
# HEALTHCHECK SERVER
# -----------------------------------------------------------------------------
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(METRICS).encode("utf-8"))
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        # silencia logs HTTP padrão
        return

def _start_health_server(port: int):
    def _serve():
        try:
            httpd = HTTPServer(("0.0.0.0", port), _HealthHandler)
            log.info("🌐 Healthcheck ativo em :%d (/healthz, /metrics)", port)
            httpd.serve_forever()
        except Exception as e:
            log.error("Falha ao iniciar healthcheck: %r", e)
    t = threading.Thread(target=_serve, daemon=True)
    t.start()

if HEALTHCHECK_PORT and re.fullmatch(r"\d+", HEALTHCHECK_PORT):
    _start_health_server(int(HEALTHCHECK_PORT))

# -----------------------------------------------------------------------------
# BOT SENDER
# -----------------------------------------------------------------------------
def bot_send_text(dest: Union[str, int], text: str) -> Tuple[bool, str]:
    if not BOT_BASE:
        return False, "BOT_TOKEN ausente"
    payload = {"chat_id": dest, "text": text, "disable_web_page_preview": True}
    try:
        r = requests.post(f"{BOT_BASE}/sendMessage", json=payload, timeout=20)
        if r.status_code == 200 and r.json().get("ok"):
            return True, "ok"
        return False, r.text
    except Exception as e:
        return False, repr(e)

async def client_send_text(client: TelegramClient, dest: str, text: str) -> Tuple[bool, str]:
    """Envia via Telethon como fallback. 'dest' pode ser ID numérico ('-100...') ou '@user/@canal'."""
    try:
        target: Union[int, str]
        if re.fullmatch(r"-?\d+", str(dest)):
            target = int(dest)
        else:
            target = dest
        entity = await client.get_entity(target)
        await client.send_message(entity, text)
        return True, "ok"
    except Exception as e:
        return False, repr(e)

async def notify_all(client: TelegramClient, text: str):
    if not USER_DESTINATIONS:
        log.info("Nenhum destino configurado; mensagem não enviada.")
        return
    for d in USER_DESTINATIONS:
        ok, msg = bot_send_text(d, text)
        if ok:
            log.info("· envio=ok(bot) → %s", d)
            continue
        # fallback
        ok2, msg2 = await client_send_text(client, d, text)
        if ok2:
            log.info("· envio=ok(client) → %s", d)
        else:
            log.error("· envio=ERRO → %s | bot=%s | client=%s", d, msg, msg2)

# -----------------------------------------------------------------------------
# PRICE PARSER
# -----------------------------------------------------------------------------
PRICE_RE = re.compile(
    r"(?i)(r\$\s*)?("
    r"(?:\d{1,3}(?:\.\d{3})+(?:,\d{2})?)"   # 1.234,56
    r"|(?:\d{3,}(?:,\d{2})?)"               # 1234,56  / 1234
    r"|(?:\d+,\d{2})"                       # 99,90
    r")"
)

def _to_float_brl(raw: str) -> Optional[float]:
    s = raw.strip().replace(".", "").replace(",", ".")
    try:
        v = float(s)
        return v if 0 < v < 100000 else None
    except Exception:
        return None

def find_lowest_price(text: str) -> Optional[float]:
    vals: List[float] = []
    for m in PRICE_RE.finditer(text):
        has_r = bool(m.group(1))
        raw = m.group(2)
        # evita números estilo milhar sem vírgula como preço sem "R$"
        if "." in raw and "," not in raw and not has_r:
            continue
        v = _to_float_brl(raw)
        if not v or v < 5:
            continue
        if v < 100 and not has_r:
            continue
        vals.append(v)
    return min(vals) if vals else None

# -----------------------------------------------------------------------------
# REGEX DE CLASSIFICAÇÃO
# -----------------------------------------------------------------------------
BLOCK_CATS = re.compile(r"\b(celular|smartphone|iphone|android|notebook|laptop|macbook)\b", re.I)
PC_GAMER_RE = re.compile(r"\b(pc\s*gamer|setup\s*completo|kit\s*completo)\b", re.I)

RTX5050_RE   = re.compile(r"\brtx\s*5050\b", re.I)
RTX5060_RE   = re.compile(r"\brtx\s*5060(?!\s*ti)\b", re.I)
RTX5070_FAM  = re.compile(r"\brtx\s*5070(\s*ti)?\b", re.I)
RX7600_RE    = re.compile(r"\brx\s*7600\b", re.I)

INTEL_SUP = re.compile(r"\b(i(?:5|7|9)[-\s]*(?:12|13|14)\d{2,3}k?f?)\b", re.I)
AMD_SUP   = re.compile(r"\b(ryzen\s*(?:7\s*5700x?|7\s*5800x3?d?|9\s*5900x|9\s*5950x))\b", re.I)
AMD_BLOCK = re.compile(r"\b(ryzen\s*(?:3|5)|5600g?t?)\b", re.I)

A520_RE     = re.compile(r"\ba520m?\b", re.I)
B550_FAM_RE = re.compile(r"\bb550m?\b|\bx570\b", re.I)
LGA1700_RE  = re.compile(r"\b(h610m?|b660m?|b760m?|z690|z790)\b", re.I)

SPECIFIC_B760M_RE = re.compile(r"\bb760m\b", re.I)
INTEL_14600K_RE   = re.compile(r"\bi5[-\s]*14600k\b", re.I)

GAB_RE     = re.compile(r"\bgabinete\b", re.I)
FANS_HINT  = re.compile(r"(?:(\d+)\s*(?:fans?|coolers?|ventoinhas?)|(\d+)\s*x\s*120\s*mm|(\d+)\s*x\s*fan)", re.I)
WATER_RE   = re.compile(r"\bwater\s*cooler\b", re.I)
AIR_COOLER = re.compile(r"\bcooler\b", re.I)

SSD_RE  = re.compile(r"\bssd\b", re.I)
M2_RE   = re.compile(r"\bm\.?2\b|\bnvme\b", re.I)
TB1_RE  = re.compile(r"\b1\s*tb\b", re.I)
RAM_RE  = re.compile(r"\bddr4\b", re.I)
GB16_RE = re.compile(r"\b16\s*gb\b", re.I)
GB8_RE  = re.compile(r"\b8\s*gb\b", re.I)

def count_fans(text: str) -> int:
    n = 0
    for m in FANS_HINT.finditer(text):
        for g in m.groups():
            if g and g.isdigit():
                n = max(n, int(g))
    return n

def needs_header(product_key: str, price: Optional[float]) -> bool:
    if not price:
        return False
    if product_key == "gpu:rtx5060" and price < 1900:
        return True
    if product_key.startswith("cpu:") and price < 900:
        return True
    return False

def classify_and_match(text: str):
    t = text
    if BLOCK_CATS.search(t):
        return False, "block:cat", "Categoria bloqueada", None, "celular/notebook etc."
    if PC_GAMER_RE.search(t):
        return False, "block:pcgamer", "PC Gamer bloqueado", None, "setup completo"

    price = find_lowest_price(t)

    # PRIORIDADES
    if SPECIFIC_B760M_RE.search(t):
        if price and price < 1000:
            return True, "mobo:b760m", "B760M", price, "< 1000"
        return False, "mobo:b760m", "B760M", price, ">= 1000 ou sem preço"

    if INTEL_14600K_RE.search(t):
        if price and price < 1000:
            return True, "cpu:i5-14600k", "i5-14600K", price, "< 1000"
        return False, "cpu:i5-14600k", "i5-14600K", price, ">= 1000 ou sem preço"

    # GPUs
    if RTX5050_RE.search(t):
        if price and price < 1700:
            return True, "gpu:rtx5050", "RTX 5050", price, "< 1700"
        return False, "gpu:rtx5050", "RTX 5050", price, ">= 1700 ou sem preço"
    if RTX5060_RE.search(t):
        if price and price < 1900:
            return True, "gpu:rtx5060", "RTX 5060", price, "< 1900"
        return False, "gpu:rtx5060", "RTX 5060", price, ">= 1900 ou sem preço"
    if RTX5070_FAM.search(t):
        if price and price < 3860:
            return True, "gpu:rtx5070", "RTX 5070/5070 Ti", price, "< 3860"
        return False, "gpu:rtx5070", "RTX 5070/5070 Ti", price, ">= 3860 ou sem preço"
    if RX7600_RE.search(t):
        if price and price < 1700:
            return True, "gpu:rx7600", "RX 7600", price, "< 1700"
        return False, "gpu:rx7600", "RX 7600", price, ">= 1700 ou sem preço"

    # CPUs
    if AMD_BLOCK.search(t):
        return False, "cpu:amd:block", "CPU AMD inferior", price, "Ryzen 3/5 bloqueado"
    if INTEL_SUP.search(t):
        if price and price < 900:
            return True, "cpu:intel", "CPU Intel sup.", price, "< 900"
        return False, "cpu:intel", "CPU Intel sup.", price, ">= 900 ou sem preço"
    if AMD_SUP.search(t):
        if price and price < 900:
            return True, "cpu:amd", "CPU AMD sup.", price, "< 900"
        return False, "cpu:amd", "CPU AMD sup.", price, ">= 900 ou sem preço"

    # MOBOS
    if A520_RE.search(t):
        return False, "mobo:a520", "A520 bloqueada", price, "A520 bloqueada"
    if B550_FAM_RE.search(t):
        if price and price < 550:
            return True, "mobo:am4", "B550/X570", price, "< 550"
        return False, "mobo:am4", "B550/X570", price, ">= 550 ou sem preço"
    if LGA1700_RE.search(t):
        if price and price < 550:
            return True, "mobo:lga1700", "LGA1700", price, "< 550"
        return False, "mobo:lga1700", "LGA1700", price, ">= 550 ou sem preço"

    # GABINETE
    if GAB_RE.search(t):
        fans = count_fans(t)
        if not price:
            return False, "case", "Gabinete", price, "sem preço"
        if (fans == 3 and price <= 160) or (fans >= 4 and price <= 220):
            return True, "case", "Gabinete", price, f"{fans} fans ok"
        return False, "case", "Gabinete", price, "fora das regras"

    # COOLERS
    if WATER_RE.search(t) and price and price <= 150:
        return True, "cooler:water", "Water Cooler", price, "<= 150"
    if AIR_COOLER.search(t) and not WATER_RE.search(t) and price and price <= 150:
        return True, "cooler:air", "Cooler (ar)", price, "<= 150"

    # SSD
    if SSD_RE.search(t) and M2_RE.search(t) and TB1_RE.search(t):
        if price and price <= 460:
            return True, "ssd:m2:1tb", "SSD M.2 1TB", price, "<= 460"
        return False, "ssd:m2:1tb", "SSD M.2 1TB", price, "> 460 ou sem preço"

    # RAM
    if RAM_RE.search(t):
        if GB16_RE.search(t) and price and price <= 300:
            return True, "ram:16", "DDR4 16GB", price, "<= 300"
        if GB8_RE.search(t) and price and price <= 150:
            return True, "ram:8", "DDR4 8GB", price, "<= 150"

    return False, "none", "sem match", price, "sem match"

# -----------------------------------------------------------------------------
# DUP GUARD
# -----------------------------------------------------------------------------
class Seen:
    def __init__(self, maxlen=1200):
        self.maxlen = maxlen
        self.data: Dict[int, float] = {}

    def is_dup(self, msg_id: int) -> bool:
        if msg_id in self.data:
            return True
        # limpeza simples para não crescer infinito
        if len(self.data) > self.maxlen:
            for k in list(self.data)[: self.maxlen // 2]:
                del self.data[k]
        self.data[msg_id] = time.time()
        return False

seen = Seen()

# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------
def main():
    log.info("Iniciando v%s — health_port=%s", VERSION, HEALTHCHECK_PORT or "off")
    with TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH) as client:
        log.info("Conectando ao Telegram…")
        client.connect()
        if not client.is_user_authorized():
            log.error("STRING_SESSION inválida/expirada — gere novamente.")
            raise SystemExit(2)
        me = client.get_me()
        log.info("Conectado como: %s (id=%s)", getattr(me, "username", None) or me.first_name, me.id)

        # Resolver usernames para entidades
        resolved_entities = []
        uname2ent: Dict[str, object] = {}
        try:
            dialogs = client.get_dialogs()
            for d in dialogs:
                uname = getattr(d.entity, "username", None)
                if uname:
                    uname2ent[f"@{uname.lower()}"] = d.entity
        except Exception as e:
            log.warning("Falha ao carregar diálogos: %r (seguiremos com resolução on-demand)", e)

        for u in MONITORED_USERNAMES:
            if u in uname2ent:
                resolved_entities.append(uname2ent[u])
            else:
                try:
                    ent = client.get_entity(u)
                    resolved_entities.append(ent)
                except Exception as e:
                    log.warning("Não foi possível resolver %s: %r", u, e)

        # Adiciona IDs direto
        for cid in MONITORED_IDS:
            try:
                ent = client.get_entity(cid)
                resolved_entities.append(ent)
            except Exception as e:
                log.warning("Não foi possível resolver ID %s: %r", cid, e)

        if resolved_entities:
            log.info("✅ Monitorando %d chats específicos.", len(resolved_entities))
        else:
            log.warning("⚠️ Nenhum chat resolvido — o handler ouvirá TODOS os chats acessíveis pela conta.")

        # Mensagem de boot (se tiver destino)
        try:
            if USER_DESTINATIONS:
                boot_msg = f"✅ Bot ON (v{VERSION}) — {time.strftime('%Y-%m-%d %H:%M:%S')}"
                client.loop.run_until_complete(notify_all(client, boot_msg))
        except Exception as e:
            log.warning("Falha ao enviar mensagem de boot: %r", e)

        @client.on(events.NewMessage(chats=resolved_entities or None))
        async def handler(event):
            # Contagem básica
            METRICS["messages_seen"] += 1

            if seen.is_dup(event.id):
                return

            text = (event.raw_text or "").strip()
            if not text:
                return

            ok, key, title, price, reason = classify_and_match(text)
            chan_username = getattr(event.chat, "username", None)
            chan_disp = f"@{chan_username}" if chan_username else f"id:{getattr(event.chat, 'id', 'desconhecido')}"

            if ok:
                header = "Corre!🔥 " if needs_header(key, price) else ""
                msg = f"{header}{text}\n\n— via {chan_disp}"
                log.info("[%-18s] MATCH → %s | price=%s | key=%s | reason=%s",
                         chan_disp, title, f"{price:.2f}" if price else "None", key, reason)
                try:
                    await notify_all(client, msg)
                    METRICS["matches_sent"] += 1
                    METRICS["last_match_ts"] = int(time.time())
                except Exception as e:
                    log.error("Falha ao notificar: %r", e)
            else:
                log.info("[%-18s] IGNORADO → %s | price=%s | key=%s | reason=%s",
                         chan_disp, title, f"{price:.2f}" if price else "None", key, reason)

        log.info("Aguardando mensagens…")
        client.run_until_disconnected()

if __name__ == "__main__":
    main()
