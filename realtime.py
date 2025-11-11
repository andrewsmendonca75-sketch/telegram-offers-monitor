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
log = logging.getLogger("monitor")

# ---------------------------------------------
# ENV
# ---------------------------------------------
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
STRING_SESSION = os.environ["TELEGRAM_STRING_SESSION"]
BOT_TOKEN = os.environ["TELEGRAM_TOKEN"]

MONITORED_CHANNELS_RAW = os.getenv("MONITORED_CHANNELS", "")
USER_DESTINATIONS_RAW = os.getenv("USER_DESTINATIONS", os.getenv("USER_CHAT_ID", ""))

def _split_csv(val: str) -> List[str]:
    return [p.strip() for p in val.split(",") if p and p.strip()]

def _norm_username(u: str) -> Optional[str]:
    if not u: return None
    u = u.strip()
    if not u: return None
    if re.fullmatch(r"\d+", u):
        return None
    u = u.lower()
    if not u.startswith("@"):
        u = "@"+u
    return u

MONITORED_USERNAMES: List[str] = []
for x in _split_csv(MONITORED_CHANNELS_RAW):
    nu = _norm_username(x)
    if nu: MONITORED_USERNAMES.append(nu)

if not MONITORED_USERNAMES:
    log.warning("MONITORED_CHANNELS vazio — nada será filtrado.")
else:
    log.info("▶️ Canais: " + ", ".join(MONITORED_USERNAMES))

USER_DESTINATIONS: List[str] = _split_csv(USER_DESTINATIONS_RAW)
if not USER_DESTINATIONS:
    log.warning("USER_DESTINATIONS/USER_CHAT_ID não definido; nada será enviado.")
else:
    log.info("📬 Destinos: " + ", ".join(USER_DESTINATIONS))

BOT_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

def bot_send_text(dest: str, text: str) -> Tuple[bool, str]:
    payload = {"chat_id": dest, "text": text, "disable_web_page_preview": True}
    try:
        r = requests.post(f"{BOT_BASE}/sendMessage", json=payload, timeout=20)
        if r.status_code == 200 and r.json().get("ok"):
            return True, "ok"
        return False, r.text
    except Exception as e:
        return False, repr(e)

def notify_all(text: str):
    for d in USER_DESTINATIONS:
        ok, msg = bot_send_text(d, text)
        if ok:
            log.info("· envio=ok → %s", d)
        else:
            log.error("· envio=ERRO → %s", msg)

# ---------------------------------------------
# PRICE PARSER (robusto BR)
# ---------------------------------------------
# Captura: (R$ opcional) + número em formato BR:
#   - 3+ dígitos (>= 100) com ou sem ,centavos (ex: 179 | 1.799 | 3.560,00)
#   - OU qualquer número com vírgula de centavos (ex: 89,90)
# Evita números pequenos sem R$ (ex.: "16GB", "12x").
PRICE_RE = re.compile(
    r"(?i)(r\$\s*)?("
    r"(?:\d{1,3}(?:\.\d{3})+(?:,\d{2})?)"     # 1.299,90 | 12.345
    r"|(?:\d{3,}(?:,\d{2})?)"                 # 179 | 179,90 | 3560 | 3560,00
    r"|(?:\d+,\d{2})"                         # 89,90
    r")"
)

def _to_float_brl(raw: str) -> Optional[float]:
    s = raw.strip()
    s = s.replace(".", "").replace(",", ".")
    try:
        v = float(s)
        return v if 0 < v < 100000 else None
    except:
        return None

def find_lowest_price(text: str) -> Optional[float]:
    vals: List[float] = []
    for m in PRICE_RE.finditer(text):
        has_currency = bool(m.group(1))
        raw_num = m.group(2)

        # Se usar ponto como decimal (89.90), aceite somente se tiver R$
        if "." in raw_num and "," not in raw_num:
            parts = raw_num.split(".")
            if len(parts) == 2 and len(parts[1]) == 2 and not has_currency:
                # parece decimal estilo US sem R$ → ignorar (provável ruído)
                continue

        v = _to_float_brl(raw_num)
        if v is None:
            continue

        # Evitar números < 100 sem R$
        if v < 100 and not has_currency:
            continue

        # Evitar valores muito baixos (resquício) < 5
        if v < 5:
            continue

        vals.append(v)

    return min(vals) if vals else None

# ---------------------------------------------
# REGEX DE PRODUTOS
# ---------------------------------------------
# Bloqueios gerais
BLOCK_CATS = re.compile(r"\b(celular|smartphone|iphone|android|notebook|laptop|macbook)\b", re.I)
PC_GAMER_RE = re.compile(r"\b(pc\s*gamer|computador\s*gamer|setup\s*completo|kit\s*completo)\b", re.I)

# GPUs (cuidado com TI)
RTX5050_RE   = re.compile(r"\brtx\s*5050\b", re.I)
RTX5060_RE   = re.compile(r"\brtx\s*5060(?!\s*ti)\b", re.I)   # não casa 5060 Ti
RTX5060TI_RE = re.compile(r"\brtx\s*5060\s*ti\b", re.I)
RTX5070_FAM  = re.compile(r"\brtx\s*5070(\s*ti)?\b", re.I)    # 5070 e 5070 Ti
RX7600_RE    = re.compile(r"\brx\s*7600\b", re.I)

