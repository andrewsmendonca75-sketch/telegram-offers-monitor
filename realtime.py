#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from collections import defaultdict

from telethon import events
from telethon.sessions import StringSession
from telethon.errors.rpcerrorlist import AuthKeyDuplicatedError
from telethon import TelegramClient

import aiohttp

# ---------------------------
# LOGGING
# ---------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("realtime")

# ---------------------------
# ENV HELPERS
# ---------------------------
def require_env(*keys: str) -> str:
    for k in keys:
        v = os.getenv(k)
        if v:
            return v
    joined = " ou ".join(keys)
    raise RuntimeError(
        f"Variável de ambiente '{joined}' ausente. Defina no .env local ou em Environment Variables do Render."
    )

def optional_env(key: str, default: Optional[str] = None) -> Optional[str]:
    return os.getenv(key, default)

# Credenciais (com fallback)
API_ID = int(require_env("TELEGRAM_API_ID", "API_ID"))
API_HASH = require_env("TELEGRAM_API_HASH", "API_HASH")
STRING_SESSION = require_env("TELEGRAM_STRING_SESSION", "STRING_SESSION")
BOT_TOKEN = require_env("TELEGRAM_TOKEN", "BOT_TOKEN")

# Canais monitorados
MONITORED_CHANNELS = optional_env("MONITORED_CHANNELS", "")
CHANNEL_USERNAMES = [c.strip().lstrip("@") for c in MONITORED_CHANNELS.split(",") if c.strip()]

# Destinos (um ou vários chat IDs separados por vírgula)
DESTS_RAW = optional_env("USER_DESTINATIONS", optional_env("USER_CHAT_ID", ""))
USER_DESTINATIONS = [int(x.strip()) for x in DESTS_RAW.split(",") if x.strip()]

if not CHANNEL_USERNAMES:
    log.warning("Nenhum canal em MONITORED_CHANNELS — nada será monitorado.")
if not USER_DESTINATIONS:
    log.warning("Nenhum destino configurado (USER_DESTINATIONS/USER_CHAT_ID).")

# ---------------------------
# Bot API sender
# ---------------------------
class BotSender:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"

    async def send(self, chat_id: int, text: str, parse_mode: Optional[str] = None):
        chunks = split_telegram(text, 4096)
        async with aiohttp.ClientSession() as sess:
            for chunk in chunks:
                payload = {"chat_id": chat_id, "text": chunk}
                if parse_mode:
                    payload["parse_mode"] = parse_mode
                async with sess.post(f"{self.base}/sendMessage", json=payload) as r:
                    if r.status != 200:
                        body = await r.text()
                        log.error(f"Falha ao enviar para {chat_id}: HTTP {r.status} — {body}")

def split_telegram(text: str, maxlen: int) -> List[str]:
    if len(text) <= maxlen: return [text]
    out, buf, cur = [], [], 0
    for ln in text.splitlines(True):  # mantém quebras
        if cur + len(ln) > maxlen:
            out.append("".join(buf)); buf = [ln]; cur = len(ln)
        else:
            buf.append(ln); cur += len(ln)
    if buf: out.append("".join(buf))
    return out

bot_sender = BotSender(BOT_TOKEN)

# ---------------------------
# Regras de parsing/match
# ---------------------------
PRICE_RE = re.compile(
    r"(?:R\$\s*|(?<!\d))(\d{1,3}(?:\.\d{3})*(?:,\d{2})|\d+(?:,\d{2})?)(?!\d)",
    flags=re.IGNORECASE
)

def br_to_float(s: str) -> Optional[float]:
    s = s.strip().replace(".", "").replace(",", ".")
    try: return float(s)
    except: return None

def extract_prices(txt: str) -> List[float]:
    vals = []
    for m in PRICE_RE.finditer(txt):
        v = br_to_float(m.group(1))
        if v is not None: vals.append(v)
    return vals

def any_price_leq(txt: str, limit: float) -> bool:
    return any(p <= limit for p in extract_prices(txt))

def contains_any(txt: str, terms: List[str]) -> bool:
    t = txt.lower()
    return any(term.lower() in t for term in terms)

def matches_rules(txt: str) -> bool:
    t = txt.lower()
    # 1) PS5
    if contains_any(t, ["playstation 5", "ps5"]) and contains_any(t, ["slim", "edição digital", "digital"]):
        return True
    # 2) GPUs
    if contains_any(t, ["rtx 5060", "5060 ti", "rx 7600"]):
        return True
    # 3) RAM DDR4 8GB 3200 ≤ 180
    if contains_any(t, ["ddr4"]) and contains_any(t, ["8gb", "8 gb"]) and contains_any(t, ["3200"]):
        if any_price_leq(t, 180.0): return True
    # 4) SSD NVMe 1TB ≤ 460
    if contains_any(t, ["nvme", "m.2"]) and contains_any(t, ["1tb", "1 tb", "1 tera", "1tera"]):
        if any_price_leq(t, 460.0): return True
    # 5) Placas-mãe
    if contains_any(t, ["b550"]) and any_price_leq(t, 550.0): return True
    if contains_any(t, ["x570"]) and any_price_leq(t, 680.0): return True
    if contains_any(t, ["lga1700"]) and any_price_leq(t, 680.0): return True
    # 6) CPU ≤ 900
    if contains_any(t, ["ryzen", "intel core", "i3-", "i5-", "i7-", "i9-"]) and any_price_leq(t, 900.0):
        return True
    # 7) Gabinete 4+ fans ≤ 180
    if contains_any(t, ["gabinete"]) and contains_any(t, ["4 fan", "4fan", "4 fans", "quatro fans"]) and any_price_leq(t, 180.0):
        return True
    # 8) PSUs
    if contains_any(t, ["fonte", "psu", "power supply"]):
        if contains_any(t, ["650w", "750w"]) and contains_any(t, ["80 plus bronze", "bronze"]) and any_price_leq(t, 350.0):
            return True
        if contains_any(t, ["750w", "850w", "1000w", "1200w"]) and contains_any(t, ["80 plus gold", "gold"]) and any_price_leq(t, 350.0):
            return True
    # 9) iClamper
    if "iclamper" in t: return True
    # 10) Redragon (exemplos)
    if "redragon" in t and contains_any(t, ["kumara", "k552"]): return True
    if "redragon" in t and contains_any(t, ["elf pro", "k649"]) and any_price_leq(t, 160.0): return True
    return False

