# realtime.py
import os
import re
import asyncio
import logging
import json
from typing import List, Optional, Tuple

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import PeerChannel
import aiohttp

# -------------------- LOG --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("monitor")

# -------------------- ENV --------------------
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
STRING_SESSION = os.environ["TELEGRAM_STRING_SESSION"]

BOT_TOKEN = os.environ["TELEGRAM_TOKEN"]
# destino primário: manda o alerta VIA BOT para o seu chat id
USER_CHAT_ID = os.environ.get("USER_CHAT_ID")  # ex.: "1818469361" (numérico)
# opcional: override de canais por env
ENV_CHANNELS = os.environ.get("CHANNELS")

# -------------------- CANAIS --------------------
DEFAULT_CHANNELS = [
    "@TalkPC",
    "@pcdorafa",
    "@PCMakerTopOfertas",
    "@promocoesdolock",
    "@HardTecPromocoes",
    "@canalandrwss",
    "@iuriindica",
    "@dantechofertas",
    "@mpromotech",
    "@mmpromo",
    "@promohypepcgamer",
    "@ofertaskabum",
    "@terabyteshopoficial",
    "@pichauofertas",
    "@sohardwaredorocha",
    "@soplacadevideo",
    "1465877129",  # id cru
]

def parse_channels() -> List[str]:
    if ENV_CHANNELS:
        parts = [p.strip() for p in ENV_CHANNELS.split(",") if p.strip()]
        return parts or DEFAULT_CHANNELS
    return DEFAULT_CHANNELS

# -------------------- NORMALIZAÇÃO --------------------
def normalize(s: str) -> str:
    s = s.replace("\xa0", " ")
    s = s.replace("R$\u00a0", "R$ ")
    return " ".join(s.split())

