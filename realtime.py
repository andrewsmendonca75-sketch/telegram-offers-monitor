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
from telethon.tl.types import Channel, Chat
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

# Credenciais
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
# Bot API
# ---------------------------
class BotSender:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"

    async def send(self, chat_id: int, text: str, parse_mode: Optional[str] = None):
        for chunk in split_telegram(text, 4096):
            async with aiohttp.ClientSession() as sess:
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
    out, cur = [], ""
    for ln in text.splitlines(True):
        if len(cur) + len(ln) > maxlen:
            out.append(cur); cur = ln
        else:
            cur += ln
    if cur: out.append(cur)
    return out

bot_sender = BotSender(BOT_TOKEN)

# ---------------------------
# Regras de parsing/match
# ---------------------------

# Agora **exige** "R$" antes do número
PRICE_RE = re.compile(
    r"R\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})|\d+(?:,\d{2})?)",
    flags=re.IGNORECASE
)

def br_to_float(s: str) -> Optional[float]:
    s = s.strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except:
        return None

def extract_prices(txt: str) -> List[float]:
    vals = []
    for m in PRICE_RE.finditer(txt):
        v = br_to_float(m.group(1))
        if v is not None:
            vals.append(v)
    return vals

def any_price_leq(txt: str, limit: float) -> bool:
    prices = extract_prices(txt)
    return any(p <= limit for p in prices)

def contains_any(txt: str, terms: List[str]) -> bool:
    t = txt.lower()
    return any(term.lower() in t for term in terms)

# Regras alvo
def matches_rules(txt: str) -> bool:
    t = txt.lower()
    # 1) PS5 Slim Digital
    if contains_any(t, ["playstation 5", "ps5"]) and contains_any(t, ["slim", "edição digital", "digital"]):
        return True
    # 2) GPUs alvo
    if contains_any(t, ["rtx 5060", "5060 ti", "rx 7600"]):
        return True
    # 3) RAM DDR4 8GB 3200 ≤ 180
    if contains_any(t, ["ddr4"]) and contains_any(t, ["8gb", "8 gb"]) and contains_any(t, ["3200"]):
        if any_price_leq(t, 180.0): 
            return True
        else:
            return False
    # 4) SSD NVMe 1TB ≤ 460
    if contains_any(t, ["nvme", "m.2"]) and contains_any(t, ["1tb", "1 tb", "1 tera", "1tera"]):
        if any_price_leq(t, 460.0): 
            return True
        else:
            return False
    # 5) Placas-mãe
    if contains_any(t, ["b550"]) and any_price_leq(t, 550.0): return True
    if contains_any(t, ["x570"]) and any_price_leq(t, 680.0): return True
    if contains_any(t, ["lga1700"]) and any_price_leq(t, 680.0): return True
    # 6) CPU ≤ 900
    if contains_any(t, ["ryzen", "intel core", "i3-", "i5-", "i7-", "i9-"]):
        if any_price_leq(t, 900.0):
            return True
        else:
            return False
    # 7) Gabinete 4+ fans ≤ 180
    if contains_any(t, ["gabinete"]) and contains_any(t, ["4 fan", "4fan", "4 fans", "quatro fans"]):
        if any_price_leq(t, 180.0):
            return True
        else:
            return False
    # 8) PSUs
    if contains_any(t, ["fonte", "psu", "power supply"]):
        if contains_any(t, ["650w", "750w"]) and contains_any(t, ["80 plus bronze", "bronze"]) and any_price_leq(t, 350.0):
            return True
        if contains_any(t, ["750w", "850w", "1000w", "1200w"]) and contains_any(t, ["80 plus gold", "gold"]) and any_price_leq(t, 350.0):
            return True
        return False
    # 9) iClamper (sempre)
    if "iclamper" in t:
        return True
    # 10) Redragon
    if "redragon" in t and contains_any(t, ["kumara", "k552"]):
        return True
    if "redragon" in t and contains_any(t, ["elf pro", "k649"]) and any_price_leq(t, 160.0):
        return True
    return False

# Heurística de “início de produto”
HEADER_HINT = re.compile(
    r"^([\u2600-\u27BF\U0001F300-\U0001FAFF]{1,3}\s*)?("
    r"console|processador|placa|fonte|gabinete|ssd|memória|memoria|monitor|teclado|mouse|headset|notebook|kit|🔥|🚨|⚠️)",
    re.IGNORECASE
)

def looks_like_header(line: str) -> bool:
    l = (line or "").strip()
    if not l:
        return False
    if HEADER_HINT.search(l):
        return True
    if len(l) <= 120 and any(w in l.lower() for w in [
        "console","processador","placa","fonte","gabinete","ssd","memória","memoria",
        "monitor","teclado","mouse","headset","notebook","kit"
    ]):
        return True
    return False

