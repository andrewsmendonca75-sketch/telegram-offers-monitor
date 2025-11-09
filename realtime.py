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

# Canais a monitorar (usernames separados por vírgula) — ex.: "@talkpc,@pcdorafa"
MONITORED_CHANNELS_RAW = os.getenv("MONITORED_CHANNELS", "")
# Destinos (USER_DESTINATIONS tem prioridade; se vazio cai para USER_CHAT_ID)
USER_DESTINATIONS_RAW = os.getenv("USER_DESTINATIONS", os.getenv("USER_CHAT_ID", ""))

# Retries no envio pelo bot
BOT_RETRY = int(os.getenv("BOT_RETRY", "2"))

# ---------------------------------------------
# HELPERS — listas e usernames
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
    log.warning("MONITORED_CHANNELS vazio — o handler vai ouvir tudo, mas sem filtro de origem.")
    log.info("▶️ Canais: (nenhum)")
else:
    log.info(f"▶️ Canais: {', '.join(MONITORED_USERNAMES)}")

USER_DESTINATIONS: List[str] = _split_list(USER_DESTINATIONS_RAW)
if not USER_DESTINATIONS:
    log.warning("USER_DESTINATIONS/USER_CHAT_ID não definido; nada será enviado.")
else:
    log.info(f"📬 Destinos: {', '.join(USER_DESTINATIONS)}")

# ---------------------------------------------
# TELEGRAM BOT API — SEM parse_mode
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
# NORMALIZAÇÃO / PREÇO (robusto BR)
# ---------------------------------------------
ZW_RE = re.compile(r"[\u200b\u200c\u200d\u2060\u00a0]")