# ---------------------------
# Acúmulo por canal
# ---------------------------
DEBOUNCE_SECONDS = int(os.getenv("ACCUMULATE_SECONDS", "15"))

@dataclass
class Accum:
    texts: List[str] = field(default_factory=list)
    any_match: bool = False
    task: Optional[asyncio.Task] = None

accums: Dict[int, Accum] = defaultdict(Accum)  # chat_id -> Accum

def sanitize_text(t: str) -> str:
    return (t or "").replace("\r\n", "\n").replace("\r", "\n")

def join_with_spacing(texts: List[str]) -> str:
    out = []
    for t in texts:
        t = t.strip()
        if not t: continue
        out.append(t)
        if not t.endswith("\n"): out.append("\n")
        out.append("\n")
    return "".join(out)

async def flush_channel(chat_id: int, channel_name: str):
    acc = accums.get(chat_id)
    if not acc or not acc.texts:
        accums.pop(chat_id, None); return
    full_text = join_with_spacing(acc.texts)
    if acc.any_match:
        header = f"Fonte: @{channel_name}\n\n"
        payload = header + full_text
        for dest in USER_DESTINATIONS:
            await bot_sender.send(dest, payload)
        log.info(f"· envio=ok → destino(s)={','.join(map(str, USER_DESTINATIONS))}")
    else:
        log.info(f"[@{channel_name:18}] IGNORADO (sem match após acumulação)")
    accums.pop(chat_id, None)

async def _delay_flush(chat_id: int, ch_name: str):
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        await flush_channel(chat_id, ch_name)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.exception(f"Erro no flush: {e}")

# ---------------------------
# MAIN
# ---------------------------
async def main():
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

    try:
        async with client:
            # Resolver canais (async/await correto)
            resolved = []
            names_map: Dict[int, str] = {}
            for uname in CHANNEL_USERNAMES:
                try:
                    ent = await client.get_entity(uname)
                    resolved.append(ent)
                    names_map[ent.id] = getattr(ent, "username", None) or str(ent.id)
                except Exception as e:
                    log.error(f"Falha ao resolver @{uname}: {e}")

            if resolved:
                log.info("▶️ Canais resolvidos: " + ", ".join([f"@{names_map[e.id]}" for e in resolved]))
            log.info(f"✅ Logado — monitorando {len(resolved)} canais…")
            log.info("▶️ Rodando. Pressione Ctrl+C para sair.")

            @client.on(events.NewMessage(chats=resolved if resolved else None))
            async def handler(event):
                try:
                    chat_id = event.chat_id
                    ch_name = names_map.get(chat_id, str(chat_id))
                    text = sanitize_text(event.message.message or event.raw_text or "")
                    if not text: return

                    acc = accums[chat_id]
                    acc.texts.append(text)

                    matched = matches_rules(text)
                    acc.any_match = acc.any_match or matched

                    prefix = "MATCH    " if matched else "IGNORADO "
                    reason = "" if matched else " reason=sem match"
                    preview = text.replace("\n", " ")[:40]
                    log.info(f"[@{ch_name:18}] {prefix}→ {preview}…{reason}")

                    # (re)agendar flush desse canal
                    if acc.task and not acc.task.done():
                        acc.task.cancel()
                        try: await acc.task
                        except: pass
                    acc.task = asyncio.create_task(_delay_flush(chat_id, ch_name))

                except Exception as e:
                    log.exception(f"Erro no handler: {e}")

            await client.run_until_disconnected()

    except AuthKeyDuplicatedError:
        log.error(
            "AuthKeyDuplicatedError: sua sessão foi usada em outro IP ao mesmo tempo.\n"
            "→ Gere uma NOVA TELEGRAM_STRING_SESSION e substitua no Render.\n"
            "→ Garanta que não há outra instância usando a mesma sessão."
        )
        sys.exit(1)

# ---------------------------
# ENTRYPOINT
# ---------------------------
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Encerrado por KeyboardInterrupt.")
    except AuthKeyDuplicatedError:
        log.error(
            "AuthKeyDuplicatedError: sua sessão foi usada em outro IP ao mesmo tempo.\n"
            "→ Gere uma NOVA TELEGRAM_STRING_SESSION e substitua no Render.\n"
            "→ Garanta que não há outra instância usando a mesma sessão."
        )
        sys.exit(1)
    except Exception:
        log.exception("Falha inesperada.")
        sys.exit(1)
