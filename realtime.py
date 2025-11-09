#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, List, Optional

from telethon import events
from telethon.sessions import StringSession
from telethon.errors.rpcerrorlist import AuthKeyDuplicatedError
from telethon import TelegramClient

import aiohttp
import json
from collections import defaultdict

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
    # se nenhum presente:
    joined = " ou ".join(keys)
    raise RuntimeError(
        f"Variável de ambiente '{joined}' ausente. Defina no .env local ou em Environment Variables do Render."
    )

def optional_env(key: str, default: Optional[str] = None) -> Optional[str]:
    return os.getenv(key, default)

# Lê credenciais (com fallback para nomes antigos)
API_ID = int(require_env("TELEGRAM_API_ID", "API_ID"))
API_HASH = require_env("TELEGRAM_API_HASH", "API_HASH")
STRING_SESSION = require_env("TELEGRAM_STRING_SESSION", "STRING_SESSION")
BOT_TOKEN = require_env("TELEGRAM_TOKEN", "BOT_TOKEN")

# Canais monitorados
MONITORED_CHANNELS = optional_env("MONITORED_CHANNELS", "")
CHANNEL_USERNAMES = [c.strip() for c in MONITORED_CHANNELS.split(",") if c.strip()]

# Destinos (um ou vários chat IDs separados por vírgula)
DESTS_RAW = optional_env("USER_DESTINATIONS", optional_env("USER_CHAT_ID", ""))
USER_DESTINATIONS = [int(x.strip()) for x in DESTS_RAW.split(",") if x.strip()]

if not CHANNEL_USERNAMES:
    log.warning("Nenhum canal em MONITORED_CHANNELS — nada será monitorado.")

if not USER_DESTINATIONS:
    log.warning("Nenhum destino configurado (USER_DESTINATIONS/USER_CHAT_ID).")

# ---------------------------
# HTTP/Telegram Bot API
# ---------------------------
class BotSender:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"

    async def send(self, chat_id: int, text: str, parse_mode: Optional[str] = None):
        # Telegram limita 4096 chars por mensagem. Quebramos se necessário.
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
    if len(text) <= maxlen:
        return [text]
    out = []
    buf = []
    cur = 0
    lines = text.splitlines(True)  # mantém quebras
    for ln in lines:
        if cur + len(ln) > maxlen:
            out.append("".join(buf))
            buf = [ln]
            cur = len(ln)
        else:
            buf.append(ln)
            cur += len(ln)
    if buf:
        out.append("".join(buf))
    return out

bot_sender = BotSender(BOT_TOKEN)

# ---------------------------
# PARSE & MATCH RULES
# ---------------------------
PRICE_RE = re.compile(
    r"(?:R\$\s*|(?<!\d))(\d{1,3}(?:\.\d{3})*(?:,\d{2})|\d+(?:,\d{2})?)(?!\d)",
    flags=re.IGNORECASE
)

def br_to_float(s: str) -> Optional[float]:
    # Converte "2.970,90" -> 2970.90 ; "2970,90" -> 2970.90 ; "2970" -> 2970.0
    s = s.strip()
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None

def extract_prices(txt: str) -> List[float]:
    vals = []
    for m in PRICE_RE.finditer(txt):
        val = br_to_float(m.group(1))
        if val is not None:
            vals.append(val)
    return vals

def any_price_leq(txt: str, limit: float) -> bool:
    return any(p <= limit for p in extract_prices(txt))

def contains_any(txt: str, terms: List[str]) -> bool:
    t = txt.lower()
    return any(term.lower() in t for term in terms)

# Regras inspiradas no que você vinha usando
def matches_rules(txt: str) -> bool:
    t = txt.lower()

    # 1) PS5 (console)
    if contains_any(t, ["playstation 5", "ps5"]) and contains_any(t, ["slim", "edição digital", "digital"]):
        return True

    # 2) GPUs alvo
    if contains_any(t, ["rtx 5060", "5060 ti", "rx 7600"]):
        return True

    # 3) RAM DDR4 8GB 3200 ≤ 180
    if contains_any(t, ["ddr4"]) and contains_any(t, ["8gb", "8 gb"]) and contains_any(t, ["3200"]):
        if any_price_leq(t, 180.0):
            return True

    # 4) SSD NVMe M.2 1TB ≤ 460
    if contains_any(t, ["nvme", "m.2"]) and contains_any(t, ["1tb", "1 tb", "1tera", "1 tera"]):
        if any_price_leq(t, 460.0):
            return True

    # 5) Placa-mãe (exemplos)
    if contains_any(t, ["b550"]) and any_price_leq(t, 550.0):
        return True
    if contains_any(t, ["x570"]) and any_price_leq(t, 680.0):
        return True
    if contains_any(t, ["lga1700"]) and any_price_leq(t, 680.0):
        return True

    # 6) CPU ≤ 900
    if contains_any(t, ["ryzen", "intel core", "i3-", "i5-", "i7-", "i9-"]):
        if any_price_leq(t, 900.0):
            return True

    # 7) Gabinete com ≥4 fans ≤ 180
    if contains_any(t, ["gabinete"]) and contains_any(t, ["4 fan", "4fan", "4 fans", "quatro fans"]):
        if any_price_leq(t, 180.0):
            return True

    # 8) PSU — exemplos (bronze/gold) com preços absurdamente baixos validam
    if contains_any(t, ["fonte", "psu", "power supply"]):
        # Bronze 650/750 ou Gold 750/850/1000/1200 com preço plausível
        if contains_any(t, ["650w", "750w"]) and contains_any(t, ["80 plus bronze", "bronze"]):
            if any_price_leq(t, 350.0):
                return True
        if contains_any(t, ["750w", "850w", "1000w", "1200w"]) and contains_any(t, ["80 plus gold", "gold"]):
            if any_price_leq(t, 350.0):
                return True

    # 9) iClamper (sempre útil)
    if "iclamper" in t:
        return True

    # 10) Teclados Redragon
    if "redragon" in t and contains_any(t, ["kumara", "k552"]) :
        return True
    if "redragon" in t and contains_any(t, ["elf pro", "k649"]) and any_price_leq(t, 160.0):
        return True

    return False

