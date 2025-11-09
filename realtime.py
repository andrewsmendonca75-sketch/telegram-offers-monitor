# realtime.py
# -*- coding: utf-8 -*-
import os
import re
import time
import json
import logging
from typing import List, Optional, Tuple, Dict, Set

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

# Canais a monitorar (usernames, separados por vírgula). Ex.: "@talkpc,@pcdorafa,..."
MONITORED_CHANNELS_RAW = os.getenv("MONITORED_CHANNELS", "")
# Destinos para onde enviar o alerta. Pode ser número (chat id) ou @canal (se o bot for admin).
# Ex.: "1818469361,@TalkPC"
USER_DESTINATIONS_RAW = os.getenv("USER_DESTINATIONS", os.getenv("USER_CHAT_ID", ""))

# Timeout básico de retries no envio pelo bot
BOT_RETRY = int(os.getenv("BOT_RETRY", "2"))

# ---------------------------------------------
# HELPERS — normalização de listas do ENV
# ---------------------------------------------
def _split_list(val: str) -> List[str]:
    if not val:
        return []
    parts = [p.strip() for p in val.split(",") if p.strip()]
    return parts

def _norm_username(u: str) -> Optional[str]:
    if not u:
        return None
    u = u.strip()
    if not u:
        return None
    # Se for um número/ID, não é username de canal
    if re.fullmatch(r"\d+", u):
        return None
    # Normaliza para @lowercase (Telethon resolve case-insensitive)
    u = u.lower()
    if not u.startswith("@"):
        u = "@"+u
    return u

MONITORED_USERNAMES: List[str] = []
for x in _split_list(MONITORED_CHANNELS_RAW):
    nu = _norm_username(x)
    if nu:
        MONITORED_USERNAMES.append(nu)
if not MONITORED_USERNAMES:
    log.warning("MONITORED_CHANNELS vazio — nada será filtrado (handler ouvirá tudo, mas filtrará por 0 canais).")
    log.info("▶️ Canais: (nenhum)")
else:
    log.info(f"▶️ Canais: {', '.join(MONITORED_USERNAMES)}")

USER_DESTINATIONS: List[str] = _split_list(USER_DESTINATIONS_RAW)
if not USER_DESTINATIONS:
    log.warning("USER_DESTINATIONS/USER_CHAT_ID não definido; nada será enviado.")
else:
    log.info(f"📬 Destinos: {', '.join(USER_DESTINATIONS)}")

# ---------------------------------------------
# TELEGRAM BOT API
# ---------------------------------------------
BOT_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

