import os
import re
import asyncio
import logging
import json
from typing import List, Optional, Tuple, Dict, Any

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User, Channel, Chat

import urllib.parse
import http.client

# ---------------------------------------------------------
# Log
# ---------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("realtime")

# ---------------------------------------------------------
# ENVs
# ---------------------------------------------------------
API_ID = int(os.environ.get("TELEGRAM_API_ID", "0"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()
STRING_SESSION = os.environ.get("TELEGRAM_STRING_SESSION", "").strip()

BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
USER_CHAT_ID = os.environ.get("USER_CHAT_ID", "").strip()

RAW_CHANNELS = os.environ.get("MONITORED_CHANNELS", "").strip()

if not API_ID or not API_HASH or not STRING_SESSION:
    raise RuntimeError("Faltam TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_STRING_SESSION nas ENVs.")

if not BOT_TOKEN or not USER_CHAT_ID:
    raise RuntimeError("Faltam TELEGRAM_TOKEN / USER_CHAT_ID nas ENVs.")

# Sanitiza canais: aceita somente @usernames (sem IDs numéricos soltos)
def _parse_channels(raw: str) -> List[str]:
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    valid = []
    for p in parts:
        # Aceita @username (ou sem @, só letras/números/underscore)
        if p.startswith("@"):
            uname = p
        else:
            # normaliza para @username se vier sem @
            uname = "@" + p

        # filtra óbvios inválidos (IDs, números)
        if re.fullmatch(r"@[\w\d_]{3,64}", uname) and not re.fullmatch(r"@\d+", uname):
            valid.append(uname.lower())
        else:
            log.warning(f"Ignorando entrada inválida em MONITORED_CHANNELS: {p}")
    # remove duplicados preservando ordem
    seen = set()
    out = []
    for v in valid:
        if v not in seen:
            out.append(v)
            seen.add(v)
    return out

MONITORED_CHANNELS: List[str] = _parse_channels(RAW_CHANNELS)

if not MONITORED_CHANNELS:
    log.warning("MONITORED_CHANNELS vazio — nada será filtrado (handler ouvirá tudo, mas filtrará por 0 canais).")
    log.info("▶️ Canais: (nenhum)")
else:
    log.info("▶️ Canais: " + ", ".join(MONITORED_CHANNELS))

# ---------------------------------------------------------
# Bot sender (sem parse_mode)
# ---------------------------------------------------------
def _http_post_json(host: str, path: str, payload: Dict[str, Any]) -> Tuple[int, str]:
    body = json.dumps(payload)
    headers = {
        "Content-Type": "application/json",
        "Connection": "close",
        "User-Agent": "offer-monitor/1.0"
    }
    conn = http.client.HTTPSConnection(host, timeout=15)
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", errors="replace")
        return resp.status, data
    finally:
        conn.close()

def send_via_bot(text: str) -> bool:
    token = BOT_TOKEN
    chat_id = USER_CHAT_ID
    host = "api.telegram.org"
    path = f"/bot{urllib.parse.quote(token)}/sendMessage"

    payload = {
        "chat_id": int(chat_id),
        "text": text,
        # não definir parse_mode para evitar "unsupported parse_mode"
        "disable_web_page_preview": False,
        "disable_notification": False
    }
    status, data = _http_post_json(host, path, payload)
    if status != 200:
        log.error(f"Falha ao enviar via bot ({status}): {data}")
        return False
    try:
        jd = json.loads(data)
        ok = bool(jd.get("ok"))
        if not ok:
            log.error(f"Falha ao enviar via bot (API): {data}")
        return ok
    except Exception:
        log.error(f"Resposta inválida do bot: {data}")
        return False

# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------
CURRENCY_RE = re.compile(
    r"""
    (?:
        (?<!\w)R\$\s*        # prefixo R$
        ([\d\.\s]+,\d{2}|\d+(?:\.\d{3})+|\d+)
    )
    """,
    re.VERBOSE | re.IGNORECASE
)

# também captura "POR: 999", "PREÇO: 199" etc (sem confundir IDs de link)
BARE_PRICE_RE = re.compile(
    r"""
    (?:
        (?:(?:por|preco|preço|valor|pix|vista|à\s*vista)\s*[:=]?\s*)?
        (?:R\$\s*)?
        (\d{1,3}(?:[\.\s]\d{3})*(?:,\d{2})?|\d{2,})(?!\d)  # 99 / 1.299 / 1 299 / 1.299,90
    )
    """,
    re.VERBOSE | re.IGNORECASE
)

def parse_price(text: str) -> Optional[float]:
    """
    Procura primeiro 'R$ ...'. Se não achar, tenta padrões "por/preço/valor ...".
    Converte vírgula decimal e ignora separador de milhar.
    Retorna menor preço plausível encontrado (normalmente o à vista).
    """
    candidates: List[float] = []

    def _to_float(num: str) -> Optional[float]:
        s = num.strip()
        # remove espaços de milhar
        s = re.sub(r"\s", "", s)
        # se houver ambos '.' e ',', assume '.' milhar e ',' decimal
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        else:
            # só ponto → pode ser milhar (1.299) ou decimal (89.99). Se tem mais que 1 ponto, tira pontos.
            if s.count(".") > 1:
                s = s.replace(".", "")
        try:
            val = float(s)
            # ignora valores absurdos (> 100k)
            if 0 < val < 100000:
                return val
        except Exception:
            return None
        return None

    for m in CURRENCY_RE.finditer(text):
        v = _to_float(m.group(1))
        if v is not None:
            candidates.append(v)

    if not candidates:
        # Tenta capturas mais genéricas, mas vamos filtrar para não pegar ID aleatório
        for m in BARE_PRICE_RE.finditer(text):
            raw = m.group(1)
            # ignora números curtos que parecem quantidade (ex: 12X, 3 fans) — mantemos >= 2 dígitos, mas já está no regex
            v = _to_float(raw)
            if v is not None:
                candidates.append(v)

    if not candidates:
        return None

    # Heurística: menor preço costuma ser o à vista
    return min(candidates)

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()

def has_words(text: str, *words: str) -> bool:
    t = norm(text)
    return all(w.lower() in t for w in words)

def any_word(text: str, *words: str) -> bool:
    t = norm(text)
    return any(w.lower() in t for w in words)

def count_fans(text: str) -> Optional[int]:
    """
    Tenta inferir qtde de fans/ventoinhas a partir do texto: '3x', 'kit 5 fans', '4 cooler/fan', etc.
    """
    t = text.lower()
    # padrões tipo "3x120", "5x fan"
    m = re.search(r'(\d+)\s*(?:x|unid|uni|fans?|coolers?|ventoinhas?)', t)
    if m:
        try:
            n = int(m.group(1))
            if 1 <= n <= 12:
                return n
        except Exception:
            pass
    # "kit com 6" ou "com 6 fans"
    m = re.search(r'kit.*?(?:com\s*)?(\d+)\s*(?:fans?|coolers?|ventoinhas?)', t)
    if m:
        try:
            n = int(m.group(1))
            if 1 <= n <= 12:
                return n
        except Exception:
            pass
    return None

# ---------------------------------------------------------
# Filtros de produtos
# ---------------------------------------------------------
CPU_INTEL_ALLOWED_RE = re.compile(
    r'\b(?:i5[-\s]?12600k?f?|i5[-\s]?14400f|i5[-\s]?14\d{3}[k|kf|f]?|i7[-\s]?14\d{3}[k|kf|f]?|i9[-\s]?14\d{3}[k|kf|f]?)\b',
    re.IGNORECASE
)

CPU_AMD_ALLOWED_RE = re.compile(
    r'\b(?:ryzen\s*7\s*(?:5700x?|5800x3d|5800x|5900x|5950x))\b',
    re.IGNORECASE
)

MB_INTEL_RE = re.compile(r'\b(?:h610|b660|b760|z690|z790)\b', re.IGNORECASE)
MB_AMD_OK_RE = re.compile(r'\b(?:b550|x570)\b', re.IGNORECASE)
MB_AMD_BLOCK_RE = re.compile(r'\b(?:a520)\b', re.IGNORECASE)

GPU_OK_RE = re.compile(r'\b(?:rtx\s*5060|rx\s*7600)\b', re.IGNORECASE)

PSU_RE = re.compile(r'\b(?:80\s*\+?\s*plus\s*)?(?:bronze|gold)\b', re.IGNORECASE)
WATT_RE = re.compile(r'(\d{3,4})\s*w', re.IGNORECASE)

REDRAGON_RE = re.compile(r'\bredragon\b', re.IGNORECASE)
KUMARA_RE = re.compile(r'\b(k552|kumara)\b', re.IGNORECASE)

PS5_CONSOLE_RE = re.compile(r'\b(?:ps5|playstation\s*5)\b', re.IGNORECASE)
PS5_BLOCK_RE = re.compile(r'\b(?:capa|cover|case|controle|controller|dualsense|jogo|game|mídia|midia|card|gift)\b', re.IGNORECASE)

ICLAMPER_RE = re.compile(r'\b(?:iclamp(?:er)?|filtro\s*de\s*linha)\b', re.IGNORECASE)
KIT_FANS_RE = re.compile(r'\b(?:kit|conjunto).*(?:fan|cooler|ventoinha)', re.IGNORECASE)

def is_cpu_allowed(text: str, price: Optional[float]) -> bool:
    if price is None:
        return False
    t = text.lower()
    if CPU_INTEL_ALLOWED_RE.search(t):
        return price <= 900.0
    if CPU_AMD_ALLOWED_RE.search(t):
        return price <= 900.0
    return False

def is_mobo_allowed(text: str, price: Optional[float]) -> bool:
    if price is None:
        return False
    t = text.lower()
    if MB_AMD_BLOCK_RE.search(t):
        return False
    if MB_INTEL_RE.search(t):
        return price <= 680.0
    if MB_AMD_OK_RE.search(t):
        return price <= 680.0
    # B550 “qualquer” foi pedido no começo, mas consolidamos com teto 680
    return False

def is_gpu_allowed(text: str, price: Optional[float]) -> bool:
    # Enviar RTX 5060 e RX 7600 independente de preço
    return GPU_OK_RE.search(text) is not None

def is_psu_allowed(text: str, price: Optional[float]) -> bool:
    if price is None:
        return False
    if not PSU_RE.search(text):
        return False
    m = WATT_RE.search(text)
    watts_ok = False
    if m:
        try:
            w = int(m.group(1))
            watts_ok = (w >= 600)
        except Exception:
            watts_ok = False
    return watts_ok and (price <= 350.0)

def is_wc_allowed(text: str, price: Optional[float]) -> bool:
    if price is None:
        return False
    return any_word(text, "water", "cooler") and (price <= 200.0)

def is_case_allowed(text: str, price: Optional[float]) -> bool:
    """
    Regras:
     - Bloqueia gabinetes "sem fans" OU "< 5 fans" se preço < 150.
     - Alerta se possui >= 5 fans E preço <= 230.
     - Demais, ignora.
    """
    t = text.lower()
    if not any_word(t, "gabinete", "case", "cabinet"):
        return False

    n = count_fans(t)  # pode ser None
    if price is None:
        return False

    # pista textual "sem fan"
    no_fans = "sem fan" in t or "sem cooler" in t or "sem ventoinha" in t

    if (no_fans or (n is not None and n < 5)) and (price < 150.0):
        return False  # bloqueia

    if (n is not None and n >= 5) and (price <= 230.0):
        return True  # alerta

    return False

def is_fans_kit(text: str) -> bool:
    # kits de 3 a 9 fans
    if KIT_FANS_RE.search(text) or any_word(text, "kit", "fans", "coolers", "ventoinhas"):
        n = count_fans(text)
        if n is not None and (3 <= n <= 9):
            return True
    return False

def is_iclamper(text: str) -> bool:
    return ICLAMPER_RE.search(text) is not None

def is_redragon_kb(text: str, price: Optional[float]) -> bool:
    if price is None:
        return False
    if not REDRAGON_RE.search(text):
        return False
    # Qualquer Redragon até 160 (independe de ser Kumara/K552)
    return price <= 160.0

def is_ps5_console(text: str) -> bool:
    return PS5_CONSOLE_RE.search(text) is not None and not PS5_BLOCK_RE.search(text)

def decide(text: str) -> Tuple[bool, str, Optional[float]]:
    """
    Retorna (enviar?, motivo, preço_detectado)
    """
    price = parse_price(text)

    # Ordem de checagem por prioridade
    if is_cpu_allowed(text, price):
        return True, f"CPU <= 900 (R$ {price:.2f})", price
    if is_mobo_allowed(text, price):
        return True, f"MOBO <= 680 (R$ {price:.2f})", price
    if is_gpu_allowed(text, price):
        return True, "GPU match (RTX 5060 / RX 7600)", price
    if is_psu_allowed(text, price):
        return True, f"PSU Bronze/Gold >=600W <=350 (R$ {price:.2f})", price
    if is_wc_allowed(text, price):
        return True, f"Water Cooler <= 200 (R$ {price:.2f})", price
    if is_case_allowed(text, price):
        return True, f"Gabinete ≥5 fans e ≤230 (R$ {price:.2f})", price
    if is_fans_kit(text):
        return True, "Kit de fans (3 a 9 unid.)", price
    if is_iclamper(text):
        return True, "Filtro de linha iCLAMPER", price
    if is_ps5_console(text):
        return True, "PS5 console", price

    # Não enviar
    return False, "sem match", price

# ---------------------------------------------------------
# Main
# ---------------------------------------------------------
client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

async def resolve_channels(client: TelegramClient, wanted: List[str]) -> List[Any]:
    """
    Constrói lista de entidades a partir de @usernames.
    Usa get_dialogs para aquecer cache e resolver corretamente.
    """
    await client.get_dialogs()  # aquece cache
    resolved = []
    missing = []
    for uname in wanted:
        try:
            ent = await client.get_entity(uname)
            resolved.append(ent)
        except Exception:
            missing.append(uname)
    if missing:
        log.warning("Não foi possível resolver alguns canais: " + ", ".join(missing))
    return resolved

def render_message(channel_username: str, raw_text: str) -> str:
    """
    Envia a oferta 'como está' + uma linha de contexto do canal.
    (Sem 'Preço encontrado', sem parse_mode)
    """
    header = f"🔥 Alerta em {channel_username}\n\n"
    return f"{header}{raw_text.strip()}"

async def main():
    async with client:
        log.info("Conectado.")
        # Resolve canais
        chats_to_watch = []
        if MONITORED_CHANNELS:
            entities = await resolve_channels(client, MONITORED_CHANNELS)
            chats_to_watch = entities
            log.info("▶️ Canais resolvidos: " + ", ".join(
                [f"@{e.username}" for e in entities if isinstance(e, Channel) and e.username] +
                [str(getattr(e, 'title', '')) for e in entities if not (isinstance(e, Channel) and e.username)]
            ))
        else:
            log.info("▶️ Canais resolvidos: ")

        @client.on(events.NewMessage(chats=chats_to_watch if chats_to_watch else None))
        async def handler(event: events.NewMessage.Event):
            try:
                chat = await event.get_chat()
                if isinstance(chat, (Channel, Chat, User)):
                    uname = f"@{getattr(chat, 'username', None)}" if getattr(chat, 'username', None) else getattr(chat, 'title', 'desconhecido')
                else:
                    uname = "desconhecido"

                text = event.raw_text or ""
                send, reason, price = decide(text)

                pr_log = f"price={price:.2f}" if isinstance(price, (int, float)) and price is not None else "price=None"
                log.info(f"· [{uname:<20}] {'match' if send else 'ignorado (sem match)'} → {pr_log} reason={reason}")

                if send:
                    out = render_message(uname, text)
                    ok = send_via_bot(out)
                    if ok:
                        log.info("· envio=ok → destino=bot")
                    else:
                        log.error("· envio=erro → destino=bot")
            except Exception as e:
                log.exception(f"Erro no handler: {e}")

        log.info("▶️ Rodando. Pressione Ctrl+C para sair.")
        await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("✅ Encerrado.")
