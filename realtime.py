#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio, logging, os, re, sys
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from collections import defaultdict
from telethon import events, TelegramClient
from telethon.sessions import StringSession
from telethon.errors.rpcerrorlist import AuthKeyDuplicatedError
from telethon.tl.types import Channel, Chat
import aiohttp

# ---------------- LOG ----------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("realtime")

# ---------------- ENV ----------------
def require_env(*keys: str) -> str:
    for k in keys:
        v = os.getenv(k)
        if v:
            return v
    raise RuntimeError(f"Variável de ambiente '{' ou '.join(keys)}' ausente.")

def optional_env(key: str, default: Optional[str] = None) -> Optional[str]:
    return os.getenv(key, default)

API_ID = int(require_env("TELEGRAM_API_ID", "API_ID"))
API_HASH = require_env("TELEGRAM_API_HASH", "API_HASH")
STRING_SESSION = require_env("TELEGRAM_STRING_SESSION", "STRING_SESSION")
BOT_TOKEN = require_env("TELEGRAM_TOKEN", "BOT_TOKEN")

# usernames liberados (sem @)
CHANNEL_USERNAMES = [c.strip().lstrip("@").lower()
                     for c in optional_env("MONITORED_CHANNELS","").split(",") if c.strip()]

# NOVO: IDs liberados (ex.: -1003286562822,-1002063152651)
MONITORED_IDS = []
_raw_ids = optional_env("MONITORED_IDS", "")
if _raw_ids:
    for x in _raw_ids.split(","):
        x = x.strip()
        if not x: continue
        try:
            MONITORED_IDS.append(int(x))
        except:  # permite “-100…”
            pass

ALLOW_ANY = optional_env("ALLOW_ANY","false").lower() in ("1","true","yes")

DESTS_RAW = optional_env("USER_DESTINATIONS", optional_env("USER_CHAT_ID",""))
USER_DESTINATIONS = [int(x.strip()) for x in DESTS_RAW.split(",") if x.strip()]

# ---------------- SENDER -------------
class BotSender:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"
    async def send(self, chat_id: int, text: str):
        for chunk in split_telegram(text, 4096):
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{self.base}/sendMessage", json={"chat_id": chat_id, "text": chunk}) as r:
                    if r.status != 200:
                        log.error(f"Falha ao enviar p/{chat_id}: {r.status} {await r.text()}")

def split_telegram(text: str, maxlen: int) -> List[str]:
    if len(text) <= maxlen: return [text]
    out, cur = [], ""
    for ln in text.splitlines(True):
        if len(cur)+len(ln) > maxlen:
            out.append(cur); cur = ln
        else:
            cur += ln
    if cur: out.append(cur)
    return out

bot_sender = BotSender(BOT_TOKEN)

# ---------------- REGRAS -------------
PRICE_RE = re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})|\d+(?:,\d{2})?)", re.I)
HEADER_HINT = re.compile(r"^([\u2600-\u27BF\U0001F300-\U0001FAFF]{1,3}\s*)?(console|processador|placa|fonte|gabinete|ssd|mem[óo]ria|monitor|teclado|mouse|headset|notebook|kit|🔥|🚨|⚠️)", re.I)

def br_to_float(s: str) -> Optional[float]:
    try: return float(s.replace(".","").replace(",",".").strip())
    except: return None

def extract_prices(txt: str) -> List[float]:
    return [v for v in (br_to_float(m.group(1)) for m in PRICE_RE.finditer(txt)) if v is not None]

def any_price_leq(txt: str, limit: float) -> bool:
    return any(p <= limit for p in extract_prices(txt))

def contains_any(txt: str, terms: List[str]) -> bool:
    t = txt.lower()
    return any(term.lower() in t for term in terms)

def matches_rules(txt: str) -> bool:
    t = txt.lower()
    if contains_any(t, ["playstation 5","ps5"]) and contains_any(t, ["slim","edição digital","digital"]): return True
    if contains_any(t, ["rtx 5060","5060 ti","rx 7600"]): return True
    if "iclamper" in t: return True

    # CPU ≤ 900
    if contains_any(t, ["ryzen","intel core","i3-","i5-","i7-","i9-"]) and any_price_leq(t, 900): return True

    # SSD NVMe 1TB ≤ 460
    if contains_any(t, ["nvme","m.2"]) and contains_any(t, ["1tb","1 tb","1 tera","1tera"]) and any_price_leq(t, 460): return True

    # RAM DDR4 8GB 3200 ≤ 180
    if "ddr4" in t and contains_any(t, ["8gb","8 gb"]) and "3200" in t and any_price_leq(t, 180): return True

    # Placas-mãe
    if "a520" in t and any_price_leq(t, 320): return True
    if "b550" in t and any_price_leq(t, 550): return True
    if "x570" in t and any_price_leq(t, 680): return True
    if "lga1700" in t and any_price_leq(t, 680): return True

    # Gabinete 4+ fans ≤ 180
    if "gabinete" in t and contains_any(t, ["4 fan","4fan","4 fans","quatro fans"]) and any_price_leq(t, 180): return True

    # Fontes
    if contains_any(t, ["fonte","psu","power supply"]):
        if contains_any(t, ["650w","750w"]) and contains_any(t, ["80 plus bronze","bronze"]) and any_price_leq(t, 350): return True
        if contains_any(t, ["750w","850w","1000w","1200w"]) and contains_any(t, ["80 plus gold","gold"]) and any_price_leq(t, 350): return True

    # Teclados Redragon
    if "redragon" in t and contains_any(t, ["kumara","k552"]): return True
    if "redragon" in t and contains_any(t, ["elf pro","k649"]) and any_price_leq(t, 160): return True

    return False