# ---------------------------
# Acúmulo por canal (por BLOCO de produto)
# ---------------------------
DEBOUNCE_SECONDS = int(os.getenv("ACCUMULATE_SECONDS", "15"))

@dataclass
class Accum:
    lines: List[str] = field(default_factory=list)
    any_match: bool = False
    task: Optional[asyncio.Task] = None

accums: Dict[int, Accum] = defaultdict(Accum)  # chat_id -> Accum

def sanitize_text(t: str) -> str:
    return (t or "").replace("\r\n", "\n").replace("\r", "\n")

def join_block(lines: List[str]) -> str:
    out = []
    for t in lines:
        t = (t or "").rstrip()
        if not t:
            out.append("\n")
        else:
            out.append(t + ("\n" if not t.endswith("\n") else ""))
    return "".join(out).strip("\n")

async def flush_block(chat_id: int, display_source: str):
    acc = accums.get(chat_id)
    if not acc or not acc.lines:
        accums.pop(chat_id, None)
        return

    block = join_block(acc.lines)

    # Validação FINAL do bloco
    if acc.any_match and matches_rules(block):
        payload = f"{block}\n\nFonte: {display_source}"
        for dest in USER_DESTINATIONS:
            await bot_sender.send(dest, payload)
        log.info(f"· envio=ok → destinos={','.join(map(str, USER_DESTINATIONS))}")
    else:
        log.info(f"(drop) bloco sem match válido para {display_source}")

    accums.pop(chat_id, None)

async def schedule_flush(chat_id: int, display_source: str):
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        await flush_block(chat_id, display_source)
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
            # Resolver canais e nome de exibição
            resolved = []
            names_map: Dict[int, str] = {}

            for uname in CHANNEL_USERNAMES:
                try:
                    ent = await client.get_entity(uname)
                    resolved.append(ent)
                    username = getattr(ent, "username", None)
                    title = getattr(ent, "title", None)
                    if username:
                        display = f"@{username}"
                    elif isinstance(ent, (Channel, Chat)) and title:
                        display = title
                    else:
                        display = str(ent.id)
                    names_map[ent.id] = display
                except Exception as e:
                    log.error(f"Falha ao resolver @{uname}: {e}")

            if resolved:
                log.info("▶️ Canais resolvidos: " + ", ".join([names_map[e.id] for e in resolved]))
            log.info(f"✅ Logado — monitorando {len(resolved)} canais…")
            log.info("▶️ Rodando. Pressione Ctrl+C para sair.")

            @client.on(events.NewMessage(chats=resolved if resolved else None))
            async def handler(event):
                try:
                    chat_id = event.chat_id

                    # **Corrige fonte -100…**: resolve nome/título dinamicamente se necessário
                    if chat_id not in names_map:
                        try:
                            ent = await event.get_chat()
                            username = getattr(ent, "username", None)
                            title = getattr(ent, "title", None)
                            if username:
                                names_map[chat_id] = f"@{username}"
                            elif isinstance(ent, (Channel, Chat)) and title:
                                names_map[chat_id] = title
                            else:
                                names_map[chat_id] = str(chat_id)
                        except Exception:
                            names_map[chat_id] = str(chat_id)

                    display_source = names_map.get(chat_id, str(chat_id))
                    text = sanitize_text(event.message.message or event.raw_text or "")
                    if not text:
                        return

                    # Se chegou um NOVO CABEÇALHO e já havia linhas acumuladas, flush do bloco anterior
                    if looks_like_header(text) and accums[chat_id].lines:
                        if accums[chat_id].task and not accums[chat_id].task.done():
                            accums[chat_id].task.cancel()
                            try:
                                await accums[chat_id].task
                            except:
                                pass
                        await flush_block(chat_id, display_source)

                    acc = accums[chat_id]  # defaultdict

                    # Acumula a linha atual
                    acc.lines.append(text)

                    # Marca se essa linha isoladamente casa regras (com preço real, por causa do "R$")
                    line_match = matches_rules(text)
                    acc.any_match = acc.any_match or line_match

                    prefix = "MATCH    " if line_match else "IGNORADO "
                    preview = text.replace("\n", " ")[:60]
                    log.info(f"[{display_source:18}] {prefix}→ {preview}…")

                    # Reagenda flush deste bloco
                    if acc.task and not acc.task.done():
                        acc.task.cancel()
                        try:
                            await acc.task
                        except:
                            pass
                    acc.task = asyncio.create_task(schedule_flush(chat_id, display_source))

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
    except AuthKeyDuplicicatedError:
        log.error(
            "AuthKeyDuplicicatedError: sessão usada em outro IP simultaneamente. "
            "Gere nova STRING_SESSION e use só nesta instância."
        )
        sys.exit(1)
    except Exception:
        log.exception("Falha inesperada.")
        sys.exit(1)
