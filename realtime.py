# realtime.py
import os
import sys
import json
import re
import asyncio
import signal
import fcntl
import unicodedata
from typing import List, Dict, Any, Optional

import requests  # envio via Bot API
from telethon import events, types
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors.rpcerrorlist import (
    UsernameNotOccupiedError,
    AuthKeyDuplicatedError,
    FloodWaitError,
)

# =========================
# ENV VARS
# =========================
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
STRING_SESSION = os.getenv("TELEGRAM_STRING_SESSION", "")

# Envio primário: Bot API (gera notificação no seu Telegram)
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
USER_CHAT_ID = os.getenv("USER_CHAT_ID", "")  # ex.: 1818469361

# Fallback/duplicado: enviar também para um chat via Telethon (opcional)
TARGET_CHAT = os.getenv("TARGET_CHAT", "me")  # "me" = Mensagens Salvas
CONFIG_PATH = os.getenv("CONFIG_PATH", "config.json")

# =========================
# LOGS
# =========================
def info(msg: str): print(msg, flush=True)
def warn(msg: str): print(f"WARNING: {msg}", flush=True)
def err(msg: str):  print(f"ERROR: {msg}", flush=True)

# =========================
# SINGLE INSTANCE LOCK
# =========================
def acquire_single_instance_lock() -> int:
    lock_fd = os.open("/tmp/telegram_monitor.lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.lockf(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fd
    except OSError:
        err("Outra instância já está rodando; saindo.")
        sys.exit(0)

# =========================
# CONFIG
# =========================
def load_config(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        err(f"Falha ao ler {path}: JSON inválido ({e}). Corrija vírgulas finais/comentários.")
        sys.exit(1)
    cfg.setdefault("channels", [])
    cfg.setdefault("keywords", [])
    cfg.setdefault("limits", {})  # não usamos limites, mas mantemos compat.
    return cfg

# =========================
# NORMALIZA TEXTO
# =========================
def normalize_text(s: str) -> str:
    # normaliza forma, remove espaços invisíveis, colapsa espaços e remove acentos
    s = unicodedata.normalize('NFKC', s)
    s = s.replace('\u200b', '').replace('\u00A0', ' ')
    s = re.sub(r'\s+', ' ', s).strip().lower()
    s = unicodedata.normalize('NFD', s)
    s = ''.join(ch for ch in s if unicodedata.category(ch) != 'Mn')
    return s

# =========================
# PREÇO (robusto)
# ignora GB/TB/MHz/mm etc. e valores irreais (< 10)
# =========================
PRICE_RE = re.compile(
    r'(?:r\$\s*)?((?:\d{1,3}(?:[.\s]\d{3})+|\d+)(?:[.,]\d{2})?)'
    r'(?!\s*(?:gb|tb|mhz|ghz|mm|cm))\b',
    re.I
)
def parse_brl_to_float(s: str) -> Optional[float]:
    try:
        s = s.strip().lower()
        s = re.sub(r'[^\d\.,]', '', s)
        if ',' in s and s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '')
        v = float(s)
        if v < 10:
            return None
        return v
    except Exception:
        return None

def find_lowest_price(text: str) -> Optional[float]:
    vals = []
    for m in PRICE_RE.finditer(text):
        v = parse_brl_to_float(m.group(0))
        if v is not None:
            vals.append(v)
    return min(vals) if vals else None

# =========================
# FILTROS FINOS (PS5/DUALSENSE/RAM/WC)
# =========================
NEG_PS5 = re.compile(
    r'\b(jogo|jogos|game|midia|m[ií]dia|steelbook|dlc|capa|case|pelicula|pel[ií]cula|suporte|dock|base|charging\s*station|grip|thumb|cooler|stand)\b',
    re.I
)
PS5_CONSOLE = re.compile(r'\b(ps5|playstation\s*5)\b', re.I)
PS5_CONSOLE_HINT = re.compile(r'\b(console|slim|edicao|edi[cç][aã]o|bundle|midia\s*(digital|fisica|f[ií]sica)?|versao|versa[oã])\b', re.I)

DUALSENSE = re.compile(r'\b(dualsense|controle\s*(ps5|playstation\s*5))\b', re.I)
NEG_DUALSENSE = re.compile(r'\b(capa|case|grip|thumb|suporte|dock|base|charging\s*station|pelicula|pel[ií]cula)\b', re.I)

RAM_SIZE = re.compile(r'\b(8\s*gb|16\s*gb)\b', re.I)
DDR4 = re.compile(r'\bddr\s*4\b', re.I)
NEG_RAM = re.compile(r'\b(notebook|laptop|so[\-\s]?dimm|sodimm|celular|smartphone|android|iphone|lpddr|ddr3|ddr5)\b', re.I)

WC_240 = re.compile(r'\bwater\.?\s*cooler\b.*\b240\s*mm\b|\b240\s*mm\b.*\bwater\.?\s*cooler\b', re.I)

def classify_product(text: str) -> Optional[str]:
    t = normalize_text(text)
    if PS5_CONSOLE.search(t) and PS5_CONSOLE_HINT.search(t) and not NEG_PS5.search(t):
        return "ps5_console"
    if DUALSENSE.search(t) and not NEG_DUALSENSE.search(t):
        return "controle_ps5"
    if RAM_SIZE.search(t) and DDR4.search(t) and not NEG_RAM.search(t):
        return "ram_ddr4"
    if WC_240.search(t):
        return "water cooler 240mm"
    return None

# =========================
# PADRÕES ROBUSTOS (regex) PARA KEYWORDS
# =========================
KEYWORD_PATTERNS: Dict[str, re.Pattern] = {
    # SSDs
    "ssd nvme 1tb": re.compile(r'\b(ssd).*?(nvme).*?(1\s*tb)\b|\b(nvme).*?(ssd).*?(1\s*tb)\b', re.I),
    "ssd 1tb":      re.compile(r'\b(ssd).*?(1\s*tb)\b|\b(1\s*tb).*?(ssd)\b', re.I),

    # GPUs
    "rtx 5060":     re.compile(r'\brtx\s*50\s*60\b|\brtx\s*5060\b', re.I),
    "rx 7600":      re.compile(r'\brx\s*7600\b', re.I),

    # CPUs
    "ryzen 7 5700x": re.compile(r'\bryzen\s*7\s*5700x\b', re.I),
    "ryzen 7 5700":  re.compile(r'\bryzen\s*7\s*5700\b', re.I),

    # Fontes
    "fonte 650w":   re.compile(r'\b(fonte|psu)\b.*\b650\s*w\b|\b650\s*w\b.*\b(fonte|psu)\b', re.I),
    "fonte 600w":   re.compile(r'\b(fonte|psu)\b.*\b600\s*w\b|\b600\s*w\b.*\b(fonte|psu)\b', re.I),

    # Placa-mãe B550 (qualquer marca/modelo)
    "placa mae b550": re.compile(
        r'\b(placa\s*ma[e]|motherboard|mobo)\b.*\b(b550m?)\b|\b(b550m?)\b.*\b(placa\s*ma[e]|motherboard|mobo)\b', re.I
    ),
    "b550": re.compile(r'\bb550m?\b', re.I),
}

# =========================
# TELETHON HELPERS
# =========================
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
    if target.strip().lower() == "me":
        return "me"
    try:
        return await client.get_entity(target)
    except Exception as e:
        warn(f"Não consegui resolver TARGET_CHAT '{target}': {e}. Usarei 'me'.")
        return "me"

# =========================
# ENVIO DA NOTIFICAÇÃO
# =========================
def build_alert_text(src_channel: Optional[str], text: str, price: Optional[float]) -> str:
    ch = f"@{src_channel}" if src_channel else "Canal"
    header = f"🔥 Alerta em {ch}"
    if price is not None:
        return f"{header}\n\n• Preço encontrado: R$ {price:,.2f}\n\n{text}"
    return f"{header}\n\n{text}"

def send_via_bot_api(token: str, chat_id: str, text: str) -> bool:
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
            "parse_mode": "HTML"
        }, timeout=10)
        if resp.status_code != 200:
            warn(f"Bot API HTTP {resp.status_code}: {resp.text}")
            return False
        data = resp.json()
        if not data.get("ok"):
            warn(f"Bot API erro: {data}")
            return False
        return True
    except Exception as e:
        warn(f"Falha Bot API: {e}")
        return False