def looks_like_header(line: str) -> bool:
    l = (line or "").strip()
    if not l: return False
    if HEADER_HINT.search(l): return True
    if len(l) <= 120 and any(w in l.lower() for w in ["console","processador","placa","fonte","gabinete","ssd","memória","memoria","monitor","teclado","mouse","headset","notebook","kit"]):
        return True
    return False

# ------------ ACÚMULO / FLUSH ------------
DEBOUNCE_SECONDS = int(os.getenv("ACCUMULATE_SECONDS","12"))

@dataclass
class Accum:
    lines: List[str] = field(default_factory=list)
    task: Optional[asyncio.Task] = None

accums: Dict[int, Accum] = defaultdict(Accum)
names_map: Dict[int, str] = {}   # id -> display
allow_map: Dict[int, bool] = {}  # id -> allowed

def sanitize_text(t: str) -> str:
    return (t or "").replace("\r\n","\n").replace("\r","\n")

def resolve_display(ent) -> str:
    username = getattr(ent,"username", None)
    title = getattr(ent,"title", None)
    if username: return f"@{username}"
    if isinstance(ent,(Channel,Chat)) and title: return title
    return str(ent.id)

def chat_allowed_by_display(display: str) -> bool:
    if ALLOW_ANY: return True
    key = display.lower().lstrip("@")
    return key in CHANNEL_USERNAMES

def chat_allowed(chat_id: int, display: str) -> bool:
    if ALLOW_ANY: return True
    if chat_id in MONITORED_IDS: return True
    return chat_allowed_by_display(display)

def split_items_by_chunks(text: str) -> List[str]:
    text = sanitize_text(text).strip()
    raw_chunks = re.split(r"\n{2,}", text)
    out: List[str] = []
    for chunk in raw_chunks:
        lines = [l for l in chunk.split("\n") if l.strip()]
        if not lines: continue
        cur, temp = [], []
        for ln in lines:
            if looks_like_header(ln) and cur:
                temp.append("\n".join(cur)); cur = [ln]
            else:
                cur.append(ln)
        if cur: temp.append("\n".join(cur))
        for c in temp:
            k = c.strip()
            if k and k not in out:
                out.append(k)
    return out

async def send_block(block: str, source: str):
    payload = f"{block}\n\nFonte: {source}"
    for dest in USER_DESTINATIONS:
        await bot_sender.send(dest, payload)
    log.info(f"· envio=ok → destinos={','.join(map(str, USER_DESTINATIONS))}")

async def flush_block(chat_id: int, source: str):
    acc = accums.get(chat_id)
    if not acc or not acc.lines:
        accums.pop(chat_id, None); return
    full = sanitize_text("\n".join(acc.lines)).strip()
    items = split_items_by_chunks(full)
    any_sent = False
    for it in items:
        if matches_rules(it):
            await send_block(it, source); any_sent = True
    if not any_sent:
        log.info(f"(drop) bloco sem match válido para {source}")
    accums.pop(chat_id, None)

async def schedule_flush(chat_id: int, source: str):
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        await flush_block(chat_id, source)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.exception(f"Erro no flush: {e}")

# ---------------- MAIN ----------------
async def main():
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    try:
        async with client:
            # resolve whitelist p/ log
            resolved = []
            for uname in CHANNEL_USERNAMES:
                try:
                    ent = await client.get_entity(uname)
                    disp = resolve_display(ent)
                    names_map[ent.id] = disp
                    allow_map[ent.id] = True
                    resolved.append(disp)
                except Exception as e:
                    log.error(f"Falha ao resolver @{uname}: {e}")
            if resolved:
                log.info("▶️ Canais resolvidos: " + ", ".join(resolved))
            if MONITORED_IDS:
                log.info("▶️ IDs liberados: " + ", ".join(map(str, MONITORED_IDS)))
            log.info("✅ Logado — monitorando mensagens… (whitelist por @username e por ID)")
            log.info("▶️ Rodando. Pressione Ctrl+C para sair.")

            @client.on(events.NewMessage())
            async def handler(event):
                try:
                    chat_id = event.chat_id
                    if chat_id not in names_map:
                        try:
                            ent = await event.get_chat()
                            disp = resolve_display(ent)
                        except Exception:
                            disp = str(chat_id)
                        names_map[chat_id] = disp

                    display = names_map.get(chat_id, str(chat_id))
                    if not chat_allowed(chat_id, display):
                        # silenciosamente ignora fora da whitelist
                        return

                    text = sanitize_text(event.message.message or event.raw_text or "")
                    if not text.strip():
                        return

                    # ENVIO IMEDIATO SE A PRÓPRIA MENSAGEM JÁ BATE REGRA
                    if matches_rules(text):
                        await send_block(text, display)
                        log.info(f"[{display:18}] MATCH → envio imediato")
                        return

                    # senão, acumula e faz flush por debounce (ideal p/ posts com “partes”)
                    acc = accums[chat_id]
                    acc.lines.append(text)
                    if acc.task and not acc.task.done():
                        acc.task.cancel()
                        try: await acc.task
                        except: pass
                    acc.task = asyncio.create_task(schedule_flush(chat_id, display))
                    log.info(f"[{display:18}] ACUMULANDO")

                except Exception as e:
                    log.exception(f"Erro no handler: {e}")

            await client.run_until_disconnected()

    except AuthKeyDuplicatedError:
        log.error("AuthKeyDuplicatedError: sessão usada em outro IP ao mesmo tempo. Gere nova STRING_SESSION e use só aqui.")
        sys.exit(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Encerrado.")
    except Exception:
        log.exception("Falha inesperada.")
        sys.exit(1)
