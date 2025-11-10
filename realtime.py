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
log = logging.getLogger("realtime")

# ---------------------------------------------
# ENV
# ---------------------------------------------
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
STRING_SESSION = os.environ["TELEGRAM_STRING_SESSION"]
BOT_TOKEN = os.environ["TELEGRAM_TOKEN"]

MONITORED_CHANNELS_RAW = os.getenv("MONITORED_CHANNELS", "")
USER_DESTINATIONS_RAW = os.getenv("USER_DESTINATIONS", os.getenv("USER_CHAT_ID", ""))

BOT_RETRY = int(os.getenv("BOT_RETRY", "2"))

def _split_list(val: str) -> List[str]:
    return [p.strip() for p in val.split(",") if p.strip()] if val else []

def _norm_username(u: str) -> Optional[str]:
    if not u:
        return None
    u = u.strip()
    # se numérico, não é username
    if re.fullmatch(r"\d+", u):
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

# garantir inclusão de @economister se não estiver
if "@economister" not in MONITORED_USERNAMES:
    MONITORED_USERNAMES.append("@economister")

if not MONITORED_USERNAMES:
    log.warning("MONITORED_CHANNELS vazio — nada será filtrado (handler ouvirá tudo, mas filtrará por 0 canais).")
    log.info("▶️ Canais: (nenhum)")
else:
    log.info("▶️ Canais: " + ", ".join(MONITORED_USERNAMES))

USER_DESTINATIONS: List[str] = _split_list(USER_DESTINATIONS_RAW)
if not USER_DESTINATIONS:
    log.warning("USER_DESTINATIONS/USER_CHAT_ID não definido; nada será enviado.")
else:
    log.info(f"📬 Destinos: {', '.join(USER_DESTINATIONS)}")

# ---------------------------------------------
# BOT API (sem parse_mode)
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
            log.info("· envio=ok → destinos=%s", dest)
        else:
            log.error("· ERRO envio via bot: %s", msg)
            for _ in range(BOT_RETRY):
                time.sleep(0.5)
                ok2, _ = bot_send_text(dest, text)
                if ok2:
                    log.info("· envio=ok (retry) → destinos=%s", dest)
                    break

# ---------------------------------------------
# PRICE PARSER (BR)
# ---------------------------------------------
PRICE_REGEX = re.compile(
    r"""(?i)
    (?:R\$\s*)?(
        \d{1,3}(?:\.\d{3})+(?:,\d{2})?   # 1.234,56 / 12.345
        |\d+(?:,\d{2})?                  # 199,90 / 199
        |\d+\.\d{2}                      # 199.90
    )
    """,
    re.VERBOSE,
)

def _normalize_number(s: str) -> Optional[float]:
    s = s.strip()
    s = re.sub(r"\s+", "", s)
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        # 1.700 → 1700 (milhar)
        parts = s.split(".")
        if len(parts) > 1 and all(p.isdigit() for p in parts):
            # se último bloco tem 3 dígitos e há pelo menos um à esquerda, junta
            if len(parts[-1]) == 3:
                s = "".join(parts)
    try:
        v = float(s)
        if 0 < v < 100000:
            return v
    except:
        return None
    return None

def extract_prices(text: str) -> List[float]:
    vals: List[float] = []
    for m in PRICE_REGEX.finditer(text):
        raw = m.group(1)
        v = _normalize_number(raw)
        if v is not None:
            vals.append(v)
    # descartar valores muito baixos (provável %/erro)
    vals = [v for v in vals if v >= 5]
    return vals

def lowest_price(text: str) -> Optional[float]:
    vals = extract_prices(text)
    return min(vals) if vals else None

# ---------------------------------------------
# REGEX DE PRODUTOS / CATEGORIAS
# ---------------------------------------------
# GPUs
RTX5060_RE = re.compile(r"\brtx\s*5060\b", re.IGNORECASE)
RX7600_RE = re.compile(r"\brx\s*7600\b", re.IGNORECASE)

