#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
realtime.py — robusto, com logs e alertas ao admin.

Ambientes esperados (env):
  TG_API_ID            -> int (obrigatório)
  TG_API_HASH          -> str (obrigatório)
  TG_BOT_TOKEN         -> str (opcional; se ausente, usa sessão de usuário)
  TG_USER_SESSION      -> str (opcional; StringSession para "userbot")
  ADMIN_CHAT_IDS       -> str (opcional; ids separados por vírgula para alertas)
  HEALTHCHECK_PORT     -> int (opcional; se setado, sobe HTTP /healthz)
  LOG_LEVEL            -> str (DEBUG, INFO, WARNING, ERROR) padrão INFO
  SENTRY_DSN           -> str (opcional; ativa Sentry)
  APP_NAME             -> str (opcional; nome p/ logs/alertas)
"""

import os
import sys
import time
import json
import signal
import asyncio
import logging
import traceback
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

# ---- Sentry opcional ---------------------------------------------------------
SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0.0)
    except Exception:
        # Não falhe por causa do Sentry
        pass

# ---- Logging -----------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger(os.getenv("APP_NAME", "realtime"))

# ---- Config ------------------------------------------------------------------
APP_NAME = os.getenv("APP_NAME", "TalkPC")
API_ID = int(os.environ.get("TG_API_ID", "0"))
API_HASH = os.environ.get("TG_API_HASH", "")
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
USER_SESSION = os.environ.get("TG_USER_SESSION")
ADMIN_CHAT_IDS = [
    int(x.strip()) for x in os.getenv("ADMIN_CHAT_IDS", "").split(",") if x.strip()
]
HEALTHCHECK_PORT = os.getenv("HEALTHCHECK_PORT")
HEALTHCHECK_PORT = int(HEALTHCHECK_PORT) if HEALTHCHECK_PORT else None

if not API_ID or not API_HASH:
    log.error("TG_API_ID e TG_API_HASH são obrigatórios.")
    sys.exit(1)

# ---- Telethon ----------------------------------------------------------------
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ---- Healthcheck HTTP opcional -----------------------------------------------
class _Healthz(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            body = json.dumps({"ok": True, "app": APP_NAME}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

def _start_healthcheck_server(port: int):
    def _run():
        httpd = HTTPServer(("0.0.0.0", port), _Healthz)
        log.info(f"Healthcheck HTTP em 0.0.0.0:{port} (/healthz)")
        with suppress(Exception):
            httpd.serve_forever()
    t = Thread(target=_run, daemon=True)
    t.start()

# ---- Utilitários --------------------------------------------------------------
async def notify_admins(client: TelegramClient, text: str):
    if not ADMIN_CHAT_IDS:
        return
    for chat_id in ADMIN_CHAT_IDS:
        with suppress(Exception):
            await client.send_message(chat_id, text)

def _fmt_ex(e: BaseException) -> str:
    tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    if len(tb) > 3500:
        tb = tb[-3500:]
    return f"⚠️ {APP_NAME} erro:\n\n{tb}"

# ---- Handlers seguros ---------------------------------------------------------
def safe_handler(fn):
    async def wrapper(event):
        try:
            await fn(event)
        except Exception as e:
            logging.exception("Exceção em handler %s", fn.__name__)
            try:
                # feedback ao chat (sem vazar stack enorme)
                await event.reply("⚠️ Ocorreu um erro ao processar sua mensagem.")
            except Exception:
                pass
            # alerta admin
            client = event.client  # Telethon injeta
            with suppress(Exception):
                await notify_admins(client, _fmt_ex(e))
    return wrapper

# ---- Core do bot --------------------------------------------------------------
async def build_client() -> TelegramClient:
    if BOT_TOKEN:
        # Modo BOT
        client = TelegramClient("bot_session", API_ID, API_HASH)
        await client.start(bot_token=BOT_TOKEN)
        me = await client.get_me()
        log.info(f"Bot iniciado como @{getattr(me, 'username', None)} (id={me.id})")
        return client
    else:
        # Modo USER (StringSession obrigatória)
        if not USER_SESSION:
            log.error("TG_USER_SESSION ausente (necessário para modo userbot sem TG_BOT_TOKEN).")
            sys.exit(1)
        client = TelegramClient(StringSession(USER_SESSION), API_ID, API_HASH)
        await client.start()
        me = await client.get_me()
        log.info(f"Userbot iniciado: {me.first_name} (id={me.id})")
        return client

# Exemplos de handlers ----------------------
@safe_handler
async def on_ping(event: events.NewMessage.Event):
    if not event.raw_text:
        return
    txt = event.raw_text.strip().lower()
    if txt in ("/ping", "ping", "/ping@"+APP_NAME.lower()):
        await event.reply("🏓 pong")

@safe_handler
async def on_any_message(event: events.NewMessage.Event):
    # Log simples de chegada (sem dados sensíveis)
    log.debug("Mensagem recebida de %s em %s", event.sender_id, event.chat_id)
    # Exemplo: ignore mensagens do próprio bot
    if event.is_private and (await event.get_sender()).is_self:
        return
    # Coloque aqui sua lógica de negócio...
    # e.g., roteamento por palavras-chave, etc.

# ---- Run loop ----------------------------------------------------------------
async def run_bot_forever():
    if HEALTHCHECK_PORT:
        _start_healthcheck_server(HEALTHCHECK_PORT)

    client = await build_client()

    # Registra handlers
    client.add_event_handler(on_ping, events.NewMessage(pattern=r"(?i)^/ping\b"))
    client.add_event_handler(on_any_message, events.NewMessage)

    await notify_admins(client, f"✅ {APP_NAME} iniciado às {time.strftime('%Y-%m-%d %H:%M:%S')}.")

    # Mantém vivo até desconectar
    log.info("Conectado. Aguardando eventos…")
    await client.run_until_disconnected()

# ---- EntryPoint com backoff ---------------------------------------------------
def main():
    # uvloop opcional
    with suppress(Exception):
        import uvloop
        uvloop.install()

    backoff = 1
    max_backoff = 60

    # Sinais para shutdown gentil
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    stop_event = asyncio.Event()

    def _signal_handler(sig, frame):
        log.warning(f"Recebido sinal {sig}. Encerrando…")
        loop.call_soon_threadsafe(stop_event.set)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    while True:
        log.info(">>> start realtime.py")
        try:
            loop.run_until_complete(
                asyncio.wait_for(
                    asyncio.shield(run_bot_forever()),
                    timeout=None
                )
            )
            # Se chegou aqui "limpo", é estranho — não deveria terminar.
            log.warning("run_bot_forever terminou SEM exceção. Investigando…")
            # Força reinício não imediato, e alerta admin no próximo ciclo
            time.sleep(2)
            backoff = min(backoff * 2, max_backoff)
        except asyncio.CancelledError:
            log.info("Cancelado. Saindo com 0.")
            sys.exit(0)
        except Exception as e:
            log.exception("Falha no loop principal — reiniciando com backoff %ss", backoff)
            # Tenta notificar admin fora do client (não disponível aqui). Apenas log/sentry.
            if SENTRY_DSN:
                with suppress(Exception):
                    sentry_sdk.capture_exception(e)
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

        # Sai se sinalizado
        if stop_event.is_set():
            log.info("Stop event set — encerrando processo.")
            break

    # Fecha loop com segurança
    pending = asyncio.all_tasks(loop=loop)
    for task in pending:
        task.cancel()
    with suppress(Exception):
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    loop.close()
    sys.exit(0)

if __name__ == "__main__":
    main()
