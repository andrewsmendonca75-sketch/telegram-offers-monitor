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
        return None  # id numérico, não username
    u = u.lower()
    if not u.startswith("@"):
        u = "@"+u
    return u

MONITORED_USERNAMES: List[str] = []
for x in _split_csv(MONITORED_CHANNELS_RAW):
    nu = _norm_username(x)
    if nu: MONITORED_USERNAMES.append(nu)

if not MONITORED_USERNAMES:
    log.warning("MONITORED_CHANNELS vazio — nada será filtrado (handler ouvirá tudo, mas filtrará por 0 canais).")
    log.info("▶️ Canais: (nenhum)")
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
            log.info("· envio=ok → destinos=%s", d)
        else:
            log.error("· envio=ERRO → %s", msg)

# ---------------------------------------------
# PRICE PARSER (BR robusto)
# ---------------------------------------------
PRICE_RE = re.compile(
    r"""
    (?:
        (?:r\$\s*)?
        (?:
            \d{1,3}(?:\.\d{3})+(?:,\d{2})?  # 1.234,56
          | \d+(?:,\d{2})?                  # 123 ou 123,45
          | \d+\.\d{2}                      # 123.45
        )
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

def _to_float_brl(s: str) -> Optional[float]:
    s = s.strip()
    s = re.sub(r"\s+", "", s)
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        m = re.fullmatch(r"(\d+)\.(\d{3})", s)
        if m:
            s = m.group(1) + m.group(2)
    try:
        v = float(s)
        return v if 0 < v < 100000 else None
    except:
        return None

def find_lowest_price(text: str) -> Optional[float]:
    vals = []
    for m in PRICE_RE.finditer(text):
        raw = re.sub(r"(?i)^r\$\s*", "", m.group(0))
        v = _to_float_brl(raw)
        if v is not None:
            vals.append(v)
    if not vals: return None
    cleaned = [x for x in vals if x >= 5.0]
    if not cleaned: cleaned = vals
    return min(cleaned)

# ---------------------------------------------
# REGEX / REGRAS
# ---------------------------------------------
# GPUs
RTX5050_RE = re.compile(r"\brtx\s*5050\b", re.IGNORECASE)   # NOVO
RTX5060_RE = re.compile(r"\brtx\s*5060\b", re.IGNORECASE)
RTX5070_RE = re.compile(r"\brtx\s*5070\b", re.IGNORECASE)
RX7600_RE  = re.compile(r"\brx\s*7600\b", re.IGNORECASE)

# CPUs Intel
INTEL_ANY_SUP = re.compile(
    r"""\b(?:
        i5[-\s]*12(?:600|700)k?f? |
        i5[-\s]*13(?:400|500|600)k?f? |
        i5[-\s]*14(?:400|500|600)k?f? |
        i7[-\s]*12(?:700|900)k?f? |
        i7[-\s]*13(?:700|900)k?f? |
        i7[-\s]*14(?:700|900)k?f? |
        i9[-\s]*\d{4,5}k?f?
    )\b""",
    re.IGNORECASE | re.VERBOSE
)
INTEL_14400F = re.compile(r"\bi5[-\s]*14400k?f?\b", re.IGNORECASE)
INTEL_12600F_KF = re.compile(r"\bi5[-\s]*12600k?f?\b", re.IGNORECASE)
INTEL_12400F = re.compile(r"\bi5[-\s]*12400f\b", re.IGNORECASE)

# CPUs AMD (AM4 sup.)
AMD_SUP = re.compile(
    r"""\b(?:
        ryzen\s*7\s*5700x? |
        ryzen\s*7\s*5800x3?d? |
        ryzen\s*9\s*5900x |
        ryzen\s*9\s*5950x
    )\b""",
    re.IGNORECASE | re.VERBOSE
)

# MOBOS
A520_RE = re.compile(r"\ba520m?\b", re.IGNORECASE)
B550_RE = re.compile(r"\bb550m?\b", re.IGNORECASE)
AM4_TOP = re.compile(r"\b(?:tuf|elite|aorus|tomahawk|steel\s*legend|strix|prime)\b", re.IGNORECASE)
LGA1700 = re.compile(r"\b(?:h610|b660|b760|z690|z790)\b", re.IGNORECASE)

# GABINETE
GAB_RE = re.compile(r"\bgabinete\b", re.IGNORECASE)
FANS_HINT = re.compile(
    r"""(?:
        (?:(\d+)\s*(?:fans?|coolers?|ventoinhas?))|
        (?:(\d+)\s*x\s*120\s*mm)|
        (?:(\d+)\s*x\s*fan)
    )""",
    re.IGNORECASE | re.VERBOSE
)

# COOLERS
WATER_RE = re.compile(r"\bwater\s*cooler\b", re.IGNORECASE)
AIR_COOLER_RE = re.compile(r"\bcooler\b", re.IGNORECASE)  # genérico (NÃO water)

# SSD NVMe M.2 1TB
SSD_RE = re.compile(r"\bssd\b", re.IGNORECASE)
M2_RE  = re.compile(r"\bm\.?2\b|\bm2\b|\bnvme\b", re.IGNORECASE)
TB1_RE = re.compile(r"\b1\s*tb\b|\b1tb\b", re.IGNORECASE)

# RAM DDR4
RAM_RE   = re.compile(r"\bmem[oó]ria|\bram\b", re.IGNORECASE)
DDR4_RE  = re.compile(r"\bddr\s*4\b|\bddr4\b", re.IGNORECASE)
GB16_RE  = re.compile(r"\b16\s*gb\b|\b16gb\b", re.IGNORECASE)
GB8_RE   = re.compile(r"\b8\s*gb\b|\b8gb\b", re.IGNORECASE)

# PS5 (exemplo)
PS5_RE = re.compile(r"\bps5\b|\bplaystation\s*5\b", re.IGNORECASE)

# ---------------------------------------------
# UTIL
# ---------------------------------------------
def count_fans(text: str) -> int:
    n = 0
    for m in FANS_HINT.finditer(text):
        for g in m.groups():
            if g and g.isdigit():
                n = max(n, int(g))
    return n

def add_header_if_needed(product_key: str, price: Optional[float]) -> bool:
    if price is None:
        return False
    # GPUs com header especial
    if product_key == "gpu:rtx5060" and price < 1900:
        return True
    if product_key == "gpu:rtx5070" and price < 3700:
        return True
    # CPUs com header especial
    if product_key.startswith("cpu:intel") and price < 899:
        return True
    if product_key.startswith("cpu:amd") and price < 899:
        return True
    # MOBOS top com header especial
    if product_key in ("mobo:am4:top", "mobo:lga1700:top") and price < 550:
        return True
    return False

# ---------------------------------------------
# MATCH
# ---------------------------------------------
def classify_and_match(text: str) -> Tuple[bool, str, str, Optional[float], str, str]:
    """
    Retorna:
      ok, product_key, title, price, reason, display_title
    """
    t = text
    price = find_lowest_price(t)

    # GPUs
    if RTX5050_RE.search(t):
        # ALERTA quando <= 1600 (sem cabeçalho especial)
        if price is not None and price <= 1600:
            return True, "gpu:rtx5050", "RTX 5050", price, f"RTX 5050 ≤ 1600 (R$ {price:.2f})", "RTX 5050"
        else:
            return False, "gpu:rtx5050", "RTX 5050", price, "RTX 5050 > 1600 ou sem preço", "RTX 5050"

    if RTX5060_RE.search(t):
        return True, "gpu:rtx5060", "RTX 5060", price, "GPU RTX 5060 detectada", "RTX 5060"

    if RTX5070_RE.search(t):
        return True, "gpu:rtx5070", "RTX 5070", price, "GPU RTX 5070 detectada", "RTX 5070"

    if RX7600_RE.search(t):
        return True, "gpu:rx7600", "RX 7600", price, "GPU RX 7600 detectada", "RX 7600"

    # CPUs Intel (12400F/12600F-KF/14400F/superiores)
    if INTEL_14400F.search(t) or INTEL_12600F_KF.search(t) or INTEL_12400F.search(t) or INTEL_ANY_SUP.search(t):
        if price is not None and price <= 900:
            return True, "cpu:intel", "CPU Intel (i5/i7/i9)", price, f"CPU Intel ≤ 900 (R$ {price:.2f})", "CPU Intel"
        else:
            return False, "cpu:intel", "CPU Intel (i5/i7/i9)", price, "CPU Intel com preço > 900 ou ausente", "CPU Intel"

    # CPUs AMD (AM4 sup.)
    if AMD_SUP.search(t):
        if price is not None and price <= 900:
            return True, "cpu:amd", "CPU AMD (AM4 sup.)", price, f"CPU AMD ≤ 900 (R$ {price:.2f})", "CPU AMD"
        else:
            return False, "cpu:amd", "CPU AMD (AM4 sup.)", price, "CPU AMD com preço > 900 ou ausente", "CPU AMD"

    # MOBOS — bloquear A520
    if A520_RE.search(t):
        return False, "mobo:am4", "Placa-mãe A520", price, "A520 bloqueada", "A520"

    # B550 < 550
    if B550_RE.search(t):
        if price is not None and price < 550:
            return True, "mobo:am4", "Placa-mãe B550", price, f"B550 < 550 (R$ {price:.2f})", "B550"
        else:
            return False, "mobo:am4", "Placa-mãe B550", price, "B550 ≥ 550 ou sem preço", "B550"

    # MOBOS top (AM4 e LGA1700) < 550
    if (AM4_TOP.search(t) and B550_RE.search(t)):
        if price is not None and price < 550:
            return True, "mobo:am4:top", "Placa-mãe AM4 TOP", price, f"Top AM4 < 550 (R$ {price:.2f})", "AM4 TOP"
        else:
            return False, "mobo:am4:top", "Placa-mãe AM4 TOP", price, "Top AM4 ≥ 550 ou sem preço", "AM4 TOP"

    if LGA1700.search(t):
        if AM4_TOP.search(t):
            if price is not None and price < 550:
                return True, "mobo:lga1700:top", "Placa-mãe LGA1700 TOP", price, f"Top LGA1700 < 550 (R$ {price:.2f})", "LGA1700 TOP"
            else:
                return False, "mobo:lga1700:top", "Placa-mãe LGA1700 TOP", price, "Top LGA1700 ≥ 550 ou sem preço", "LGA1700 TOP"
        return False, "mobo:lga1700", "Placa-mãe LGA1700", price, "LGA1700 comum (apenas top alerta)", "LGA1700"

    # GABINETE — 3 fans ≤ 160; 4+ fans ≤ 220
    if GAB_RE.search(t):
        fans = count_fans(t)
        if price is None:
            return False, "case", "Gabinete", price, "Gabinete sem preço", "Gabinete"
        if (fans == 3 and price <= 160) or (fans >= 4 and price <= 220):
            return True, "case", f"Gabinete ({fans} fans)", price, f"Gabinete {fans} fans OK (R$ {price:.2f})", "Gabinete"
        else:
            return False, "case", f"Gabinete ({fans or 's/ info'} fans)", price, "Gabinete fora das regras", "Gabinete"

    # WATER COOLER ≤ 200
    if WATER_RE.search(t):
        if price is not None and price <= 200:
            return True, "cooler:water", "Water cooler", price, f"Water cooler ≤ 200 (R$ {price:.2f})", "Water cooler"
        else:
            return False, "cooler:water", "Water cooler", price, "Water cooler > 200 ou sem preço", "Water cooler"

    # COOLER (ar) ≤ 130 — só se mencionar "cooler" e NÃO mencionar "water cooler"
    if AIR_COOLER_RE.search(t) and not WATER_RE.search(t):
        if price is not None and price <= 130:
            return True, "cooler:air", "Cooler (ar)", price, f"Cooler (ar) ≤ 130 (R$ {price:.2f})", "Cooler (ar)"
        else:
            return False, "cooler:air", "Cooler (ar)", price, "Cooler (ar) > 130 ou sem preço", "Cooler (ar)"

    # SSD NVMe M.2 1TB ≤ 460
    if SSD_RE.search(t) and M2_RE.search(t) and TB1_RE.search(t):
        if price is not None and price <= 460:
            return True, "ssd:m2:1tb", "SSD NVMe M.2 1TB", price, f"SSD M.2 1TB ≤ 460 (R$ {price:.2f})", "SSD M.2 1TB"
        else:
            return False, "ssd:m2:1tb", "SSD NVMe M.2 1TB", price, "SSD 1TB > 460 ou sem preço", "SSD M.2 1TB"

    # RAM DDR4 (16GB ≤ 300, 8GB ≤ 150)
    if (RAM_RE.search(t) or DDR4_RE.search(t)) and DDR4_RE.search(t):
        if GB16_RE.search(t):
            if price is not None and price <= 300:
                return True, "ram:ddr4:16", "Memória DDR4 16GB", price, f"DDR4 16GB ≤ 300 (R$ {price:.2f})", "DDR4 16GB"
            else:
                return False, "ram:ddr4:16", "Memória DDR4 16GB", price, "DDR4 16GB > 300 ou sem preço", "DDR4 16GB"
        if GB8_RE.search(t):
            if price is not None and price <= 150:
                return True, "ram:ddr4:8", "Memória DDR4 8GB", price, f"DDR4 8GB ≤ 150 (R$ {price:.2f})", "DDR4 8GB"
            else:
                return False, "ram:ddr4:8", "Memória DDR4 8GB", price, "DDR4 8GB > 150 ou sem preço", "DDR4 8GB"

    # PS5 (opcional)
    if PS5_RE.search(t):
        return True, "console:ps5", "PlayStation 5", price, "PS5 detectado", "PS5"

    return False, "none", "Desconhecido", price, "sem match", "sem match"

# ---------------------------------------------
# Anti-duplicado simples
# ---------------------------------------------
class Seen:
    def __init__(self, maxlen=500):
        self.maxlen = maxlen
        self._d: Dict[int, float] = {}

    def is_dup(self, msg_id: int) -> bool:
        if msg_id in self._d:
            return True
        if len(self._d) > self.maxlen:
            items = sorted(self._d.items(), key=lambda kv: kv[1])[: self.maxlen // 2]
            for k, _ in items:
                self._d.pop(k, None)
        self._d[msg_id] = time.time()
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
        uname2entity = {}
        for d in dialogs:
            try:
                if d.entity and getattr(d.entity, "username", None):
                    uname2entity["@{}".format(d.entity.username.lower())] = d.entity
            except Exception:
                pass

        resolved = []
        for u in MONITORED_USERNAMES:
            ent = uname2entity.get(u)
            if ent is None:
                log.warning("Canal não encontrado (ignorado): %s", u)
            else:
                resolved.append(ent)

        if resolved:
            log.info("▶️ Canais resolvidos: " + ", ".join(f"@{getattr(e,'username','')}" for e in resolved if getattr(e,'username',None)))
        else:
            log.info("▶️ Canais resolvidos: ")
        log.info("✅ Logado — monitorando %d canais…", len(resolved))
        log.info("▶️ Rodando. Pressione Ctrl+C para sair.")

        @client.on(events.NewMessage(chats=resolved if resolved else None))
        async def handler(event):
            try:
                if seen.is_dup(event.id):
                    return
                text = (event.raw_text or "").strip()
                if not text:
                    return

                ok, pkey, title, price, reason, short_name = classify_and_match(text)

                chan = getattr(event.chat, "username", None)
                chan_disp = f"@{chan}" if chan else "(desconhecido)"

                if ok:
                    header = "Corre!🔥 " if add_header_if_needed(pkey, price) else ""
                    to_send = f"{header}{text}\n\n— via {chan_disp}"
                    log.info("[%-18s] MATCH → %s | price=%s | key=%s | reason=%s",
                             chan_disp, short_name, f"{price:.2f}" if price is not None else "None", pkey, reason)
                    notify_all(to_send)
                else:
                    log.info("[%-18s] IGNORADO → %s | price=%s | key=%s | reason=%s",
                             chan_disp, short_name, f"{price:.2f}" if price is not None else "None", pkey, reason)

            except Exception as e:
                log.exception("Handler error: %s", e)

        client.run_until_disconnected()

if __name__ == "__main__":
    main()