# -------------------- PREÇO (ROBUSTO) --------------------
# Captura preços tipo:
# R$ 2.029,00 | R$ 179 | 1.269,99 | 629.99 | 2 029
PRICE_RE = re.compile(
    r"""
    (?:
        R\$\s*      # com R$
    )?
    (?<![A-Za-z0-9]) # não colado em letra/dígito à esquerda
    (               # valor
      (?:\d{1,3}(?:[.\s]\d{3})+|\d+)   # milhar opcional
      (?:[.,]\d{2})?                   # centavos opcionais
    )
    (?![A-Za-z])    # não colado em letra à direita
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Ruídos comuns que NÃO são preço (ids de link, querystring etc.)
NO_PRICE_CONTEXT = re.compile(
    r"(?:\b(?:id|sku|pid|p|item|ref|code|cod|utm_[a-z]+)\s*[=:]\s*\d{5,})",
    re.IGNORECASE,
)

def _safe_to_float(token: str) -> Optional[float]:
    # se há ponto E vírgula, decide separador de milhar/decimal
    t = token.strip()
    # números gigantes bizarros
    if len(re.sub(r"\D", "", t)) > 9:
        return None
    # heurística BR: se vírgula existe e é decimal (R$ 1.234,56)
    if "," in t and t.rfind(",") > t.rfind("."):
        t2 = t.replace(".", "").replace(",", ".")
    else:
        # 1.269,99 ou 1269.99 ou 1269
        t2 = t.replace(",", "")
    try:
        v = float(t2)
        if v <= 0.49 or v > 50000:  # limites razoáveis pra varejo
            return None
        return v
    except:
        return None

def extract_best_price(text: str) -> Optional[float]:
    norm = normalize(text)
    # Retira blocos claramente meta (links longos) pra evitar "p=2189916"
    cleaned = re.sub(r"https?://\S+", " ", norm)
    cleaned = re.sub(NO_PRICE_CONTEXT, " ", cleaned)

    candidates = []
    for m in PRICE_RE.finditer(cleaned):
        raw = m.group(1)
        # precisa estar próximo de contexto de preço (linha com R$, "preço", "por:", "pix", "à vista", "no app")
        span = cleaned[max(0, m.start() - 20): m.end() + 20].lower()
        context_ok = any(kw in span for kw in ["r$", "preço", "por", "pix", "à vista", "avista", "no app", "cupom", "12x", "x sem juros", "link"])
        val = _safe_to_float(raw)
        if val and context_ok:
            candidates.append(val)

    if not candidates:
        return None
    # Usa o menor preço válido detectado (geralmente o preço à vista)
    return min(candidates)

# -------------------- HELPERS --------------------
def has_any(n: str, *patterns: str) -> bool:
    n = n.lower()
    return any(p in n for p in patterns)

def regex_any(n: str, pattern: str) -> Optional[re.Match]:
    return re.search(pattern, n, flags=re.IGNORECASE)

def count_fans(n: str) -> int:
    m = re.search(r"(\d+)\s*(?:x\s*)?(?:fans?|ventoinhas?)", n, re.IGNORECASE)
    if not m:
        return 0
    try:
        return int(m.group(1))
    except:
        return 0

def watts_from_text(n: str) -> int:
    m = re.search(r"(\d{3,4})\s*w\b", n, re.IGNORECASE)
    return int(m.group(1)) if m else 0

def has_80plus_grade(n: str) -> Optional[str]:
    if re.search(r"\b80\s*\+?\s*plus\s*gold\b", n, re.IGNORECASE) or re.search(r"\bgold\b", n, re.IGNORECASE):
        return "gold"
    if re.search(r"\b80\s*\+?\s*plus\s*bronze\b", n, re.IGNORECASE) or re.search(r"\bbronze\b", n, re.IGNORECASE):
        return "bronze"
    return None

def is_ps5_console_or_controller(n: str) -> bool:
    # Deve ter "ps5" e (console|controle|dualsense)
    if "ps5" not in n:
        return False
    if re.search(r"\b(console|controle|dualsense)\b", n, re.IGNORECASE):
        return True
    # Bloqueia termos de jogo/acessório
    if re.search(r"\b(jogo|game|mídia|midia|case|capa|suporte|stand|grip)\b", n, re.IGNORECASE):
        return False
    return False

def is_ps5_accessory(n: str) -> bool:
    return re.search(r"\bps5\b", n, re.IGNORECASE) and re.search(r"\b(jogo|mídia|midia|capa|case|suporte|stand)\b", n, re.IGNORECASE)

# -------------------- REGRAS --------------------
INTEL_OK_MB = r"\b(?:h610|b660|b760|z690|z790)\b"
AMD_OK_MB   = r"\b(?:b550|x570)\b"
AMD_BAD_MB  = r"\b(?:a520)\b"

def cpu_is_intel_superior(n: str) -> bool:
    """
    Considera i5-14400F ou superior (i5-14600, i7, i9), e também 12600F/KF (pedido explícito).
    """
    n = n.lower()
    if re.search(r"\bi5[- ]?(14400f|14500|14600|14600k|14600kf)\b", n):
        return True
    if re.search(r"\bi5[- ]?12600(kf|f|k)?\b", n):
        return True
    if re.search(r"\bi7[- ]?\d{4,5}\b", n):
        return True
    if re.search(r"\bi9[- ]?\d{4,5}\b", n):
        return True
    return False

def cpu_is_amd_superior(n: str) -> bool:
    """
    Considera R7 5700X ou superior (5800, 5800X3D, 5900X, etc.) e também R7 5700 (sem X) por pedido.
    """
    n = n.lower()
    if re.search(r"\bryzen\s*7\s*5700x\b", n):
        return True
    if re.search(r"\bryzen\s*7\s*5700\b", n):  # sem X, permitido
        return True
    if re.search(r"\bryzen\s*7\s*(58|59|79)\d{2}(x3d|x)?\b", n):
        return True
    return False

def is_reddragon_keyboard(n: str) -> bool:
    return ("teclado" in n) and ("redragon" in n)

def is_water_cooler(n: str) -> bool:
    return re.search(r"\bwater\s*cooler\b", n, re.IGNORECASE) is not None

def is_gpu(n: str, model_pat: str) -> bool:
    return re.search(model_pat, n, re.IGNORECASE) is not None

def is_ram_ddr4(n: str) -> bool:
    return re.search(r"\bddr4\b", n, re.IGNORECASE) is not None

def is_motherboard(n: str) -> bool:
    return re.search(r"\b(placa[-\s]?m[ãa]e|mother\s*board|mb\s)\b", n, re.IGNORECASE) is not None or \
           re.search(INTEL_OK_MB + "|" + AMD_OK_MB + "|" + AMD_BAD_MB, n, re.IGNORECASE) is not None

def should_alert(text: str) -> Tuple[bool, str]:
    """
    Retorna (alerta?, motivo)
    """
    n = normalize(text).lower()
    price = extract_best_price(text)

    # -------- Bloqueios PS5 acessórios/jogos --------
    if is_ps5_accessory(n):
        return (False, "ps5_acessorio_jogo_ignorado")

    # -------- CPUs (<= R$ 900) --------
    if cpu_is_intel_superior(n) or cpu_is_amd_superior(n):
        if price is not None and price <= 900:
            return (True, "cpu_superior<=900")
        else:
            return (False, "cpu_preco>900_ou_sem_preco")

    # -------- PS5 (apenas console/controle) --------
    if is_ps5_console_or_controller(n):
        # sem limite de preço definido por você; se quiser, coloque <= 3500 console, <= 400 controle
        return (True, "ps5_console_controle")

    # -------- Motherboards --------
    if is_motherboard(n):
        # bloqueia A520 sempre
        if re.search(AMD_BAD_MB, n, re.IGNORECASE):
            return (False, "a520_bloqueada")

        if price is None:
            return (False, "mb_sem_preco")

        # Intel LGA1700
        if re.search(INTEL_OK_MB, n, re.IGNORECASE):
            return (price <= 680, f"mb_intel<=680:{price}")

        # AMD AM4 (R7 5700/5700X)
        if re.search(AMD_OK_MB, n, re.IGNORECASE):
            return (price <= 680, f"mb_amd<=680:{price}")

        # outras chipsets de mb: ignorar
        return (False, "mb_outro_chipset")

    # -------- Teclado Redragon (<= R$ 160) --------
    if is_reddragon_keyboard(n):
        if price is not None and price <= 160:
            return (True, "teclado_redragon<=160")
        return (False, "teclado_redragon_preco_alto_ou_nao_definido")

    # -------- Fonte (>=600W e 80Plus Bronze/Gold) (<= R$ 350) --------
    if "fonte" in n or "psu" in n or "power supply" in n:
        w = watts_from_text(n)
        grade = has_80plus_grade(n)
        if w >= 600 and grade in ("bronze", "gold"):
            if price is not None and price <= 350:
                return (True, f"fonte_{w}w_{grade}_<=350")
            return (False, f"fonte_{w}w_{grade}_preco_alto_ou_nao_def")
        return (False, "fonte_nao_atende_watts_ou_80plus")

    # -------- Water Cooler (<= R$ 200) --------
    if is_water_cooler(n):
        if price is not None and price <= 200:
            return (True, "water_cooler<=200")
        return (False, "water_cooler_preco>200_ou_sem_preco")

    # -------- RAM DDR4 (sem limite específico) --------
    if is_ram_ddr4(n):
        # mantém alerta sempre que identificar DDR4 — você já preferiu isso “sem limite”
        return (True, "ram_ddr4")

    # -------- Gabinete (regras novas) --------
    if "gabinete" in n:
        fc = count_fans(n)
        if price is None:
            return (False, f"gabinete_sem_preco_{fc}fans")

        # Caso 1: sem ou <5 fans e preço <150 => BLOQUEIA
        if (fc < 5) and (price < 150):
            return (False, f"gabinete_bloqueado_{fc}fans_<150")

        # Caso 2: >=5 fans e preço <=230 => ALERTA
        if (fc >= 5) and (price <= 230):
            return (True, f"gabinete_{fc}fans_<=230")

        # outros casos: ignora
        return (False, f"gabinete_sem_match_{fc}fans_{price}")

    # -------- GPUs específicas --------
    # RTX 5060 (sem limite de preço definido por você)
    if is_gpu(n, r"\brtx\s*5060\b"):
        return (True, "gpu_rtx5060")

    # RX 7600
    if is_gpu(n, r"\brx\s*7600\b"):
        return (True, "gpu_rx7600")

    # fallback
    return (False, "sem_match")

# -------------------- ENVIO VIA BOT --------------------
BOT_API = "https://api.telegram.org"

async def bot_send_text(session: aiohttp.ClientSession, chat_id: str, text: str) -> bool:
    # Telegram limita 4096 chars por mensagem
    MAX_LEN = 4096
    chunks = [text[i:i+MAX_LEN] for i in range(0, len(text), MAX_LEN)] or [text]
    ok = True
    for part in chunks:
        url = f"{BOT_API}/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": part, "disable_web_page_preview": False}
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.error(f"Falha ao enviar via bot: HTTP {resp.status} — {body}")
                ok = False
    return ok

# -------------------- MAIN --------------------
async def main():
    channels = parse_channels()
    # Loga quais canais
    resolved = ", ".join(channels)
    log.info(f"▶️ Canais: {resolved}")

    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Sessão inválida — gere uma nova TELEGRAM_STRING_SESSION.")

    log.info(f"✅ Logado — monitorando {len(channels)} canais…")
    log.info("▶️ Rodando. Pressione Ctrl+C para sair.")

    session = aiohttp.ClientSession()

    @client.on(events.NewMessage(chats=channels))
    async def handler(ev: events.newmessage.NewMessage.Event):
        try:
            text = ev.raw_text or ""
            ch = ev.chat if ev.chat else None
            ch_name = getattr(ch, "username", None)
            if not ch_name and hasattr(ch, "title") and ch.title:
                ch_name = ch.title
            src = f"@{ch_name}" if ch_name and not str(ch_name).startswith("@") else (ch_name or "canal")

            alert, reason = should_alert(text)
            if alert:
                # Enviar somente o texto ORIGINAL do grupo, sem prefixos adicionais.
                send_text = text

                where = USER_CHAT_ID or ""
                dest = where if where else "USER_CHAT_ID_NAO_CONFIGURADO"
                ok = await bot_send_text(session, dest, send_text)
                log.info(f"· [ok] envio=bot  motivo={reason} src={src} ok={ok}")
            else:
                # Logs úteis de debug para entender porque caiu fora
                p = extract_best_price(text)
                log.info(f"· [ignorado] {reason} → '{text[:80]}{'…' if len(text)>80 else ''}'  price={p}")
        except Exception as e:
            log.exception(f"Erro no handler: {e}")

    try:
        await client.run_until_disconnected()
    finally:
        await session.close()

if __name__ == "__main__":
    asyncio.run(main())