PRICE_REGEX = re.compile(
    r"""
    (?:
        (?:r\$\s*)?
        (?:
            \d{1,3}(?:\.\d{3})+(?:,\d{2})?  |
            \d+(?:,\d{2})?                  |
            \d+\.\d{2}
        )
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

def clean_text(s: str) -> str:
    return ZW_RE.sub(" ", s or "")

def _to_float(num: str) -> Optional[float]:
    s = re.sub(r"\s+", "", num.strip())
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        if "." in s:
            parts = s.split(".")
            if len(parts) == 2:
                left, right = parts
                if right.isdigit() and len(right) == 3 and left.isdigit():
                    s = left + right
            else:
                s = s.replace(".", "")
    try:
        val = float(s)
        if 0 < val < 100000:
            return val
    except Exception:
        return None
    return None

def parse_lowest_price_brl(text: str) -> Optional[float]:
    candidates = []
    for m in PRICE_REGEX.finditer(text):
        raw = re.sub(r"(?i)^r\$\s*", "", m.group(0)).strip()
        val = _to_float(raw)
        if val is not None:
            candidates.append(val)
    if not candidates:
        return None
    cleaned = [v for v in candidates if v >= 5.0]
    if not cleaned:
        cleaned = candidates
    return min(cleaned)

# ---------------------------------------------
# REGRAS (REGEX + heurísticas)
# ---------------------------------------------
GPU_RE = re.compile(r"(?i)\brtx\s*5060\b|\brx\s*7600\b")
INTEL_CPU_OK = re.compile(r"(?i)\bi5[-\s]*12(?:600|700)k?f?\b|\bi5[-\s]*13(?:400|500|600)k?f?\b|\bi5[-\s]*14(?:400|500|600)k?f?\b|\bi7[-\s]*1[234]\d{2}k?f?\b|\bi9[-\s]*\d{4,5}k?f?\b")
INTEL_12400F = re.compile(r"(?i)\bi5[-\s]*12400f\b")
INTEL_12600F_KF = re.compile(r"(?i)\bi5[-\s]*12600k?f?\b")
INTEL_14400F = re.compile(r"(?i)\bi5[-\s]*14400k?f?\b")

AMD_CPU_OK = re.compile(r"(?i)\bryzen\s*7\s*5700x?\b|\bryzen\s*7\s*5800x3?d?\b|\bryzen\s*9\s*5900x\b|\bryzen\s*9\s*5950x\b")

MB_INTEL_RE = re.compile(r"(?i)\bh610m?\b|\bb660m?\b|\bb760m?\b|\bz690\b|\bz790\b")
MB_AMD_RE   = re.compile(r"(?i)\bb550m?\b|\bx570\b")
MB_A520_RE  = re.compile(r"(?i)\ba520m?\b")

GABINETE_RE = re.compile(r"(?i)\bgabinete\b")
FAN_COUNT_RE = re.compile(r"(?i)(?:(\d+)\s*(?:fans?|coolers?|ventoinhas?)|(?:(\d+)\s*x\s*120\s*mm)|(?:(\d+)\s*x\s*fan))")

PSU_RE = re.compile(r"(?i)\b(fonte|psu)\b")
PSU_CERT_RE = re.compile(r"(?i)(?:80\s*\+?\s*plus\s*)?(?:bronze|gold)")
PSU_WATTS_RE = re.compile(r"(?i)\b(\d{3,4})\s*w\b")

WATER_COOLER_RE = re.compile(r"(?i)\bwater\s*cooler\b")
PS5_CONSOLE_RE = re.compile(r"(?i)\bplaystation\s*5\b|\bps5\b")
ICLAMPER_RE = re.compile(r"(?i)\biclamper\b|\bclamp(?:er)?\b")
KIT_FANS_RE = re.compile(r"(?i)\b(?:kit\s*(?:de\s*)?(?:fans?|ventoinhas?)|ventoinhas?\s*kit)\b")
REDRAGON_KB_RE = re.compile(r"(?i)\bredragon\b")
KUMARA_RE = re.compile(r"(?i)\bkumara\b")

BRANDS_RE = re.compile(r"(?i)\b(asus|msi|gigabyte|galax|zotac|inno3d|pny|asrock|powercolor|sapphire|xfx|redragon|corsair|aoc|mancer|hayom|gamemax|pichau|terabyte|kxg|kingston|crucial|lexar|adata|xpg)\b")

def count_fans(text: str) -> int:
    count = 0
    for m in FAN_COUNT_RE.finditer(text):
        for g in m.groups():
            if g and g.isdigit():
                count = max(count, int(g))
    return count

def pick_brand(text: str) -> Optional[str]:
    m = BRANDS_RE.search(text)
    return m.group(1).lower() if m else None

def pick_model_key(text: str) -> Optional[str]:
    # prioriza ordem de match
    if GPU_RE.search(text):
        brand = pick_brand(text) or "unknown"
        m = re.search(r"(?i)\b(rtx\s*5060|rx\s*7600)\b", text)
        return f"gpu:{brand}:{m.group(1).lower().replace(' ', '')}" if m else f"gpu:{brand}"
    if INTEL_12400F.search(text): return "cpu:intel:i5-12400f"
    if INTEL_12600F_KF.search(text): return "cpu:intel:i5-12600*f"
    if INTEL_14400F.search(text): return "cpu:intel:i5-14400*f"
    if INTEL_CPU_OK.search(text): return "cpu:intel:>=series"
    if AMD_CPU_OK.search(text):
        m = re.search(r"(?i)\b(ryzen\s*[79]\s*\d{4}x?3?d?)\b", text)
        return f"cpu:amd:{m.group(1).lower().replace(' ', '')}" if m else "cpu:amd:am4-high"
    if MB_A520_RE.search(text): return "mobo:a520"
    if MB_INTEL_RE.search(text): return "mobo:lga1700"
    if MB_AMD_RE.search(text): return "mobo:am4"
    if GABINETE_RE.search(text):
        fans = count_fans(text)
        return f"case:{fans or 0}fans"
    if PSU_RE.search(text): return "psu"
    if WATER_COOLER_RE.search(text): return "watercooler"
    if PS5_CONSOLE_RE.search(text): return "console:ps5"
    if ICLAMPER_RE.search(text): return "iclamper"
    if KIT_FANS_RE.search(text): return "kitfans"
    if REDRAGON_KB_RE.search(text) and not KUMARA_RE.search(text):
        return "kbd:redragon!kumara"
    return None

def should_alert(text: str) -> Tuple[bool, str]:
    t = clean_text(text)
    price = parse_lowest_price_brl(t)

    # GPUs
    if GPU_RE.search(t):
        return True, "GPU match (RTX 5060 / RX 7600)"

    # CPUs (<= 900)
    if INTEL_14400F.search(t) or INTEL_12600F_KF.search(t) or INTEL_12400F.search(t) or INTEL_CPU_OK.search(t):
        if price is not None and price <= 900:
            return True, f"CPU <= 900 (R$ {price:.2f})"
        return False, "CPU Intel, mas preço > 900 ou ausente"

    if AMD_CPU_OK.search(t):
        if price is not None and price <= 900:
            return True, f"CPU <= 900 (R$ {price:.2f})"
        return False, "CPU AMD, mas preço > 900 ou ausente"

    # MOBOS (bloqueia A520; alerta Intel/AMD <= 680)
    if MB_A520_RE.search(t):
        return False, "A520 bloqueada"
    if MB_INTEL_RE.search(t) or MB_AMD_RE.search(t):
        if price is not None and price <= 680:
            return True, f"MOBO <= 680 (R$ {price:.2f})"
        return False, "MOBO, mas preço > 680 ou ausente"

    # GABINETE
    if GABINETE_RE.search(t):
        fans = count_fans(t)
        if price is None:
            return False, "gabinete sem preço"
        if fans >= 5 and price <= 230:
            return True, f"gabinete ok: {fans} fans ≤ R$ 230 (R$ {price:.2f})"
        if price < 150 and fans < 5:
            return False, "gabinete bloqueado: <5 fans e preço < 150"
        return False, "gabinete fora das regras"

    # PSU
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
            level = PSU_CERT_RE.search(t).group(0)
            return True, f"PSU ok: {watts}W {level} ≤ R$ 350 (R$ {price:.2f})"
        return False, "PSU fora das regras"

    # Water cooler
    if WATER_COOLER_RE.search(t):
        if price is not None and price <= 200:
            return True, f"Water cooler ≤ 200 (R$ {price:.2f})"
        return False, "Water cooler > 200 ou sem preço"

    # PS5 / iClamper / kit de fans / Redragon > Kumara
    if PS5_CONSOLE_RE.search(t):
        return True, "PS5 console"

    if ICLAMPER_RE.search(t):
        return True, "iClamper"

    if KIT_FANS_RE.search(t):
        nums = re.findall(r"\b([3-9])\b", t)
        if nums:
            return True, f"kit de fans ({'/'.join(nums)} un.)"
        fans = count_fans(t)
        if 3 <= fans <= 9:
            return True, f"kit de fans ({fans} un.)"
        return False, "kit de fans sem quantidade clara (3-9)"

    if REDRAGON_KB_RE.search(t):
        if not KUMARA_RE.search(t):
            if price is not None and price <= 160:
                return True, f"Redragon (não Kumara) ≤ 160 (R$ {price:.2f})"
            return False, "Redragon > 160 ou sem preço"
        return False, "Kumara bloqueado (apenas superiores)"

    return False, "sem match"

# ---------------------------------------------
# ANTI-DUP + COOLDOWN (produto+marca+fonte)
# ---------------------------------------------
class LRUSeen:
    def __init__(self, maxlen: int = 400):
        self.maxlen = maxlen
        self.set: Dict[int, float] = {}

    def seen(self, msg_id: int) -> bool:
        if msg_id in self.set:
            return True
        if len(self.set) > self.maxlen:
            for k, _ in sorted(self.set.items(), key=lambda kv: kv[1])[: self.maxlen // 2]:
                self.set.pop(k, None)
        self.set[msg_id] = time.time()
        return False

seen_cache = LRUSeen(400)

# cooldown: (source, product_key) -> last_price
last_seen_price: Dict[Tuple[str, str], float] = {}

def pass_cooldown(source: str, product_key: Optional[str], price: Optional[float]) -> bool:
    """
    Regras:
    - se não tiver product_key ou price → passa (sem travar)
    - primeira vez → passa
    - se preço cair → passa
    - se variação >= 5% (pra cima ou pra baixo) → passa
    - senão → bloqueia
    """
    if product_key is None or price is None:
        return True
    key = (source, product_key)
    if key not in last_seen_price:
        last_seen_price[key] = price
        return True
    prev = last_seen_price[key]
    if price < prev:
        last_seen_price[key] = price
        return True
    delta = abs(price - prev) / prev if prev > 0 else 1.0
    if delta >= 0.05:
        last_seen_price[key] = price
        return True
    # não passou
    return False

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
                    username_to_entity["@" + d.entity.username.lower()] = d.entity
            except Exception:
                pass

        resolved_entities = []
        if MONITORED_USERNAMES:
            for uname in MONITORED_USERNAMES:
                ent = username_to_entity.get(uname)
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

                t = clean_text(raw_text)
                ok, reason = should_alert(t)

                chan_username = getattr(event.chat, "username", None)
                source = f"@{chan_username.lower()}" if chan_username else "(desconhecido)"
                price = parse_lowest_price_brl(t)
                product_key = pick_model_key(t)

                if ok:
                    # aplica cooldown
                    if pass_cooldown(source, product_key, price):
                        if price is not None:
                            log.info(f"· [{source: <20}] match → price={price:.2f} reason={reason} pk={product_key}")
                        else:
                            log.info(f"· [{source: <20}] match → price=None reason={reason} pk={product_key}")
                        send_alert_to_all(raw_text)  # envia texto puro (sem parse_mode)
                    else:
                        log.info(f"· [{source: <20}] bloqueado (cooldown) → price={price} pk={product_key}")
                else:
                    if price is not None:
                        log.info(f"· [{source: <20}] ignorado (sem match) → price={price:.2f} reason={reason}")
                    else:
                        log.info(f"· [{source: <20}] ignorado (sem match) → price=None reason={reason}")

            except Exception as e:
                log.exception(f"Handler error: {e}")

        client.run_until_disconnected()

if __name__ == "__main__":
    main()