# =========================
# MAIN
# =========================
async def main():
    if not API_ID or not API_HASH or not STRING_SESSION:
        err("API_ID/API_HASH/STRING_SESSION ausentes. Configure as variáveis de ambiente.")
        sys.exit(1)

    _lock = acquire_single_instance_lock()

    cfg = load_config(CONFIG_PATH)
    channels_cfg: List[str] = cfg.get("channels", [])
    keywords_cfg: List[str] = cfg.get("keywords", [])
    # limits_cfg = cfg.get("limits", {})  # sem uso agora

    kw_set = set(normalize_text(k) for k in keywords_cfg if isinstance(k, str) and k.strip())

    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

    # shutdown limpo
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
        err("❌ AuthKeyDuplicatedError: sessão usada em paralelo. Gere nova TELEGRAM_STRING_SESSION e garanta 1 instância.")
        return

    if not await client.is_user_authorized():
        err("Sessão não autorizada. Gere a TELEGRAM_STRING_SESSION corretamente (use make_session.py).")
        return

    # resolve canais
    resolved_chats = await resolve_channels(client, channels_cfg)
    if not resolved_chats:
        err("Nenhum canal válido no config. Encerrando.")
        return

    # lista canais resolvidos (debug)
    names = []
    for peer in resolved_chats:
        try:
            ent = await client.get_entity(peer)
            uname = getattr(ent, "username", None)
            names.append(f"@{uname}" if uname else f"id:{getattr(ent,'id',None)}")
        except Exception:
            names.append("<?>")
    info("▶️ Canais resolvidos: " + ", ".join(names))

    target_entity = await get_target_entity(client, TARGET_CHAT)

    info(f"✅ Logado — monitorando {len(resolved_chats)} canais…")

    @client.on(events.NewMessage(chats=resolved_chats))
    async def on_new_message(event):
        try:
            raw = event.raw_text or ""
            t = normalize_text(raw)

            # origem
            src_username = None
            try:
                ch = await event.get_chat()
                src_username = getattr(ch, "username", None)
            except Exception:
                pass

            # 1) classificador fino (mais confiável)
            cat = classify_product(raw)

            # 2) padrões robustos por categoria (usa texto normalizado!)
            matched_keyword = None
            if cat is None:
                for key, rx in KEYWORD_PATTERNS.items():
                    if rx.search(t):
                        matched_keyword = key
                        break

            # 3) fallback: keywords simples do config (normalizado)
            if cat is None and matched_keyword is None:
                for k in kw_set:
                    if k in t:
                        matched_keyword = k
                        break

            # 3.1) Anti-falso-positivo PS5: se cair por "ps5" genérico,
            # exija que seja console real (não jogo/capa/suporte),
            # ou if for controle, aplique filtro de acessório.
            if cat is None and matched_keyword in {"ps5", "controle ps5", "dualsense"}:
                if matched_keyword == "ps5":
                    if not (PS5_CONSOLE.search(t) and PS5_CONSOLE_HINT.search(t)) or NEG_PS5.search(t):
                        info(f"· [{src_username or 'id'}] ignorado (ps5 não é console válido) → {raw[:80]!r}")
                        return
                    cat, matched_keyword = "ps5_console", None
                else:
                    # controle ps5/dualsense
                    if not DUALSENSE.search(t) or NEG_DUALSENSE.search(t):
                        info(f"· [{src_username or 'id'}] ignorado (acessório/cover de controle) → {raw[:80]!r}")
                        return
                    cat, matched_keyword = "controle_ps5", None

            if cat is None and matched_keyword is None:
                info(f"· [{src_username or 'id'}] ignorado (sem match) → {raw[:80]!r}")
                return

            # preço (opcional)
            price = find_lowest_price(raw)

            # como removemos limites, sempre envia quando há match
            decision, reason = "send", "sem limite"
            info(f"· [{src_username or 'id'}] match → cat={cat} kw={matched_keyword} price={price} decision={decision} ({reason})")

            # monta texto e envia
            alert_text = build_alert_text(src_username, raw, price)

            # 1) Envio preferencial via Bot API (gera notificação)
            sent = False
            if BOT_TOKEN and USER_CHAT_ID:
                sent = send_via_bot_api(BOT_TOKEN, USER_CHAT_ID, alert_text)

            # 2) Fallback/duplicado via Telethon
            if not sent:
                try:
                    await client.send_message(target_entity, alert_text)
                    sent = True
                except Exception as e:
                    warn(f"Falha ao enviar via Telethon TARGET_CHAT: {e}")

            info(f"· envio={'ok' if sent else 'falhou'} → destino={'bot->USER_CHAT_ID' if (BOT_TOKEN and USER_CHAT_ID) else 'TARGET_CHAT'}")

        except FloodWaitError as e:
            warn(f"FloodWait: aguardando {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            warn(f"Erro no handler: {e}")

    await client.start()
    info("▶️ Rodando. Pressione Ctrl+C para sair.")
    await stop_event.wait()
    info("✅ Encerrado.")

if __name__ == "__main__":
    asyncio.run(main())
