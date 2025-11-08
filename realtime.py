# realtime.py
import os
import sys
import json
import re
import asyncio
import signal
import fcntl
from typing import List, Dict, Any, Optional

from telethon import events
from telethon import functions
from telethon import types
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors.rpcerrorlist import (
    UsernameNotOccupiedError,
    AuthKeyDuplicatedError,
    FloodWaitError,
)

# -------------------------
# Config / Env
# -------------------------
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
STRING_SESSION = os.getenv("TELEGRAM_STRING_SESSION", "")
TARGET_CHAT = os.getenv("TARGET_CHAT", "me")  # destino dos alertas (ex.: @canalandrwss) ou "me"

CONFIG_PATH = os.getenv("CONFIG_PATH", "config.json")

# -------------------------
# Logging simples (prints)
# -------------------------
def info(msg: str): print(msg, flush=True)
def warn(msg: str): print(f"WARNING: {msg}", flush=True)
def err(msg: str):  print(f"ERROR: {msg}", flush=True)

# -------------------------
# Single-instance lock
# -------------------------
def acquire_single_instance_lock() -> int:
    lock_fd = os.open("/tmp/telegram_monitor.lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.lockf(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fd
    except OSError:
        err("Outra instância já está rodando; saindo.")
        sys.exit(0)

# -------------------------
# Leitura do config.json
# -------------------------
def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("channels", [])
    cfg.setdefault("keywords", [])
    cfg.setdefault("limits", {})
    return cfg

# -------------------------
# Regex úteis
# -------------------------

# Preço em R$
# Ex.: R$ 1.799,90 | 1799,90 | 1.799 | R$1799
PRICE_RE = re.compile(
    r'(?:r\$?\s*)?(\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})|\d+(?:,\d{2}))',
    re.I
)

def parse_brl_to_float(s: str) -> Optional[float]:
    try:
        s = s.strip().lower().replace("r$", "").strip()
        s = s.replace(".", "").replace(" ", "")
        s = s.replace(",", ".")
        return float(s)
    except Exception:
        return None

def find_lowest_price(text: str) -> Optional[float]:
    prices = []
    for m in PRICE_RE.finditer(text):
        val = parse_brl_to_float(m.group(0))
        if val is not None:
            prices.append(val)
    return min(prices) if prices else None

# --- Filtros para eliminar falsos positivos ---

NEG_PS5 = re.compile(
    r'\b(jogo|jogos|game|m[ií]dia|steelbook|dlc|capa|case|pel[ií]cula|suporte|dock|base|charging\s*station|grip|thumb|cooler|stand)\b',
    re.I
)
PS5_CONSOLE = re.compile(r'\b(ps5|playstation\s*5)\b', re.I)
PS5_CONSOLE_HINT = re.compile(r'\b(console|slim|edi[cç][aã]o|bundle|m[ií]dia\s*(digital|f[ií]sica)|vers[aã]o)\b', re.I)

DUALSENSE = re.compile(r'\b(dualsense|controle\s*(ps5|playstation\s*5))\b', re.I)
NEG_DUALSENSE = re.compile(r'\b(capa|case|grip|thumb|suporte|dock|base|charging\s*station|pel[ií]cula)\b', re.I)

RAM_SIZE = re.compile(r'\b(8\s*gb|16\s*gb)\b', re.I)
DDR4 = re.compile(r'\bddr\s*4\b', re.I)
NEG_RAM = re.compile(r'\b(notebook|laptop|so[\-\s]?dimm|sodimm|celular|smartphone|android|iphone|lpddr|ddr3|ddr5)\b', re.I)

WC_240 = re.compile(r'\bwater\.?\s*cooler\b.*\b240\s*mm\b|\b240\s*mm\b.*\bwater\.?\s*cooler\b', re.I)

def classify_product(text: str) -> Optional[str]:
    """Retorna uma categoria 'afinada' ou None (ignorar)."""
    t = text.lower()

    # PS5 console: requer dica de console e não pode ter termos negativos (jogo/acessório)
    if PS5_CONSOLE.search(t) and PS5_CONSOLE_HINT.search(t) and not NEG_PS5.search(t):
        return "ps5_console"

    # Controle PS5 (DualSense)
    if DUALSENSE.search(t) and not NEG_DUALSENSE.search(t):
        return "controle_ps5"

    # RAM DDR4 desktop 8/16GB (não notebook/celular/LPDDR/DDR3/DDR5)
    if RAM_SIZE.search(t) and DDR4.search(t) and not NEG_RAM.search(t):
        return "ram_ddr4"

    # Water Cooler 240mm
    if WC_240.search(t):
        return "water cooler 240mm"

    return None

# -------------------------
# Telethon helpers
# -------------------------
async def resolve_channels(client: TelegramClient, refs: List[str]) -> List[types.InputPeerChannel]:
    resolved: List[types.InputPeerChannel] = []
    for ref in refs:
        try:
            ent = await client.get_entity(ref)
            if hasattr(ent, "id") and hasattr(ent, "access_hash"):
                resolved.append(types.InputPeerChannel(ent.id, ent.access_hash))
            else:
                warn(f"Ignorando '{ref}': não é canal/supergrupo.")
        except UsernameNotOccupiedError:
            warn(f"⚠️ Username inexistente: {ref}")
        except ValueError as e:
            warn(f"⚠️ Não foi possível resolver '{ref}': {e}")
        except Exception as e:
            warn(f"⚠️ Erro inesperado ao resolver '{ref}': {e}")
    return resolved

async def get_target_entity(client: TelegramClient, target: str):
    # "me" envia para Mensagens Salvas
    if target.strip().lower() == "me":
        return "me"
    try:
        return await client.get_entity(target)
    except Exception as e:
        err(f"Não consegui resolver TARGET_CHAT '{target}': {e}. Vou usar 'me'.")
        return "me"

# -------------------------
# Mensagem de alerta
# -------------------------
def build_alert_text(src_channel: Optional[str], text: str, price: Optional[float]) -> str:
    ch = f"@{src_channel}" if src_channel else "Canal"
    header = f"🔥 Alerta em {ch}"
    if price is not None:
        return f"{header}\n\n• Preço encontrado: R$ {price:,.2f}\n\n{text}"
    return f"{header}\n\n{text}"

# -------------------------
# Main
# -------------------------
async def main():
    # valida envs
    if not API_ID or not API_HASH or not STRING_SESSION:
        err("API_ID/API_HASH/STRING_SESSION ausentes. Configure as variáveis de ambiente.")
        sys.exit(1)

    # lock single instance
    _lock = acquire_single_instance_lock()

    # carrega config
    cfg = load_config(CONFIG_PATH)
    channels_cfg: List[str] = cfg.get("channels", [])
    keywords_cfg: List[str] = cfg.get("keywords", [])
    limits_cfg: Dict[str, float] = cfg.get("limits", {})

    # normaliza keywords p/ comparação case-insensitive
    kw_set = set(k.strip().lower() for k in keywords_cfg if isinstance(k, str) and k.strip())

    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

    # graceful shutdown
    stop_event = asyncio.Event()

    def _graceful(*_):
        info("Recebi sinal — desconectando...")
        try:
            asyncio.create_task(client.disconnect())
        except Exception:
            pass
        stop_event.set()

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)

    # conecta
    try:
        await client.connect()
    except AuthKeyDuplicatedError:
        err("❌ AuthKeyDuplicatedError: a mesma sessão conectou em dois IPs (deploy paralelo). "
            "Gere uma nova TELEGRAM_STRING_SESSION e garanta 1 instância apenas.")
        return

    if not await client.is_user_authorized():
        err("Sessão não autorizada. Gere a TELEGRAM_STRING_SESSION corretamente (use make_session.py).")
        return

    # resolve canais
    resolved_chats = await resolve_channels(client, channels_cfg)
    if not resolved_chats:
        err("Nenhum canal válido no config. Encerrando.")
        return

    target_entity = await get_target_entity(client, TARGET_CHAT)

    info(f"✅ Logado — monitorando {len(resolved_chats)} canais…")

    # --- Handler de mensagens novas ---
    @client.on(events.NewMessage(chats=resolved_chats))
    async def on_new_message(event):
        try:
            raw = event.raw_text or ""
            t = raw.lower()

            # canal origem (melhor para log/alerta)
            src_username = None
            try:
                ch = await event.get_chat()
                src_username = getattr(ch, "username", None)
            except Exception:
                pass

            # 1) filtro fino (evita PS5 jogo/acessório; RAM celular/notebook; etc.)
            cat = classify_product(raw)

            # 2) se não bateu nas categorias finas, verifique keywords genéricas
            matched_keyword = None
            if cat is None:
                for k in kw_set:
                    if k in t:
                        matched_keyword = k
                        break

            if (cat is None) and (matched_keyword is None):
                # nada relevante → ignore
                return

            # 3) preço detectado
            price = find_lowest_price(raw)

            # 4) verifica limites quando existir
            #    prioridade: se cat mapeia exatamente a uma chave do limits_cfg, usa-a
            #    caso contrário, usa limite pela palavra-chave matched_keyword (se houver)
            limit_key = None
            if cat and cat in limits_cfg:
                limit_key = cat
            elif matched_keyword and matched_keyword in limits_cfg:
                limit_key = matched_keyword

            if limit_key is not None:
                limit_val = float(limits_cfg[limit_key])
                if price is None or price > limit_val:
                    # não atende limite → ignore
                    return

            # 5) envia alerta
            alert_text = build_alert_text(src_username, raw, price)
            await client.send_message(target_entity, alert_text)

        except FloodWaitError as e:
            warn(f"FloodWait: aguardando {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            warn(f"Erro no handler: {e}")

    # inicia e aguarda
    await client.start()
    info("▶️ Rodando. Pressione Ctrl+C para sair.")
    await stop_event.wait()
    info("✅ Encerrado.")

if __name__ == "__main__":
    asyncio.run(main())

