# -*- coding: utf-8 -*-
import os
import re
import time
import json
import logging
import traceback
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List, Optional, Tuple, Dict, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from telethon import events
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

# ---------------------------------------------
# LOGGING
# ---------------------------------------------
APP_NAME = os.getenv("APP_NAME", "monitor")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(APP_NAME)

# ---------------------------------------------
# ENV / CONFIG
# ---------------------------------------------
def _req(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise SystemExit(f"ENV obrigatório ausente: {name}")
    return v

API_ID = int(_req("TELEGRAM_API_ID"))
API_HASH = _req("TELEGRAM_API_HASH")
STRING_SESSION = _req("TELEGRAM_STRING_SESSION")

# Opcional: se fornecer, envia via Bot API; senão, envia com o próprio client
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()

MONITORED_CHANNELS_RAW = os.getenv("MONITORED_CHANNELS", "")  # @user1,@user2
MONITORED_CHANNEL_IDS_RAW = os.getenv("MONITORED_CHANNEL_IDS", "")  # 100123,-100999

USER_DESTINATIONS_RAW = os.getenv("USER_DESTINATIONS", os.getenv("USER_CHAT_ID", ""))  # ids separados por vírgula

HEALTHCHECK_PORT = int(os.getenv("HEALTHCHECK_PORT", "0")) or None
VERSION = os.getenv("VERSION", "1.2.0")

# ---------------------------------------------
# HELPERS (CSV/normalização)
# ---------------------------------------------
def _split_csv(val: str) -> List[str]:
    return [p.strip() for p in val.split(",") if p and p.strip()]

def _norm_username(u: str) -> Optional[str]:
    if not u:
        return None
    u = u.strip()
    if not u:
        return None
    # se é puro número, não é @username
    if re.fullmatch(r"-?\d+", u):
        return None
    u = u.lower()
    if not u.startswith("@"):
        u = "@"+u
    return u

def _to_int_list(csv_: str) -> List[int]:
    out: List[int] = []
    for x in _split_csv(csv_):
        try:
            out.append(int(x))
        except:
            log.warning("Ignorando MONITORED_CHANNEL_IDS inválido: %r", x)
    return out

# Listas finais de monitoramento
MONITORED_USERNAMES: List[str] = []
for x in _split_csv(MONITORED_CHANNELS_RAW):
    nu = _norm_username(x)
    if nu:
        MONITORED_USERNAMES.append(nu)

MONITORED_IDS: List[int] = _to_int_list(MONITORED_CHANNEL_IDS_RAW)

if not MONITORED_USERNAMES and not MONITORED_IDS:
    log.warning("MONITORED_CHANNELS/MONITORED_CHANNEL_IDS vazio(s) — nada será filtrado explicitamente.")

# destinos (strings, podem ser @user ou id numérico)
USER_DESTINATIONS: List[str] = _split_csv(USER_DESTINATIONS_RAW)
if not USER_DESTINATIONS:
    log.warning("USER_DESTINATIONS/USER_CHAT_ID não definido; nada será enviado.")

# ---------------------------------------------
# HTTP (Bot API) com retry/backoff
# ---------------------------------------------
BOT_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""
_session = None
if BOT_TOKEN:
    _session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["POST", "GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
    _session.mount("https://", adapter)
    _session.mount("http://", adapter)

def _tg_send_via_bot(dest: str, text: str) -> Tuple[bool, str]:
    if not BOT_TOKEN:
        return False, "BOT_TOKEN ausente"
    payload = {"chat_id": dest, "text": text, "disable_web_page_preview": True}
    try:
        r = _session.post(f"{BOT_BASE}/sendMessage", json=payload, timeout=20)
        if r.status_code == 200 and r.json().get("ok"):
            return True, "ok"
        return False, r.text
    except Exception as e:
        return False, repr(e)

# ---------------------------------------------
# PRICE PARSER (BRL)
# ---------------------------------------------
PRICE_RE = re.compile(
    r"(?i)(r\$\s*)?("
    r"(?:\d{1,3}(?:\.\d{3})+(?:,\d{2})?)"
    r"|(?:\d{3,}(?:,\d{2})?)"
    r"|(?:\d+,\d{2})"
    r")"
)

def _to_float_brl(raw: str) -> Optional[float]:
    s = raw.strip().replace(".", "").replace(",", ".")
    try:
        v = float(s)
        # limites de sanidade
        return v if 0 < v < 100000 else None
    except:
        return None

def find_lowest_price(text: str) -> Optional[float]:
    vals: List[float] = []
    for m in PRICE_RE.finditer(text):
        has_r = bool(m.group(1))
        raw = m.group(2)
        # ignora 1.099 (milhar ponto, sem vírgula e sem R$)
        if "." in raw and "," not in raw and not has_r:
            continue
        v = _to_float_brl(raw)
        if not v or v < 5:
            continue
        # < 100 sem R$ costuma ser “frete 50,00”, etc.
        if v < 100 and not has_r:
            continue
        vals.append(v)
    return min(vals) if vals else None

# ---------------------------------------------
# REGEX / REGRAS
# ---------------------------------------------
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
    # Blocks
    if BLOCK_CATS.search(t):
        return False, "block:cat", "Categoria bloqueada", None, "celular/notebook etc."
    if PC_GAMER_RE.search(t):
        return False, "block:pcgamer", "PC Gamer bloqueado", None, "setup completo"

    price = find_lowest_price(t)

    # Prioridades
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

# ---------------------------------------------
# DUP GUARD
# ---------------------------------------------
class Seen:
    def __init__(self, maxlen=1200):
        self.maxlen = maxlen
        self.data: Dict[int, float] = {}
    def is_dup(self, msg_id: int) -> bool:
        if msg_id in self.data:
            return True
        if len(self.data) > self.maxlen:
            # prune metade
            for k in list(self.data)[: self.maxlen // 2]:
                del self.data[k]
        self.data[msg_id] = time.time()
        return False

seen = Seen()

# ---------------------------------------------
# HEALTHCHECK / METRICS
# ---------------------------------------------
_metrics = {"starts": 0, "messages": 0, "matches": 0, "errors": 0, "uptime_start": 0}

class _HC(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.path == "/healthz":
            self._json(200, {"ok": True, "app": APP_NAME, "version": VERSION})
        elif self.path == "/metrics":
            out = dict(_metrics)
            out["uptime_s"] = int(time.time() - _metrics["uptime_start"]) if _metrics["uptime_start"] else 0
            self._json(200, out)
        else:
            self._json(404, {"ok": False})

def _start_hc(port: int):
    def _run():
        try:
            srv = HTTPServer(("0.0.0.0", port), _HC)
            log.info("Healthcheck em 0.0.0.0:%d (/healthz, /metrics)", port)
            srv.serve_forever()
        except Exception:
            log.exception("Healthcheck falhou")
    t = threading.Thread(target=_run, daemon=True)
    t.start()

# ---------------------------------------------
# SENDER (destinos)
# ---------------------------------------------
TG_MAX = 4096

def _chunk(text: str, size: int = TG_MAX) -> List[str]:
    if len(text) <= size:
        return [text]
    parts = []
    cur = 0
    while cur < len(text):
        parts.append(text[cur:cur+size])
        cur += size
    return parts

async def send_to_all(client: TelegramClient, text: str):
    # Se tiver BOT_TOKEN, tenta via Bot API; se falhar ou não tiver, usa o client
    if BOT_TOKEN:
        all_ok = True
        for d in USER_DESTINATIONS:
            for part in _chunk(text):
                ok, msg = _tg_send_via_bot(d, part)
                if ok:
                    log.info("· envio(bot)=ok → %s", d)
                else:
                    all_ok = False
                    log.error("· envio(bot)=ERRO → %s | %s", d, msg)
        if all_ok:
            return

    # fallback / modo sem token
    for d in USER_DESTINATIONS:
        try:
            for part in _chunk(text):
                await client.send_message(d, part, link_preview=False)
            log.info("· envio(client)=ok → %s", d)
        except Exception as e:
            log.error("· envio(client)=ERRO → %s | %s", d, repr(e))

def build_link(event) -> str:
    try:
        chan = getattr(event.chat, "username", None)
        if chan:
            return f"https://t.me/{chan}/{event.id}"
    except:
        pass
    return ""

# ---------------------------------------------
# MAIN
# ---------------------------------------------
def main():
    if HEALTHCHECK_PORT:
        _start_hc(HEALTHCHECK_PORT)

    _metrics["starts"] += 1
    _metrics["uptime_start"] = time.time()

    log.info("Conectando ao Telegram…")
    with TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH) as client:
        log.info("Conectado.")

        # --- Resolver canais monitorados
        resolved_entities = []

        # 1) por username (resolve mesmo fora dos diálogos)
        for u in MONITORED_USERNAMES:
            try:
                ent = client.get_entity(u)
                resolved_entities.append(ent)
            except Exception as e:
                log.error("Falha ao resolver %s: %s", u, repr(e))

        # 2) por ID numérico
        for cid in MONITORED_IDS:
            try:
                ent = client.get_entity(cid)
                resolved_entities.append(ent)
            except Exception as e:
                log.error("Falha ao resolver id %s: %s", cid, repr(e))

        # Log do que foi resolvido
        labels = []
        for ent in resolved_entities:
            username = getattr(ent, "username", None)
            label = f"@{username}" if username else str(getattr(ent, "id", "unknown"))
            labels.append(label)
        if labels:
            log.info("✅ Monitorando %d canais: %s", len(labels), ", ".join(labels))
        else:
            log.warning("⚠️ Nenhum canal resolvido. O handler ainda roda (chats=None), mas só filtra por conteúdo.")

        # Startup alert
        try:
            client.loop.run_until_complete(
                send_to_all(client, f"✅ {APP_NAME} v{VERSION} iniciado e monitorando.\nCanais: {', '.join(labels) or 'nenhum resolvido'}")
            )
        except Exception:
            log.exception("Falha no alerta de startup (ignorado)")

        # --- Handler
        @client.on(events.NewMessage(chats=resolved_entities or None))
        async def handler(event):
            try:
                if seen.is_dup(event.id):
                    return
                text = (event.raw_text or "").strip()
                if not text:
                    return

                _metrics["messages"] += 1

                ok, key, title, price, reason = classify_and_match(text)
                chan = getattr(event.chat, "username", None)
                chan_disp = f"@{chan}" if chan else str(getattr(event.chat, "id", "desconhecido"))
                link = build_link(event)
                price_s = f"{price:.2f}" if isinstance(price, (float, int)) else "None"

                if ok:
                    _metrics["matches"] += 1
                    header = "Corre!🔥 " if needs_header(key, price) else ""
                    base_msg = f"{header}{text}\n\n— via {chan_disp}"
                    if link:
                        base_msg += f"\n{link}"
                    log.info("[%-18s] MATCH → %s | price=%s | key=%s | reason=%s",
                             chan_disp, title, price_s, key, reason)
                    await send_to_all(client, base_msg)
                else:
                    log.info("[%-18s] IGNORADO → %s | price=%s | key=%s | reason=%s",
                             chan_disp, title, price_s, key, reason)

            except Exception as e:
                _metrics["errors"] += 1
                tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                if len(tb) > 1800:
                    tb = tb[-1800:]
                log.exception("Erro no handler")
                with contextlib.suppress(Exception):
                    await send_to_all(client, f"⚠️ {APP_NAME} erro no handler:\n\n{tb}")

        # run
        client.run_until_disconnected()

# pequena dependência local para suppress sem importar muito no topo
import contextlib

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        if len(tb) > 1800:
            tb = tb[-1800:]
        log.exception("Falha fatal no main()")
        # tentativa de alerta mesmo sem client
        if BOT_TOKEN and USER_DESTINATIONS:
            try:
                for d in USER_DESTINATIONS:
                    _tg_send_via_bot(d, f"💥 {APP_NAME} caiu no startup:\n\n{tb}")
            except Exception:
                pass
        raise
