import os
import re
import json
import asyncio
from typing import Optional, Tuple, List
import logging
import html
import time

import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# -----------------------------------------------------------------------------
# Config básica
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
STRING_SESSION = os.environ["TELEGRAM_STRING_SESSION"]

BOT_TOKEN = os.environ["TELEGRAM_TOKEN"]               # Bot que enviará pra você
USER_CHAT_ID = os.environ["USER_CHAT_ID"]              # Para quem o bot vai enviar (ex.: seu ID numérico)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.json")  # lista de canais no config.json

# -----------------------------------------------------------------------------
# Utilitários
# -----------------------------------------------------------------------------
def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def send_via_bot(text: str) -> bool:
    """Envia a mensagem 'como se fosse' o bot falando com você (sem cabeçalho).
    Mantém exatamente o texto do post do canal."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": USER_CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
        "parse_mode": None,  # não forçar parse; manda cru, preservando o que o canal escreveu
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        ok = r.status_code == 200 and r.json().get("ok", False)
        if not ok:
            logging.error("Falha ao enviar via bot: %s", r.text)
        return ok
    except Exception as e:
        logging.exception("Erro ao enviar via bot")
        return False

def normalize_text(s: str) -> str:
    s = s.replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip().lower()

def match_any(pattern: re.Pattern, text: str) -> bool:
    return bool(pattern.search(text))

# -----------------------------------------------------------------------------
# Regex de preço – EXIGE PRESENÇA DE “R$”
# Aceita variações comuns: "R$ 2.029", "por R$ 2.029,90", "no pix R$ 699"
# -----------------------------------------------------------------------------
CURRENCY_PRICE = re.compile(
    r"(?:^|[\s:>])(?:por\s*)?(?:no\s*pix\s*)?(?:à\s*vista\s*)?R\$\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)",
    re.I
)

def extract_price_brl(text: str) -> Optional[float]:
    """
    Retorna o primeiro preço válido com "R$".
    Não pega números soltos (URLs, IDs, cupons), pois exige o prefixo R$.
    """
    m = CURRENCY_PRICE.search(text)
    if not m:
        return None
    raw = m.group(1)
    # normaliza '2.029,99' -> '2029.99'
    raw = raw.replace('.', '').replace(',', '.')
    try:
        return float(raw)
    except Exception:
        return None

# -----------------------------------------------------------------------------
# Palavras-chave e filtros
# -----------------------------------------------------------------------------

# CPUs (limite: <= R$ 900, somente 14400F+ e 5700/5700X+)
CPU_I5_ALLOWED = re.compile(
    r"\b(i5[-\s]*1(?:4(?:400f|4400f)|3(?:600|500)|2(?:600f|600kf|600k|600)))\b",  # inclui 12600F/KF/K, 13400/13600, 14400F
    re.I
)
CPU_RYZEN7_ALLOWED = re.compile(
    r"\b(ryzen\s*7\s*(?:5700x?|5800x3d|5800x|5900x|5950x|57[0-9]{2}x?))\b",        # 5700 / 5700X e superiores AM4
    re.I
)

# Motherboards:
# Intel LGA1700 alvo (<= R$ 680)
MB_INTEL_OK = re.compile(r"\b(h610|b660|b760|z690|z790)(?:m|\b)", re.I)

# AMD AM4 alvo (<= R$ 680) – apenas B550 e X570 (servem ao 5700/5700X)
MB_AMD_OK = re.compile(r"\b(b550|x570)(?:m|\b)", re.I)

# Bloqueios AMD fora do escopo (AM4 de entrada e AM5):
MB_AMD_BLOCK = re.compile(
    r"\b(a520|a320|a620|b650|x670|x670e|x870|x870e)(?:m|\b)", re.I
)

# PS5 – só console/controle (evitar jogos e capas)
PS5_CONSOLE_OR_CONTROLLER = re.compile(
    r"\b(ps5)\b.*\b(console|edi(?:ç|c)ão\s*digital|bundle|dual\s*sense|dualsense|controle)\b|\b(dual\s*sense|dualsense)\b",
    re.I
)
PS5_EXCLUDE = re.compile(
    r"\b(jogo|mídia|mariachi|capa|case|pele|skin|suporte|dock|grip|capa\s+silicone|pel[ií]cula)\b",
    re.I
)

# RAM DDR4:
RAM_DDR4 = re.compile(r"\b(mem[oó]ria|ram)\b.*\bddr4\b|\bddr4\b", re.I)

# GPUs target livre (sem teto): RTX 5060 e RX 7600
GPU_TARGET = re.compile(r"\b(rtx\s*5060|rx\s*7600)\b", re.I)

# Redragon teclados – “superiores ao Kumara” (limite <= R$ 160).
# Heurística: evitar “Kumara”. Se citar “Redragon” + teclado e NÃO tiver “Kumara”, entra.
REDRAGON_KEYBOARD = re.compile(r"\bredragon\b.*\b(teclad[oa]|keyboard)\b", re.I)
REDRAGON_EXCLUDE_KUMARA = re.compile(r"\b(kumara|k552)\b", re.I)

# Fonte (PSU): apenas 600W+ Bronze/Gold, <= R$ 350
PSU_WATTAGE_600_PLUS = re.compile(r"\b(6[0-9]{2}|[7-9][0-9]{2}|1[0-9]{3,})\s*W\b", re.I)
PSU_BRONZE_GOLD = re.compile(r"\b(80\s*\+?\s*plus\s*)?(bronze|gold)\b", re.I)

# Water cooler: <= R$ 200
WATER_COOLER = re.compile(r"\bwater\s*cooler\b|\bwc\b", re.I)

# Gabinete: <= R$ 200
GABINETE = re.compile(r"\bgabinete\b|\bcase\b", re.I)

# Filtro de linha iCLAMPER (sem teto pedido)
ICLAMPER = re.compile(r"\bi-?clamper\b|\biclamp\b|\bclamp(?:er)?\b", re.I)

# Kit de ventoinhas: conter “kit” e quantidade 3–9 + “fan/ventoinha”
FAN_KIT = re.compile(
    r"\bkit\b.*\b([3-9])\s*(?:x|unid(?:ade|.)?|pe[çc]as?)?\b.*\b(fans?|ventoinhas?)\b|\b(fans?|ventoinhas?)\b.*\bkit\b.*\b([3-9])\b",
    re.I
)

# -----------------------------------------------------------------------------
# Decisão
# -----------------------------------------------------------------------------
def should_alert(text: str) -> Tuple[bool, str]:
    """
    Retorna (decidir_enviar, motivo_str).
    A mensagem será enviada exatamente como veio do canal (sem cabeçalho).
    """
    norm = normalize_text(text)
    price = extract_price_brl(text)

    # ======= CPU (limite <= R$900, somente linhas pedidas/superiores) =======
    if match_any(CPU_I5_ALLOWED, norm) or match_any(CPU_RYZEN7_ALLOWED, norm):
        if price is not None and price <= 900:
            return True, "cpu<=900"
        return False, "cpu_price_missing_or_over"

    # ======= Placas-mãe =======
    # Bloqueia AMD fora do escopo (A520 etc / AM5)
    if match_any(MB_AMD_BLOCK, norm):
        return False, "mb_block_amd"

    # Intel OK (H610/B660/B760/Z690/Z790) – requer preço <= 680
    if match_any(MB_INTEL_OK, norm):
        if price is not None and price <= 680:
            return True, "mb_intel<=680"
        return False, "mb_intel_price_missing_or_over"

    # AMD AM4 OK (B550/X570) – requer preço <= 680
    if match_any(MB_AMD_OK, norm):
        if price is not None and price <= 680:
            return True, "mb_amd<=680"
        return False, "mb_amd_price_missing_or_over"

    # ======= PS5 (somente console/controle) =======
    if match_any(PS5_CONSOLE_OR_CONTROLLER, norm) and not match_any(PS5_EXCLUDE, norm):
        # sem teto especificado
        return True, "ps5_console_or_controller"

    # ======= RAM DDR4 (sem teto) =======
    if match_any(RAM_DDR4, norm):
        return True, "ram_ddr4"

    # ======= GPUs alvo (sem teto): RTX 5060 / RX 7600 =======
    if match_any(GPU_TARGET, norm):
        return True, "gpu_target"

    # ======= Teclados Redragon superiores ao Kumara (<= R$160) =======
    if match_any(REDRAGON_KEYBOARD, norm) and not match_any(REDRAGON_EXCLUDE_KUMARA, norm):
        if price is not None and price <= 160:
            return True, "redragon_keyboard<=160"
        return False, "redragon_keyboard_price_missing_or_over"

    # ======= Fonte 600W+ Bronze/Gold (<= R$350) =======
    if re.search(r"\bfonte\b|\bpsu\b|power\s*supply", norm, re.I):
        if match_any(PSU_WATTAGE_600_PLUS, norm) and match_any(PSU_BRONZE_GOLD, norm):
            if price is not None and price <= 350:
                return True, "psu_600w_bronze_gold<=350"
            return False, "psu_price_missing_or_over"
        # não cumpre selo/wattagem
        return False, "psu_not_meeting_specs"

    # ======= Water Cooler (<= R$200) =======
    if match_any(WATER_COOLER, norm):
        if price is not None and price <= 200:
            return True, "water_cooler<=200"
        return False, "water_cooler_price_missing_or_over"

    # ======= Gabinete (<= R$200) =======
    if match_any(GABINETE, norm):
        if price is not None and price <= 200:
            return True, "gabinete<=200"
        return False, "gabinete_price_missing_or_over"

    # ======= Filtro iCLAMPER (sem teto) =======
    if match_any(ICLAMPER, norm):
        return True, "iclamp"

    # ======= Kit de ventoinhas 3–9 (sem teto) =======
    if match_any(FAN_KIT, norm):
        return True, "fan_kit"

    # Nada relevante
    return False, "no_match"

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
async def main():
    cfg = load_config(CONFIG_PATH)  # {"channels": ["@canal1", ...]}
    channels = cfg.get("channels") or []
    if not channels:
        raise RuntimeError("Nenhum canal definido em config.json (chave 'channels').")

    # Telethon client com StringSession
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

    # Conecta
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("String session inválida ou expirada. Gere novamente TELEGRAM_STRING_SESSION.")

    # Resolve IDs dos canais
    resolved = []
    for ch in channels:
        try:
            entity = await client.get_entity(ch)
            name = getattr(entity, "username", None)
            if name:
                resolved.append(f"@{name}")
            else:
                resolved.append(f"id:{entity.id}")
        except Exception:
            resolved.append(str(ch))

    logging.info("▶️ Canais resolvidos: %s", ", ".join(resolved))
    logging.info("✅ Logado — monitorando %d canais…", len(channels))
    print("▶️ Rodando. Pressione Ctrl+C para sair.", flush=True)

    @client.on(events.NewMessage(chats=channels))
    async def handler(event):
        try:
            text = event.raw_text or ""
            if not text.strip():
                return
            decide, reason = should_alert(text)
            if decide:
                ok = send_via_bot(text)  # envia o texto “cru”, sem cabeçalho
                logging.info("· [%-20s] match → reason=%s price=%s decision=send",
                             getattr(event.chat, 'username', 'chat'),
                             reason,
                             extract_price_brl(text))
                logging.info("· envio=%s → destino=bot", "ok" if ok else "falha")
            else:
                logging.info("· [%-20s] ignorado (%s) → %r",
                             getattr(event.chat, 'username', 'chat'),
                             reason, text[:120].replace("\n", "\\n"))
        except Exception as e:
            logging.exception("Erro no handler")

    # loop
    try:
        await client.run_until_disconnected()
    finally:
        logging.info("Recebi sinal — desconectando...")
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