# CPUs Intel
INTEL_ANY_SUP = re.compile(
    r"""\b(?:
        i5[-\s]*1[2-4]\d{3}k?f? |
        i7[-\s]*1[2-4]\d{3}k?f? |
        i9[-\s]*\d{4,5}k?f?
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)
INTEL_14400F = re.compile(r"\bi5[-\s]*14400k?f?\b", re.IGNORECASE)
INTEL_12600F_KF = re.compile(r"\bi5[-\s]*12600k?f?\b", re.IGNORECASE)

# CPUs AMD
RYZEN_5700X_PLUS = re.compile(
    r"""\b(?:
        ryzen\s*7\s*5700x |
        ryzen\s*7\s*5800x3?d? |
        ryzen\s*9\s*5900x |
        ryzen\s*9\s*5950x
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)
RYZEN_5700_NONX = re.compile(r"\bryzen\s*7\s*5700\b", re.IGNORECASE)

# MOBOS
B550_RE = re.compile(r"\bb550m?\b", re.IGNORECASE)
X570_RE = re.compile(r"\bx570\b", re.IGNORECASE)
INTEL_LGA1700_MB = re.compile(r"\b(h610m?|b660m?|b760m?|z690|z790)\b", re.IGNORECASE)
TOP_MB_BRANDS = re.compile(r"\b(tuf|elite|aorus|strix|tomahawk|steel\s*legend)\b", re.IGNORECASE)
A520_RE = re.compile(r"\ba520m?\b", re.IGNORECASE)  # bloqueio

# RAM DDR4
DDR4_RE = re.compile(r"\bddr\s*4\b|\bddr4\b", re.IGNORECASE)
RAM_16GB_RE = re.compile(r"\b(16\s*gb|2x8\s*gb|1x16\s*gb)\b", re.IGNORECASE)
RAM_8GB_RE = re.compile(r"\b(8\s*gb|1x8\s*gb)\b", re.IGNORECASE)

# SSD 1TB NVMe/M.2
SSD_RE = re.compile(r"\bssd\b", re.IGNORECASE)
ONE_TB_RE = re.compile(r"\b1\s*tb\b|\b1tb\b", re.IGNORECASE)
NVME_M2_RE = re.compile(r"\b(nvme|nv2|nv3|m\.?2|m2|pcie)\b", re.IGNORECASE)

# Gabinete e fans
GABINETE_RE = re.compile(r"\bgabinete\b", re.IGNORECASE)
FAN_COUNT_RE = re.compile(
    r"""(?:
        (?:(\d+)\s*(?:fans?|coolers?|ventoinhas?))|
        (?:(\d+)\s*x\s*120\s*mm)|
        (?:(\d+)\s*x\s*fan)
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Water cooler
WATER_COOLER_RE = re.compile(r"\bwater\s*cooler\b|\bwatercooler\b|\bwc\s*(?:120|240)\b", re.IGNORECASE)

# Teclado Redragon (superior ao Kumara)
REDRAGON_RE = re.compile(r"\bredragon\b", re.IGNORECASE)
KUMARA_RE = re.compile(r"\bkumara\b", re.IGNORECASE)

# PS5
PS5_RE = re.compile(r"\bps5\b|\bplaystation\s*5\b", re.IGNORECASE)

# ---------------------------------------------
# UTILS
# ---------------------------------------------
def count_fans(text: str) -> int:
    max_f = 0
    for m in FAN_COUNT_RE.finditer(text):
        for g in m.groups():
            if g and g.isdigit():
                max_f = max(max_f, int(g))
    return max_f

def footer_with_channel(text: str, channel_username: Optional[str]) -> str:
    ch = f"@{channel_username}" if channel_username else ""
    if ch:
        return f"{text}\n\n— via {ch}"
    return text

# ---------------------------------------------
# CLASSIFICADOR + REGRAS
# ---------------------------------------------
def classify_and_match(text: str) -> Tuple[bool, str, str, Optional[float], str, bool]:
    """
    Retorna:
      (is_match, categoria, titulo_curto, preco, motivo, usar_cabecalho_corre)
    """
    t = text
    price = lowest_price(t)

    # GPUs
    if RTX5060_RE.search(t) or RX7600_RE.search(t):
        categoria = "GPU"
        titulo = "RTX 5060" if RTX5060_RE.search(t) else "RX 7600"
        if price is None:
            return False, categoria, titulo, None, "GPU sem preço", False
        if RTX5060_RE.search(t):
            # MATCH e cabeçalho < 1900
            if price < 1900:
                return True, categoria, titulo, price, "RTX 5060 < 1900", True
            else:
                return True, categoria, titulo, price, "GPU (sem cabeçalho)", False
        else:
            # RX 7600: regra geral de GPU (sem cabeçalho)
            return True, categoria, titulo, price, "GPU", False

    # CPUs Intel (supers) incl. 12600F/KF
    if INTEL_ANY_SUP.search(t):
        categoria = "CPU Intel"
        titulo = INTEL_14400F.search(t) and "i5-14400F/KF" or (INTEL_12600F_KF.search(t) and "i5-12600F/KF" or "Intel série 12/13/14")
        if price is None:
            return False, categoria, titulo, None, "CPU Intel sem preço", False
        if price <= 899:
            # cabeçalho
            return True, categoria, titulo, price, "Intel ≤ 899", True
        else:
            return False, categoria, titulo, price, "CPU Intel > 899", False

    # CPUs AMD
    if RYZEN_5700X_PLUS.search(t) or RYZEN_5700_NONX.search(t):
        categoria = "CPU AMD"
        titulo = "Ryzen 7 5700X/+" if RYZEN_5700X_PLUS.search(t) else "Ryzen 7 5700"
        if price is None:
            return False, categoria, titulo, None, "CPU AMD sem preço", False
        if price <= 899:
            # cabeçalho só se 5700X ou superior
            use_hdr = bool(RYZEN_5700X_PLUS.search(t))
            motivo = "Ryzen ≤ 899" + (" (5700X+)" if use_hdr else "")
            return True, categoria, titulo, price, motivo, use_hdr
        else:
            return False, categoria, titulo, price, "CPU AMD > 899", False

    # MOBOS — bloquear A520 sempre
    if A520_RE.search(t):
        return False, "MOBO", "A520", price, "A520 bloqueada", False

    # B550 < 550 (estrito)
    if B550_RE.search(t):
        categoria = "MOBO AM4"
        titulo = "B550"
        if price is None:
            return False, categoria, titulo, None, "MOBO sem preço", False
        if price < 550:
            return True, categoria, titulo, price, "B550 < 550", False
        else:
            return False, categoria, titulo, price, "B550 ≥ 550", False

    # MOBOS TOP < 550 (Intel LGA1700 + AM4/X570) — cabeçalho
    if TOP_MB_BRANDS.search(t) and (INTEL_LGA1700_MB.search(t) or B550_RE.search(t) or X570_RE.search(t)):
        categoria = "MOBO TOP"
        titulo = "Mobo TOP (TUF/Elite/Aorus/Strix/…)"
        if price is None:
            return False, categoria, titulo, None, "MOBO top sem preço", False
        if price < 550:
            return True, categoria, titulo, price, "Mobo TOP < 550", True
        else:
            return False, categoria, titulo, price, "Mobo TOP ≥ 550", False

    # RAM DDR4 — 16GB ≤ 300, 8GB ≤ 150
    if DDR4_RE.search(t):
        categoria = "RAM DDR4"
        if RAM_16GB_RE.search(t):
            titulo = "DDR4 16GB"
            if price is not None and price <= 300:
                return True, categoria, titulo, price, "DDR4 16GB ≤ 300", False
            return False, categoria, titulo, price, "DDR4 16GB > 300 ou sem preço", False
        if RAM_8GB_RE.search(t):
            titulo = "DDR4 8GB"
            if price is not None and price <= 150:
                return True, categoria, titulo, price, "DDR4 8GB ≤ 150", False
            return False, categoria, titulo, price, "DDR4 8GB > 150 ou sem preço", False

    # SSD 1TB NVMe/M.2 ≤ 460
    if SSD_RE.search(t) and ONE_TB_RE.search(t) and NVME_M2_RE.search(t):
        categoria = "SSD 1TB NVMe/M.2"
        titulo = "SSD 1TB NVMe/M.2"
        if price is not None and price <= 460:
            return True, categoria, titulo, price, "SSD 1TB ≤ 460", False
        return False, categoria, titulo, price, "SSD 1TB > 460 ou sem preço", False

    # GABINETE: 3 fans ≤ 160; 4+ fans ≤ 220
    if GABINETE_RE.search(t):
        categoria = "Gabinete"
        titulo = "Gabinete (com fans)"
        fans = count_fans(t)
        if price is None:
            return False, categoria, titulo, None, "gabinete sem preço", False
        if fans >= 4 and price <= 220:
            return True, categoria, f"Gabinete {fans} fans", price, "≥4 fans ≤ 220", False
        if fans >= 3 and price <= 160:
            return True, categoria, f"Gabinete {fans} fans", price, "≥3 fans ≤ 160", False
        return False, categoria, f"Gabinete {fans or 0} fans", price, "gabinete fora das regras", False

    # Water cooler ≤ 200
    if WATER_COOLER_RE.search(t):
        categoria = "Water Cooler"
        titulo = "Water Cooler"
        if price is not None and price <= 200:
            return True, categoria, titulo, price, "Water cooler ≤ 200", False
        return False, categoria, titulo, price, "Water cooler > 200 ou sem preço", False

    # Teclado Redragon (superiores ao Kumara) ≤ 160 — IGNORA se > 160
    if REDRAGON_RE.search(t):
        categoria = "Teclado"
        titulo = "Redragon"
        if KUMARA_RE.search(t):
            return False, categoria, "Kumara", price, "Kumara bloqueado (apenas superiores)", False
        if price is not None and price <= 160:
            return True, categoria, "Redragon (não Kumara)", price, "Teclado Redragon ≤ 160", False
        return False, categoria, "Redragon (não Kumara)", price, "Teclado Redragon > 160 ou sem preço", False

    # PS5 — sempre alerta (sem cabeçalho)
    if PS5_RE.search(t):
        categoria = "Console"
        titulo = "PS5"
        return True, categoria, titulo, price, "PS5", False

    # Sem match
    return False, "Outros", "—", price, "sem match", False

# ---------------------------------------------
# DUP GUARD
# ---------------------------------------------
class LRUSeen:
    def __init__(self, maxlen: int = 500):
        self.maxlen = maxlen
        self.map: Dict[int, float] = {}

    def seen(self, msg_id: int) -> bool:
        if msg_id in self.map:
            return True
        if len(self.map) > self.maxlen:
            # limpa metade mais antiga
            for k, _ in sorted(self.map.items(), key=lambda kv: kv[1])[: self.maxlen // 2]:
                self.map.pop(k, None)
        self.map[msg_id] = time.time()
        return False

seen_cache = LRUSeen()

# ---------------------------------------------
# MAIN
# ---------------------------------------------
def main():
    log.info("Conectando ao Telegram...")
    with TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH) as client:
        log.info("Conectado.")
        dialogs = client.get_dialogs()

        username_to_entity = {}
        for d in dialogs:
            try:
                if d.entity and getattr(d.entity, "username", None):
                    username_to_entity["@{}".format(d.entity.username.lower())] = d.entity
            except:
                pass

        resolved_entities = []
        for uname in MONITORED_USERNAMES:
            ent = username_to_entity.get(uname)
            if ent is None:
                log.warning("Canal não encontrado (ignorado): %s", uname)
            else:
                resolved_entities.append(ent)

        if resolved_entities:
            log.info("▶️ Canais resolvidos: " + ", ".join(f"@{getattr(e, 'username', '')}" for e in resolved_entities if getattr(e, "username", None)))
        else:
            log.info("▶️ Canais resolvidos: ")
        log.info("✅ Logado — monitorando %d canais…", len(resolved_entities))
        log.info("▶️ Rodando. Pressione Ctrl+C para sair.")

        @client.on(events.NewMessage(chats=resolved_entities if resolved_entities else None))
        async def handler(event):
            try:
                if seen_cache.seen(event.id):
                    return
                raw_text = (event.raw_text or "").strip()
                if not raw_text:
                    return
                chan_user = getattr(event.chat, "username", None)
                chan_disp = f"@{chan_user}" if chan_user else "(desconhecido)"

                is_match, categoria, titulo, price, motivo, use_hdr = classify_and_match(raw_text)

                # LOG detalhado
                if is_match:
                    p = f"R$ {price:.2f}" if price is not None else "R$ ?"
                    log.info("[%-17s] MATCH → %s — %s — %s (%s)", chan_disp, categoria, titulo, p, motivo)
                    # montar mensagem com cabeçalho e rodapé
                    msg = raw_text
                    if use_hdr:
                        msg = "Corre!🔥 " + msg
                    msg = footer_with_channel(msg, chan_user)
                    send_alert_to_all(msg)
                else:
                    p = f"R$ {price:.2f}" if price is not None else "R$ ?"
                    log.info("[%-17s] IGNORADO → %s — %s — %s (%s)", chan_disp, categoria, titulo, p, motivo)

            except Exception as e:
                log.exception("Handler error: %s", e)

        client.run_until_disconnected()

if __name__ == "__main__":
    main()