# ---------------------------
# ACUMULADOR POR CANAL (para juntar mensagens complementares)
# ---------------------------
DEBOUNCE_SECONDS = int(os.getenv("ACCUMULATE_SECONDS", "15"))

@dataclass
class Accum:
    texts: List[str] = field(default_factory=list)
    any_match: bool = False
    task: Optional[asyncio.Task] = None
    title_hint: Optional[str] = None  # primeira linha do primeiro post

accums: Dict[int, Accum] = defaultdict(Accum)  # chat_id -> Accum

async def flush_channel(chat_id: int, channel_name: str):
    """Envia o pacote acumulado do canal, se ele contém algo 'match'."""
    acc = accums.get(chat_id)
    if not acc:
        return
    if not acc.texts:
        accums.pop(chat_id, None)
        return

    full_text = "".join(join_with_spacing(acc.texts))
    if acc.any_match:
        header = f"Fonte: @{channel_name}\n\n"
        payload = header + full_text
        # Envia para todos os destinos
        for dest in USER_DESTINATIONS:
            await bot_sender.send(dest, payload)
        log.info(f"· envio=ok → destino(s)={','.join(map(str, USER_DESTINATIONS))}")
    else:
        log.info(f"[@{channel_name:18}] IGNORADO (sem match após acumulação)")

    accums.pop(chat_id, None)

def join_with_spacing(texts: List[str]) -> List[str]:
    # une mantendo quebras em branco entre blocos diferentes,
    # e evitando duplicar 'Fonte:' e 'Grupo Telegram:' etc.
    cleaned: List[str] = []
    for t in texts:
        t = t.strip()
        if not t:
            continue
        cleaned.append(t)
        if not t.endswith("\n"):
            cleaned.append("\n")
        cleaned.append("\n")
    return cleaned

def sanitize_text(t: str) -> str:
    # Garante newlines limpos; evita caracteres invisíveis comuns
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    return t

# ---------------------------
# MAIN
# ---------------------------
async def main():
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

    try:
        with client:
            # Resolver canais
            resolved = []
            for uname in CHANNEL_USERNAMES:
                try:
                    ent = client.loop.run_until_complete(client.get_entity(uname))
                    resolved.append(ent)
                except Exception as e:
                    log.error(f"Falha ao resolver {uname}: {e}")
            names_map = {}  # chat_id -> username sem '@'
            for ent in resolved:
                uname = getattr(ent, "username", None) or str(ent.id)
                names_map[ent.id] = uname
            log.info(
                "▶️ Canais resolvidos: " + ", ".join([f"@{n}" for n in names_map.values()])
            )
            log.info(f"✅ Logado — monitorando {len(resolved)} canais…")
            log.info("▶️ Rodando. Pressione Ctrl+C para sair.")

            @client.on(events.NewMessage(chats=resolved))
            async def handler(event):
                try:
                    chat_id = event.chat_id
                    ch_name = names_map.get(chat_id, str(chat_id))
                    text = sanitize_text(event.message.message or event.raw_text or "")
                    if not text:
                        return

                    # Acumula
                    acc = accums[chat_id]
                    acc.texts.append(text)

                    # Registrar se algum bloco bateu as regras
                    matched = matches_rules(text)
                    acc.any_match = acc.any_match or matched

                    if matched:
                        log.info(f"[@{ch_name:18}] MATCH    → {text[:40].replace(chr(10),' ')}…")
                    else:
                        log.info(f"[@{ch_name:18}] IGNORADO → {text[:40].replace(chr(10),' ')}… reason=sem match")

                    # (re)agenda flush para esse canal
                    if acc.task and not acc.task.done():
                        acc.task.cancel()
                        try:
                            await acc.task
                        except:
                            pass
                    acc.task = asyncio.create_task(_delay_flush(chat_id, ch_name))

                except Exception as e:
                    log.exception(f"Erro no handler: {e}")

            await client.run_until_disconnected()

    except AuthKeyDuplicatedError:
        log.error(
            "AuthKeyDuplicatedError: sua sessão foi usada em outro IP ao mesmo tempo.\n"
            "→ Gere uma NOVA TELEGRAM_STRING_SESSION e substitua no Render.\n"
            "→ Certifique-se de não executar essa mesma sessão localmente."
        )
        sys.exit(1)

async def _delay_flush(chat_id: int, ch_name: str):
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        await flush_channel(chat_id, ch_name)
    except asyncio.CancelledError:
        # haverá um novo evento e rearmará o timer
        pass
    except Exception as e:
        log.exception(f"Erro no flush: {e}")

# ---------------------------
# ENTRYPOINT
# ---------------------------
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Encerrado por KeyboardInterrupt.")
    except Exception as e:
        if isinstance(e, AuthKeyDuplicatedError):
            log.error(
                "AuthKeyDuplicatedError: sua sessão foi usada em outro IP ao mesmo tempo.\n"
                "→ Gere uma NOVA TELEGRAM_STRING_SESSION e substitua no Render.\n"
                "→ Certifique-se de não executar essa mesma sessão localmente."
            )
        else:
            log.exception("Falha inesperada.")
        sys.exit(1)
