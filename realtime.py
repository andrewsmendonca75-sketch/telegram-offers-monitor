# realtime.py
import os
import sys
import json
import re
import asyncio
import signal
import fcntl
import unicodedata
from typing import List, Dict, Any, Optional, Tuple

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

# Envio primário: Bot API (notificação para você)
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
USER_CHAT_ID = os.getenv("USER_CHAT_ID", "")  # ex.: 1818469361

# Opcional: enviar também para um chat/canal via Telethon (como você)
TARGET_CHAT = os.getenv("TARGET_CHAT", "me")  # "me" = Mensagens Salvas
ALSO_SEND_TO_TARGET = os.getenv("ALSO_SEND_TO_TARGET", "0") == "1"

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
    return cfg

# =========================
# NORMALIZA TEXTO
# =========================
def normalize_text(s: str) -> str:
    s = unicodedata.normalize('NFKC', s)
    s = s.replace('\u200b', '').replace('\u00A0', ' ')
    s = re.sub(r'\s+', ' ', s).strip().lower()
    s = unicodedata.normalize('NFD', s)
    s = ''.join(ch for ch in s if unicodedata.category(ch) != 'Mn')
    return s

# =========================
# PREÇOS — regex e utilitários
# =========================
PRICE_CORE = r'(?:\d{1,3}(?:[.\s]\d{3})+|\d+)(?:[.,]\d{2})?'
PRICE_RE = re.compile(
    rf'(?:r\$\s*)?({PRICE_CORE})(?!\s*(?:gb|tb|mhz|ghz|mm|cm))\b',
    re.I
)
URL_RE = re.compile(r'https?://\S+', re.I)

MODEL_NEAR_RE = re.compile(
    r'(rtx|gtx|rx|ryzen|i3|i5|i7|i9|b\d{3}|z\d{3}|h\d{3})',
    re.I
)

def strip_urls(s: str) -> str:
    return URL_RE.sub(' ', s)

def _has_currency_prefix(text: str, start_idx: int) -> bool:
    # olha até 4 chars pra trás do início do NÚMERO por "r$"
    look = text[max(0, start_idx-4):start_idx].lower().replace(' ', '')
    return 'r$' in look

def _is_adjacent_to_letter(text: str, start: int, end: int) -> bool:
    prev = text[start-1] if start > 0 else ''
    nxt  = text[end] if end < len(text) else ''
    return (prev.isalpha() or nxt.isalpha())

def _is_installments(text: str, end: int) -> bool:
    # detecta "12x", "10 x", "em 12x"
    tail = text[end:end+6].lower()
    return bool(re.match(r'\s*(em\s*)?\d{1,2}\s*x\b', tail))

def _is_near_coupon(text: str, start: int) -> bool:
    before = text[max(0, start-12):start].lower()
    return 'cupom' in before

def _is_model_number(text: str, start: int, end: int) -> bool:
    # Se há token de modelo perto do número (rtx/rx/i5/ryzen/b550/h610 etc.), é especificação, não preço
    L = max(0, start - 8)
    R = min(len(text), end + 8)
    window = text[L:R]
    return MODEL_NEAR_RE.search(window) is not None

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

def extract_prices(text: str) -> List[Tuple[float, bool]]:
    """
    Retorna lista de tuplas (valor, has_currency_prefix) válidas no texto.
    - remove URLs antes de extrair preços (evita números tipo ?p=2189916)
    - ignora números colados em letras (AGORA15)
    - ignora parcelas (12x)
    - ignora números próximos de 'cupom'
    - ignora valores muito baixos sem 'R$'
    - ignora valores absurdos sem 'R$'
    - ignora números que parecem MODELO (rtx/rx/i5/ryzen/b550/h610) quando não há 'R$'
    """
    scan_text = strip_urls(text)
    out: List[Tuple[float, bool]] = []
    for m in PRICE_RE.finditer(scan_text):
        start, end = m.span(1)  # GRUPO DO NÚMERO
        raw_num = m.group(1)

        if _is_adjacent_to_letter(scan_text, start, end):
            continue
        if _is_installments(scan_text, end):
            continue
        if _is_near_coupon(scan_text, start):
            continue

        val = parse_brl_to_float(raw_num)
        if val is None:
            continue

        has_curr = _has_currency_prefix(scan_text, start)

        if not has_curr and val < 50:
            continue
        if not has_curr and val > 100000:
            continue
        if not has_curr and _is_model_number(scan_text, start, end):
            continue

        out.append((val, has_curr))
    return out