def bot_send_text(dest: str, text: str) -> Tuple[bool, str]:
    """
    Envia texto via Bot API, sem parse_mode (evita 'unsupported parse_mode').
    'dest' pode ser chat_id numérico ou @username (se o bot for admin no canal).
    """
    payload = {
        "chat_id": dest,
        "text": text,
        "disable_web_page_preview": True,
        # NÃO usar parse_mode para evitar 'unsupported parse_mode' com textos arbitrários.
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
            # retry simples
            for _ in range(BOT_RETRY):
                time.sleep(0.6)
                ok, msg = bot_send_text(dest, text)
                if ok:
                    log.info("· envio=ok (retry) → destino=bot")
                    break
            if not ok:
                log.error(f"· Falha ao enviar via bot (depois de retry): {msg}")

# ---------------------------------------------
# PARSER DE PREÇO (robusto p/ BR)
# ---------------------------------------------
PRICE_REGEX = re.compile(
    r"""
    (?:
        (?:r\$\s*)?                # opcional "R$"
        (?:
            \d{1,3}(?:\.\d{3})+    # 1.234 ou 12.345.678
            (?:,\d{2})?            # ,99
          | \d+(?:,\d{2})?         # 89 ou 89,90
          | \d+\.\d{2}             # 89.90 (alguns posts)
        )
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

def _to_float(num: str) -> Optional[float]:
    s = num.strip()
    s = re.sub(r"\s+", "", s)

    if "," in s:
        # Formato BR: 3.199,90 → 3199.90
        s = s.replace(".", "").replace(",", ".")
    else:
        if "." in s:
            parts = s.split(".")
            if len(parts) == 2:
                left, right = parts
                # Heurística: se direita tem 3 dígitos e esquerda é número, é milhar BR (3.199 => 3199)
                if right.isdigit() and len(right) == 3 and left.isdigit():
                    s = left + right
                else:
                    # 89.90 provável decimal; mantém
                    pass
            else:
                # Muitos pontos → milhares: remove todos
                s = s.replace(".", "")
    try:
        val = float(s)
        if 0 < val < 100000:
            return val
    except Exception:
        return None
    return None

def parse_lowest_price_brl(text: str) -> Optional[float]:
    """
    Encontra o menor preço plausível no texto.
    Usamos o menor porque posts às vezes listam antigo e atual; quase sempre o menor é o da oferta.
    """
    candidates: List[float] = []
    for m in PRICE_REGEX.finditer(text):
        raw = m.group(0)
        # remove prefixo R$
        raw_num = re.sub(r"(?i)^r\$\s*", "", raw).strip()
        val = _to_float(raw_num)
        if val is not None:
            candidates.append(val)
    if not candidates:
        return None
    # descarta valores centesimais muito baixos (erros de OCR tipo 3.199 => 3.20)
    cleaned = [v for v in candidates if v >= 5.0]
    if not cleaned:
        cleaned = candidates
    return min(cleaned) if cleaned else None

# ---------------------------------------------
# REGRAS (REGEX)
# ---------------------------------------------

# GPUs
GPU_RE = re.compile(r"\b(?:rtx\s*5060|rx\s*7600)\b", re.IGNORECASE)

# CPUs Intel (alertar 14400F/KF e “superiores” comuns; inclui 12600F/KF que você pediu)
INTEL_CPU_OK = re.compile(
    r"""\b(?:
        i5[-\s]*12(?:600|700)k?f?   |   # i5-12600(F/KF), i5-12700(KF) (acima de 12400F)
        i5[-\s]*13(?:400|500|600)k?f? |
        i5[-\s]*14(?:400|500|600)k?f? |
        i7[-\s]*12(?:700|900)k?f? |
        i7[-\s]*13(?:700|900)k?f? |
        i7[-\s]*14(?:700|900)k?f? |
        i9[-\s]*\d{4,5}k?f?
    )\b""",
    re.IGNORECASE | re.VERBOSE
)
INTEL_12400F = re.compile(r"\bi5[-\s]*12400f\b", re.IGNORECASE)  # você quis permitir
INTEL_12600F_KF = re.compile(r"\bi5[-\s]*12600k?f?\b", re.IGNORECASE)  # 12600F/KF
INTEL_14400F = re.compile(r"\bi5[-\s]*14400k?f?\b", re.IGNORECASE)

# CPUs AMD (Ryzen 7 5700 / 5700X e superiores clássicos AM4)
AMD_CPU_OK = re.compile(
    r"""\b(?:
        ryzen\s*7\s*5700x?     |
        ryzen\s*7\s*5800x3?d?  |
        ryzen\s*9\s*5900x      |
        ryzen\s*9\s*5950x
    )\b""",
    re.IGNORECASE | re.VERBOSE
)

# Placas-mãe (Intel LGA1700) — aceitar M opcional e limitar preço <= 680
MB_INTEL_RE = re.compile(r"\b(?:h610m?|b660m?|b760m?|z690|z790)\b", re.IGNORECASE)
# Placas-mãe (AMD AM4) — B550/X570 (A520 explicitamente NÃO)
MB_AMD_RE = re.compile(r"\b(?:b550m?|x570)\b", re.IGNORECASE)
MB_A520_RE = re.compile(r"\ba520m?\b", re.IGNORECASE)  # para bloquear

# Gabinete — heurística para contagem de fans
FAN_COUNT_RE = re.compile(
    r"""(?:
        (?:(\d+)\s*(?:fans?|coolers?|ventoinhas?)) |
        (?:(\d+)\s*x\s*120\s*mm) |
        (?:(\d+)\s*x\s*fan)
    )""",
    re.IGNORECASE | re.VERBOSE
)

GABINETE_RE = re.compile(r"\bgabinete\b", re.IGNORECASE)

# PSU — somente Bronze/Gold >= 600W e <= 350
PSU_RE = re.compile(r"\b(?:fonte|psu)\b", re.IGNORECASE)
PSU_CERT_RE = re.compile(r"\b(?:80\s*\+?\s*plus\s*)?(?:bronze|gold)\b", re.IGNORECASE)
PSU_WATTS_RE = re.compile(r"\b(\d{3,4})\s*w\b", re.IGNORECASE)

# Water cooler — apenas <= 200
WATER_COOLER_RE = re.compile(r"\bwater\s*cooler\b", re.IGNORECASE)

# PS5 console (para permitir alertar console)
PS5_CONSOLE_RE = re.compile(r"\bplaystation\s*5\b|\bps5\b", re.IGNORECASE)

# Filtro de linha iClamper
ICLAMPER_RE = re.compile(r"\biclamper\b|\bclamp(?:er)?\b", re.IGNORECASE)

# Kits de fans — números de 3 a 9
KIT_FANS_RE = re.compile(r"\b(?:kit\s*(?:de\s*)?(?:fans?|ventoinhas?)|ventoinhas?\s*kit)\b", re.IGNORECASE)

# Redragon teclados superiores ao Kumara por <= 160 (heurística simples: Redragon presente e NÃO conter 'Kumara')
REDRAGON_KB_RE = re.compile(r"\bredragon\b", re.IGNORECASE)
KUMARA_RE = re.compile(r"\bkumara\b", re.IGNORECASE)

# ---------------------------------------------
# MATCH LOGIC
# ---------------------------------------------
def count_fans(text: str) -> int:
    count = 0
    for m in FAN_COUNT_RE.finditer(text):
        nums = [n for n in m.groups() if n]
        for n in nums:
            try:
                v = int(n)
                count = max(count, v)
            except:
                pass
    return count

def should_alert(text: str) -> Tuple[bool, str]:
    t = text

    # GPUs (RTX 5060 / RX 7600) — sem preço mínimo específico
    if GPU_RE.search(t):
        return True, "GPU match (RTX 5060 / RX 7600)"

    # CPUs — preço <= 900
    price = parse_lowest_price_brl(t)

    # Intel: 14400F/KF ou superiores comuns + 12600F/KF + 12400F (permitido)
    if (
        INTEL_14400F.search(t)
        or INTEL_12600F_KF.search(t)
        or INTEL_12400F.search(t)
        or INTEL_CPU_OK.search(t)
    ):
        if price is not None and price <= 900:
            return True, f"CPU <= 900 (R$ {price:.2f})"
        else:
            return False, "CPU Intel, mas preço > 900 ou ausente"

    # AMD: Ryzen 7 5700 / 5700X e superiores
    if AMD_CPU_OK.search(t):
        if price is not None and price <= 900:
            return True, f"CPU <= 900 (R$ {price:.2f})"
        else:
            return False, "CPU AMD, mas preço > 900 ou ausente"

    # MOBOS Intel/AMD ≤ 680 (bloquear A520 sempre)
    if MB_A520_RE.search(t):
        return False, "A520 bloqueada"

    if MB_INTEL_RE.search(t) or MB_AMD_RE.search(t):
        if price is not None and price <= 680:
            return True, f"MOBO <= 680 (R$ {price:.2f})"
        else:
            return False, "MOBO, mas preço > 680 ou ausente"

    # GABINETE regra:
    # - Bloquear: sem fans ou <5 fans por menos de 150
    # - Alertar: até 230 com 5 fans ou mais
    if GABINETE_RE.search(t):
        fans = count_fans(t)
        if price is None:
            return False, "gabinete sem preço"
        if fans >= 5 and price <= 230:
            return True, f"gabinete ok: {fans} fans ≤ R$ 230 (R$ {price:.2f})"
        if price < 150 and fans < 5:
            return False, "gabinete bloqueado: <5 fans e preço < 150"
        return False, "gabinete fora das regras"

    # PSU (fonte) — somente Bronze/Gold, >= 600W, ≤ 350
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
            return True, f"PSU ok: {watts}W {PSU_CERT_RE.search(t).group(0)} ≤ R$ 350 (R$ {price:.2f})"
        else:
            return False, "PSU fora das regras"

    # Water cooler <= 200
    if WATER_COOLER_RE.search(t):
        if price is not None and price <= 200:
            return True, f"Water cooler ≤ 200 (R$ {price:.2f})"
        return False, "Water cooler > 200 ou sem preço"

    # PS5 console — sempre alertar (só replica o texto)
    if PS5_CONSOLE_RE.search(t):
        return True, "PS5 console"

    # Filtro de linha iClamper — sempre alertar
    if ICLAMPER_RE.search(t):
        return True, "iClamper"

    # Kit de fans (3 a 9 unidades)
    if KIT_FANS_RE.search(t):
        # checar se menciona 3..9
        nums = re.findall(r"\b([3-9])\b", t)
        if nums:
            return True, f"kit de fans ({'/'.join(nums)} un.)"
        # fallback: se mencionar "3x120" etc, o count_fans já pegou
        fans = count_fans(t)
        if 3 <= fans <= 9:
            return True, f"kit de fans ({fans} un.)"
        return False, "kit de fans sem quantidade clara (3-9)"

    # Redragon superiores ao Kumara <= 160
    if REDRAGON_KB_RE.search(t):
        if not KUMARA_RE.search(t):
            if price is not None and price <= 160:
                return True, f"Redragon (não Kumara) ≤ 160 (R$ {price:.2f})"
            else:
                return False, "Redragon > 160 ou sem preço"
        else:
            # Kumara só alerta se você quiser; pelas regras novas, só “superiores ao Kumara”.
            return False, "Kumara bloqueado (apenas superiores)"

    # Sem match
    return False, "sem match"

# ---------------------------------------------
# CACHE anti-duplicado
# ---------------------------------------------
class LRUSeen:
    def __init__(self, maxlen: int = 300):
        self.maxlen = maxlen
        self.set: Dict[int, float] = {}

    def seen(self, msg_id: int) -> bool:
        if msg_id in self.set:
            return True
        # manutenção simples
        if len(self.set) > self.maxlen:
            # remove os mais antigos
            items = sorted(self.set.items(), key=lambda kv: kv[1])[: self.maxlen // 2]
            for k, _ in items:
                self.set.pop(k, None)
        self.set[msg_id] = time.time()
        return False

seen_cache = LRUSeen(400)

# ---------------------------------------------
# MAIN
# ---------------------------------------------
def main():
    log.info("Conectando ao Telegram...")
    with TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH) as client:
        log.info("Conectado.")

        # Warm cache — garante que get_dialogs popula entidades
        dialogs = client.get_dialogs()
        # Mapa @username -> entity
        username_to_entity = {}
        for d in dialogs:
            try:
                if d.entity and getattr(d.entity, "username", None):
                    username_to_entity["@{}".format(d.entity.username.lower())] = d.entity
            except Exception:
                pass

        # Resolve canais a partir dos usernames
        resolved_entities = []
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

        # Handler — só eventos vindos dos canais resolvidos
        @client.on(events.NewMessage(chats=resolved_entities if resolved_entities else None))
        async def handler(event):
            try:
                # Dup guard
                if seen_cache.seen(event.id):
                    return

                raw_text = (event.raw_text or "").strip()
                if not raw_text:
                    return

                ok, reason = should_alert(raw_text)
                chan = getattr(event.chat, "username", None)
                chan_disp = f"@{chan}" if chan else "(desconhecido)"

                price = parse_lowest_price_brl(raw_text)
                if ok:
                    if price is not None:
                        log.info(f"· [{chan_disp: <20}] match → price={price:.2f} reason={reason}")
                    else:
                        log.info(f"· [{chan_disp: <20}] match → price=None reason={reason}")

                    # Envia exatamente como está (sem cabeçalho e sem parse_mode)
                    send_alert_to_all(raw_text)
                else:
                    if price is not None:
                        log.info(f"· [{chan_disp: <20}] ignorado (sem match) → price={price:.2f} reason={reason}")
                    else:
                        log.info(f"· [{chan_disp: <20}] ignorado (sem match) → price=None reason={reason}")

            except Exception as e:
                log.exception(f"Handler error: {e}")

        client.run_until_disconnected()

if __name__ == "__main__":
    main()