# CPUs superiores
INTEL_SUP = re.compile(r"\b(i(?:5|7|9)[-\s]*(?:12|13|14)\d{2,3}k?f?)\b", re.I)
AMD_SUP   = re.compile(r"\b(ryzen\s*(?:7\s*5700x?|7\s*5800x3?d?|9\s*5900x|9\s*5950x))\b", re.I)
AMD_BLOCK = re.compile(r"\b(ryzen\s*(?:3|5)|5600g?t?)\b", re.I)

# MOBOS
A520_RE     = re.compile(r"\ba520m?\b", re.I)
B550_FAM_RE = re.compile(r"\bb550m?\b|\bx570\b", re.I)
LGA1700_RE  = re.compile(r"\b(h610|b660|b760|z690|z790)\b", re.I)

# GABINETE / COOLER
GAB_RE     = re.compile(r"\bgabinete\b", re.I)
FANS_HINT  = re.compile(r"(?:(\d+)\s*(?:fans?|coolers?|ventoinhas?)|(\d+)\s*x\s*120\s*mm|(\d+)\s*x\s*fan)", re.I)
WATER_RE   = re.compile(r"\bwater\s*cooler\b", re.I)
AIR_COOLER = re.compile(r"\bcooler\b", re.I)

# SSD / RAM
SSD_RE  = re.compile(r"\bssd\b", re.I)
M2_RE   = re.compile(r"\bm\.?2\b|\bnvme\b", re.I)
TB1_RE  = re.compile(r"\b1\s*tb\b", re.I)
RAM_RE  = re.compile(r"\bddr4\b", re.I)
GB16_RE = re.compile(r"\b16\s*gb\b", re.I)
GB8_RE  = re.compile(r"\b8\s*gb\b", re.I)

# ---------------------------------------------
# AUX
# ---------------------------------------------
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

# ---------------------------------------------
# REGRAS
# ---------------------------------------------
def classify_and_match(text: str):
    t = text

    # Bloqueios gerais
    if BLOCK_CATS.search(t):
        return False, "block:cat", "Categoria bloqueada", None, "celular/notebook etc."
    if PC_GAMER_RE.search(t):
        return False, "block:pcgamer", "PC Gamer bloqueado", None, "PC Gamer/kit completo bloqueado"

    price = find_lowest_price(t)

    # GPUs
    if RTX5050_RE.search(t):
        if price and price < 1700:
            return True, "gpu:rtx5050", "RTX 5050", price, "< 1700"
        return False, "gpu:rtx5050", "RTX 5050", price, ">= 1700 ou sem preço"

    if RTX5060_RE.search(t):
        if price and price < 1900:
            return True, "gpu:rtx5060", "RTX 5060", price, "< 1900"
        return False, "gpu:rtx5060", "RTX 5060", price, ">= 1900 ou sem preço"

    # 5060 Ti: não alertamos (apenas ignora)
    if RTX5060TI_RE.search(t):
        return False, "gpu:rtx5060ti", "RTX 5060 Ti", price, "variante TI ignorada"

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

    # COOLERS (ar e water) <= 150
    if WATER_RE.search(t) and price and price <= 150:
        return True, "cooler:water", "Water Cooler", price, "<= 150"
    if AIR_COOLER.search(t) and not WATER_RE.search(t) and price and price <= 150:
        return True, "cooler:air", "Cooler (ar)", price, "<= 150"

    # SSD M.2 1TB <= 460
    if SSD_RE.search(t) and M2_RE.search(t) and TB1_RE.search(t):
        if price and price <= 460:
            return True, "ssd:m2:1tb", "SSD M.2 1TB", price, "<= 460"
        return False, "ssd:m2:1tb", "SSD M.2 1TB", price, "> 460 ou sem preço"

    # RAM DDR4
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
    def __init__(self, maxlen=800):
        self.maxlen = maxlen
        self.data: Dict[int, float] = {}

    def is_dup(self, msg_id):
        if msg_id in self.data:
            return True
        if len(self.data) > self.maxlen:
            for k in list(self.data)[:self.maxlen // 2]:
                del self.data[k]
        self.data[msg_id] = time.time()
        return False

seen = Seen()

# ---------------------------------------------
# MAIN
# ---------------------------------------------
def main():
    log.info("Conectando ao Telegram...")
    with TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH) as client:
        log.info("Conectado.")
        dialogs = client.get_dialogs()
        uname2ent = {f"@{d.entity.username.lower()}": d.entity for d in dialogs if getattr(d.entity, "username", None)}
        resolved = [uname2ent[u] for u in MONITORED_USERNAMES if u in uname2ent]
        log.info("✅ Monitorando %d canais…", len(resolved))

        @client.on(events.NewMessage(chats=resolved or None))
        async def handler(event):
            if seen.is_dup(event.id): return
            text = (event.raw_text or "").strip()
            if not text: return

            ok, key, title, price, reason = classify_and_match(text)
            chan = getattr(event.chat, "username", "(desconhecido)")
            chan_disp = f"@{chan}"

            if ok:
                header = "Corre!🔥 " if needs_header(key, price) else ""
                msg = f"{header}{text}\n\n— via {chan_disp}"
                log.info("[%-18s] MATCH → %s | price=%s | key=%s | reason=%s",
                         chan_disp, title, f"{price:.2f}" if price is not None else "None", key, reason)
                notify_all(msg)
            else:
                log.info("[%-18s] IGNORADO → %s | price=%s | key=%s | reason=%s",
                         chan_disp, title, f"{price:.2f}" if price is not None else "None", key, reason)

        client.run_until_disconnected()

if __name__ == "__main__":
    main()