def choose_best_price(text: str) -> Optional[float]:
    """
    1) Se houver preços com "R$", retorna o MENOR (à vista).
    2) Senão, retorna o MAIOR válido (evita 12/15/39).
    """
    prices = extract_prices(text)
    if not prices:
        return None
    with_currency = [v for v, has in prices if has]
    if with_currency:
        return min(with_currency)
    return max(v for v, _ in prices)

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

    # CPUs AMD
    "ryzen 7 5700x": re.compile(r'\bryzen\s*7\s*5700x\b', re.I),
    "ryzen 7 5700":  re.compile(r'\bryzen\s*7\s*5700\b', re.I),

    # CPUs Intel
    "i5 14400f":  re.compile(r'\bi[-\s]*5[-\s]*14400f\b', re.I),
    "i5 13400f":  re.compile(r'\bi[-\s]*5[-\s]*13400f\b', re.I),
    "i5 12400f":  re.compile(r'\bi[-\s]*5[-\s]*12400f\b', re.I),
    "i5 14600kf": re.compile(r'\bi[-\s]*5[-\s]*14600kf\b', re.I),
    "i5 14600k":  re.compile(r'\bi[-\s]*5[-\s]*14600k\b', re.I),

    # Fontes
    "fonte 650w":   re.compile(r'\b(fonte|psu)\b.*\b650\s*w\b|\b650\s*w\b.*\b(fonte|psu)\b', re.I),
    "fonte 600w":   re.compile(r'\b(fonte|psu)\b.*\b600\s*w\b|\b600\s*w\b.*\b(fonte|psu)\b', re.I),

    # Placas-mãe
    "placa mae b550": re.compile(
        r'\b(placa\s*ma[e]|motherboard|mobo)\b.*\b(b550m?)\b|\b(b550m?)\b.*\b(placa\s*ma[e]|motherboard|mobo)\b',
        re.I
    ),
    "b550": re.compile(r'\bb550m?\b', re.I),

    "placa mae h610m": re.compile(
        r'\b(placa\s*ma[e]|motherboard|mobo)\b.*\b(h610m?)\b|\b(h610m?)\b.*\b(placa\s*ma[e]|motherboard|mobo)\b',
        re.I
    ),
    "h610m": re.compile(r'\bh610m?\b', re.I),

    # Filtro de Linha iCLAMPER
    "filtro_linha_iclamper": re.compile(
        r'\bfiltro\s*de\s*linha\b.*\b(i\s*clamper|iclamp(er)?)\b|\b(i\s*clamper|iclamp(er)?)\b.*\bfiltro\s*de\s*linha\b',
        re.I
    ),

    # Kit de fans/ventoinhas (3–9 unidades)
    "kit_fans_3_9": re.compile(
        r'('
        r'(?:\b(kit|combo)\b.*\b(fans?|ventoinhas?)\b.*\b[3-9]\b)'
        r'|(?:\b[3-9]\b.*\b(kit|combo)\b.*\b(fans?|ventoinhas?)\b)'
        r'|(?:\b(fans?|ventoinhas?)\b.*\b[3-9]\s*(?:em\s*1|un|unid|pcs?|p[cç]s|pecas|pe[cç]as)\b)'
        r'|(?:\b[3-9]\s*(?:em\s*1|un|unid|pcs?|p[cç]s|pecas|pe[cç]as)\b.*\b(fans?|ventoinhas?)\b)'
        r')',
        re.I
    ),

    # Gabinete (qualquer menção)
    "gabinete": re.compile(r'\bgabinet(e|es|e\s*gamer|es\s*gamer)?\b', re.I),
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
    # Envia exatamente como veio do canal (sem cabeçalho/“Preço encontrado”)
    return text

def send_via_bot_api(token: str, chat_id: str, text: str) -> bool:
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            # sem parse_mode e sem disable_web_page_preview — mantém igual ao original
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

            # 1) classificador fino
            cat = classify_product(raw)

            # 2) padrões robustos
            matched_keyword = None
            if cat is None:
                for key, rx in KEYWORD_PATTERNS.items():
                    if rx.search(t):
                        matched_keyword = key
                        break

            # 3) fallback: keywords simples do config
            if cat is None and matched_keyword is None:
                for k in kw_set:
                    if k in t:
                        matched_keyword = k
                        break

            # Anti-falso-positivo PS5 genérico e controle
            if cat is None and matched_keyword in {"ps5", "controle ps5", "dualsense"}:
                if matched_keyword == "ps5":
                    if not (PS5_CONSOLE.search(t) and PS5_CONSOLE_HINT.search(t)) or NEG_PS5.search(t):
                        info(f"· [{src_username or 'id'}] ignorado (ps5 não é console válido) → {raw[:80]!r}")
                        return
                    cat, matched_keyword = "ps5_console", None
                else:
                    if not DUALSENSE.search(t) or NEG_DUALSENSE.search(t):
                        info(f"· [{src_username or 'id'}] ignorado (acessório/cover de controle) → {raw[:80]!r}")
                        return
                    cat, matched_keyword = "controle_ps5", None

            if cat is None and matched_keyword is None:
                info(f"· [{src_username or 'id'}] ignorado (sem match) → {raw[:80]!r}")
                return

            # PREÇO (não mostramos no alerta, mas usamos para regra do gabinete ≤ 200)
            price = choose_best_price(raw)

            # Regra específica: GABINETE só se preço ≤ 200
            if (matched_keyword == "gabinete" or cat == "gabinete"):
                if price is None or price > 200:
                    info(f"· [{src_username or 'id'}] ignorado (gabinete acima de 200 ou sem preço) → {raw[:80]!r}")
                    return

            # envia
            info(f"· [{src_username or 'id'}] match → cat={cat} kw={matched_keyword} price={price} decision=send")
            alert_text = build_alert_text(src_username, raw, price)

            sent_bot = False
            if BOT_TOKEN and USER_CHAT_ID:
                sent_bot = send_via_bot_api(BOT_TOKEN, USER_CHAT_ID, alert_text)

            sent_target = False
            if ALSO_SEND_TO_TARGET:
                try:
                    await client.send_message(target_entity, alert_text)
                    sent_target = True
                except Exception as e:
                    warn(f"Falha ao enviar também para TARGET_CHAT: {e}")

            if not sent_bot and not sent_target:
                # fallback final se nada foi
                try:
                    await client.send_message(target_entity, alert_text)
                    sent_target = True
                except Exception as e:
                    warn(f"Falha no fallback TARGET_CHAT: {e}")

            info("· envio=ok → destino=" +
                 ("bot " if sent_bot else "") +
                 ("+ target" if sent_target else ""))

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
