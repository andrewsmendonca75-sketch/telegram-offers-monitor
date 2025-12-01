# test_classify_ready.py
# -*- coding: utf-8 -*-
"""
Autocontained test harness for classifier.
Run: python3 test_classify_ready.py
"""

import re
from typing import Optional, List, Tuple

# -------------------------
# Price parser (robust)
# -------------------------
PRICE_PIX_RE = re.compile(
    r"(?i)r\$\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{1,2})?)\s*(?:no\s*pix|à\s*vista|a\s*vista|à\s*vista:|avista)",
    re.I
)
PRICE_FALLBACK_RE = re.compile(r"(?i)r\$\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{1,2})?)", re.I)

# words that indicate the number is NOT a product price (coupons / discounts / parcel)
NOT_PRICE_WORDS = re.compile(
    r"(?i)\b(cupom|cupom:|off|desconto|promo|promoção|promoção:|parcelas?|parcelado|parcelamento|x\s*de|frete|cashback|pontos?|reembolso|resgate|oferta.*cupom)\b",
    re.I
)

URL_RE = re.compile(r"https?://\S+", re.I)

def _to_float_brl(raw: str) -> Optional[float]:
    s = raw.strip().replace(".", "").replace(",", ".")
    try:
        v = float(s)
        # sensible limits
        if v <= 0 or v < 0.5 or v > 5_000_000:
            return None
        return v
    except Exception:
        return None

def find_lowest_price(text: str) -> Optional[float]:
    if not text:
        return None
    txt = URL_RE.sub(" ", text)  # remove urls (they often contain irrelevant prices)
    vals: List[float] = []
    # prefer explicit à vista / pix
    for m in PRICE_PIX_RE.finditer(txt):
        start = max(0, m.start() - 80); end = min(len(txt), m.end() + 80)
        ctx = txt[start:end]
        if NOT_PRICE_WORDS.search(ctx):
            continue
        v = _to_float_brl(m.group(1))
        if v:
            vals.append(v)
    if not vals:
        for m in PRICE_FALLBACK_RE.finditer(txt):
            start = max(0, m.start() - 80); end = min(len(txt), m.end() + 80)
            ctx = txt[start:end]
            if NOT_PRICE_WORDS.search(ctx):
                continue
            v = _to_float_brl(m.group(1))
            if v:
                vals.append(v)
    return min(vals) if vals else None

# -------------------------
# Regex rules (refined)
# -------------------------
BLOCK_CATS = re.compile(
    r"\b(celular|smartphone|iphone|android|notebook|laptop|macbook|geladeira|refrigerador|m[aá]quina\s*de\s*lavar|lavadora|lava\s*e\s*seca)\b",
    re.I
)
PC_GAMER_RE = re.compile(r"\b(pc\s*gamer|setup\s*completo|kit\s*completo)\b", re.I)

# TV Box: specifically boxes (mi box, xiaomi box, tv box, android tv box) - NOT 'Google TV' alone
TVBOX_RE = re.compile(r"\b(?:tv\s*box|xiaomi\s*box|mi\s*box|mi-box|mi\s+box|android\s*tv\s*box)\b", re.I)

# TV: generic mentions of TV / Smart TV / Televisão
TV_RE = re.compile(r"\b(?:tv|smart\s*tv|televis(?:ão|ao))\b", re.I)

# Monitors
MONITOR_SMALL_RE = re.compile(r"\b(19|20|21|22|23|24|25|26)\s*(?:\"|\'|pol|polegadas?)\b|\bmonitor\b.*\b(19|20|21|22|23|24|25|26)\b", re.I)
MONITOR_RE = re.compile(r"\bmonitor\b", re.I)
MONITOR_SIZE_RE = re.compile(r"\b(27|28|29|30|31|32|34|35|38|40|42|43|45|48|49|50|55)\s*(?:\"|\'|pol|polegadas?)\b", re.I)
MONITOR_144HZ_RE = re.compile(r"\b(14[4-9]|1[5-9]\d|[2-9]\d{2})\s*hz\b", re.I)
MONITOR_LG_27_RE = re.compile(r"\b27gs60f\b|(?=.*\blg\b)(?=.*\bultragear\b)(?=.*\b27\s*(?:\"|')?)(?=.*\b180\s*hz\b)(?=.*\b(?:fhd|full\s*hd)\b)", re.I)

# Mobos
A520_RE = re.compile(r"\ba520m?\b", re.I)
H610_RE = re.compile(r"\bh610m?\b", re.I)
LGA1700_RE = re.compile(r"\b(?:b660m?|b760m?|z690|z790)\b", re.I)
SPECIFIC_B760M_RE = re.compile(r"\bb760m\b", re.I)

# SSD
SSD_RE  = re.compile(r"\bssd\b.*\bkingston\b|\bkingston\b.*\bssd\b", re.I)
M2_RE   = re.compile(r"\bm\.?2\b|\bnvme\b", re.I)
TB1_RE  = re.compile(r"\b1\s*tb\b", re.I)

# RAM
RAM_16GB_3200_RE = re.compile(r"\b(?:ddr4)\b.*\b16\s*gb\b.*\b3200\b|\b16\s*gb\b.*\b(?:ddr4)\b.*\b3200\b", re.I)

# GPUs / CPUs (kept similar)
RTX5060_3FAN_RE = re.compile(r"\brtx\s*5060(?!\s*ti)\b.*\b(3|triple)\b.*\b(fan|fans)\b|\btriple\s*fan\b.*\brtx\s*5060\b", re.I)
RTX5060_2FAN_RE = re.compile(r"\brtx\s*5060(?!\s*ti)\b.*\b(2|dual)\b.*\b(fan|fans)\b|\bdual\s*fan\b.*\brtx\s*5060\b", re.I)
RTX5060TI_RE = re.compile(r"\brtx\s*5060\s*ti\b", re.I)
RTX5060_RE = re.compile(r"\brtx\s*5060(?!\s*ti)\b", re.I)
RTX5070_FAM  = re.compile(r"\brtx\s*5070(\s*ti)?\b", re.I)

RYZEN_7_5700X_RE = re.compile(r"\bryzen\s*7\s*5700x\b", re.I)
I5_14400F_RE = re.compile(r"\bi5[-\s]*14400f\b", re.I)
INTEL_SUP = re.compile(r"\b(i5[-\s]*14[4-9]\d{2}[kf]*|i5[-\s]*145\d{2}[kf]*|i7[-\s]*14\d{3}[kf]*|i9[-\s]*14\d{3}[kf]*)\b", re.I)
AMD_SUP   = re.compile(r"\b(ryzen\s*7\s*5700x[3d]*|ryzen\s*7\s*5800x[3d]*|ryzen\s*9\s*5900x|ryzen\s*9\s*5950x)\b", re.I)
AMD_BLOCK = re.compile(r"\b(ryzen\s*(?:3|5)\s|5600g?t?|5500|5700(?!x))\b", re.I)

# Other
WATER_240MM_ARGB_RE = re.compile(r"\bwater\s*cooler\b.*\b240\s*mm\b.*\bargb\b", re.I)
CADEIRA_RE = re.compile(r"\bcadeira\b", re.I)
DUALSENSE_RE = re.compile(r"\b(dualsense|controle\s*ps5|controle\s*playstation\s*5)\b", re.I)
AR_INVERTER_RE = re.compile(r"\bar\s*condicionado\b.*\binverter\b|\binverter\b.*\bar\s*condicionado\b", re.I)
KINDLE_RE = re.compile(r"\bkindle\b", re.I)
CAFETEIRA_PROG_RE = re.compile(r"\bcafeteira\b.*\bprogr[aá]m[aá]vel\b", re.I)
TENIS_NIKE_RE = re.compile(r"\b(tênis|tenis)\s*(nike|air\s*max|air\s*force|jordan)\b", re.I)
WEBCAM_4K_RE = re.compile(r"\bwebcam\b.*\b4k\b", re.I)

# -------------------------
# Classifier - uniform return
# -------------------------
def classify_and_match(text: str) -> Tuple[bool, str, str, Optional[float], str]:
    """
    Returns: (ok: bool, key: str, title: str, price: Optional[float], reason: str)
    """
    t = text or ""
    if BLOCK_CATS.search(t):
        return False, "block:cat", "Categoria bloqueada", None, "Categoria bloqueada"
    if PC_GAMER_RE.search(t):
        return False, "block:pcgamer", "PC Gamer bloqueado", None, "PC Gamer bloqueado"

    price = find_lowest_price(t)

    # TV Box – <= 200
    if TVBOX_RE.search(t):
        if price is None:
            return False, "tvbox", "TV Box", None, "sem preço"
        if price <= 200:
            return True, "tvbox", "TV Box", price, "<= 200"
        return False, "tvbox", "TV Box", price, "> 200"

    # TV – <= 1000
    if TV_RE.search(t):
        if price is None:
            return False, "tv", "TV / Smart TV", None, "sem preço"
        if price < 200:
            return False, "tv", "TV / Smart TV", price, "preço irreal (<200)"
        if price <= 1000:
            return True, "tv", "TV / Smart TV", price, "<= 1000"
        return False, "tv", "TV / Smart TV", price, "> 1000"

    # Block small monitors <27"
    if MONITOR_SMALL_RE.search(t):
        return False, "monitor:block_small", "Monitor < 27\"", price, "tamanho pequeno"

    # Mobos (A520/H610 blocked)
    if A520_RE.search(t):
        return False, "mobo:a520", "A520 bloqueada", price, "A520 bloqueada"
    if H610_RE.search(t):
        return False, "mobo:h610", "H610 bloqueada", price, "H610 bloqueada"
    if LGA1700_RE.search(t) or SPECIFIC_B760M_RE.search(t):
        if price is None:
            return False, "mobo:lga1700", "Placa-mãe LGA1700/B760", None, "sem preço"
        if price < 300:
            return False, "mobo:lga1700", "Placa-mãe LGA1700/B760", price, "preço irreal (<300)"
        if price < 600:
            return True, "mobo:lga1700", "Placa-mãe LGA1700/B760", price, "<600"
        return False, "mobo:lga1700", "Placa-mãe LGA1700/B760", price, ">=600"

    # GPUs
    if RTX5060_3FAN_RE.search(t):
        if price is None:
            return False, "gpu:rtx5060:3fan", "RTX 5060 3 Fans", None, "sem preço"
        if price < 1500:
            return False, "gpu:rtx5060:3fan", "RTX 5060 3 Fans", price, "preço irreal (<1500)"
        if price < 1950:
            return True, "gpu:rtx5060:3fan", "RTX 5060 3 Fans", price, "<1950"
        return False, "gpu:rtx5060:3fan", "RTX 5060 3 Fans", price, ">=1950"
    if RTX5060_2FAN_RE.search(t):
        if price is None:
            return False, "gpu:rtx5060:2fan", "RTX 5060 2 Fans", None, "sem preço"
        if price < 1500:
            return False, "gpu:rtx5060:2fan", "RTX 5060 2 Fans", price, "preço irreal (<1500)"
        if price < 1850:
            return True, "gpu:rtx5060:2fan", "RTX 5060 2 Fans", price, "<1850"
        return False, "gpu:rtx5060:2fan", "RTX 5060 2 Fans", price, ">=1850"
    if RTX5060TI_RE.search(t):
        if price is None:
            return False, "gpu:rtx5060ti", "RTX 5060 Ti", None, "sem preço"
        if price < 1500:
            return False, "gpu:rtx5060ti", "RTX 5060 Ti", price, "preço irreal (<1500)"
        if price < 2100:
            return True, "gpu:rtx5060ti", "RTX 5060 Ti", price, "<2100"
        return False, "gpu:rtx5060ti", "RTX 5060 Ti", price, ">=2100"
    if RTX5060_RE.search(t):
        if price is None:
            return False, "gpu:rtx5060", "RTX 5060", None, "sem preço"
        if price < 1500:
            return False, "gpu:rtx5060", "RTX 5060", price, "preço irreal (<1500)"
        if price < 1900:
            return True, "gpu:rtx5060", "RTX 5060", price, "<1900"
        return False, "gpu:rtx5060", "RTX 5060", price, ">=1900"
    if RTX5070_FAM.search(t):
        if price is None:
            return False, "gpu:rtx5070", "RTX 5070/5070 Ti", None, "sem preço"
        if price < 2500:
            return False, "gpu:rtx5070", "RTX 5070/5070 Ti", price, "preço irreal (<2500)"
        if price < 3500:
            return True, "gpu:rtx5070", "RTX 5070/5070 Ti", price, "<3500"
        return False, "gpu:rtx5070", "RTX 5070/5070 Ti", price, ">=3500"

    # SSD Kingston
    if SSD_RE.search(t) and M2_RE.search(t) and TB1_RE.search(t):
        if price is None:
            return False, "ssd:kingston:m2:1tb", "SSD Kingston M.2 1TB", None, "sem preço"
        if price <= 400:
            return True, "ssd:kingston:m2:1tb", "SSD Kingston M.2 1TB", price, "<=400"
        return False, "ssd:kingston:m2:1tb", "SSD Kingston M.2 1TB", price, ">400"

    # RAM DDR4 16GB 3200
    if RAM_16GB_3200_RE.search(t):
        if price is None:
            return False, "ram:16gb3200", "Memória 16GB DDR4 3200MHz", None, "sem preço"
        if price < 100:
            return False, "ram:16gb3200", "Memória 16GB DDR4 3200MHz", price, "preço irreal (<100)"
        if price <= 300:
            return True, "ram:16gb3200", "Memória 16GB DDR4 3200MHz", price, "<=300"
        return False, "ram:16gb3200", "Memória 16GB DDR4 3200MHz", price, ">300"

    # Ar inverter
    if AR_INVERTER_RE.search(t):
        if price is None:
            return False, "ar_inverter", "Ar Condicionado Inverter", None, "sem preço"
        if price < 1000:
            return False, "ar_inverter", "Ar Condicionado Inverter", price, "preço irreal (<1000)"
        if price < 1500:
            return True, "ar_inverter", "Ar Condicionado Inverter", price, "<1500"
        return False, "ar_inverter", "Ar Condicionado Inverter", price, ">=1500"

    # Monitors 27"+ 144Hz
    if MONITOR_LG_27_RE.search(t):
        if price is None:
            return False, "monitor:lg27", "Monitor LG UltraGear 27\" 180Hz", None, "sem preço"
        if price < 200:
            return False, "monitor:lg27", "Monitor LG UltraGear 27\" 180Hz", price, "preço irreal (<200)"
        if price < 700:
            return True, "monitor:lg27", "Monitor LG UltraGear 27\" 180Hz", price, "<700"
        return False, "monitor:lg27", "Monitor LG UltraGear 27\" 180Hz", price, ">=700"
    if MONITOR_RE.search(t) and MONITOR_SIZE_RE.search(t) and MONITOR_144HZ_RE.search(t):
        if price is None:
            return False, "monitor", "Monitor 27\"+ 144Hz+", None, "sem preço"
        if price < 200:
            return False, "monitor", "Monitor 27\"+ 144Hz+", price, "preço irreal (<200)"
        if price < 700:
            return True, "monitor", "Monitor 27\"+ 144Hz+", price, "<700"
        return False, "monitor", "Monitor 27\"+ 144Hz+", price, ">=700"

    return False, "none", "sem match", price, "sem match"


# -------------------------
# Tests
# -------------------------
TESTS = [
    {
        "label": "Xiaomi Box S 350 (TV Box >200 -> IGNORE)",
        "text": """Xiaomi Box S 3ª Geração 4K por R$ 350‼️

Xiaomi Box S 3ª Geração 4K 2GB RAM 32GB WiFi6 Google TV

🔥 R$ 350,00  

Cupom: VIRADADECUPOM

Link: https://mercadolivre.com/sec/1QYwUV4

Frete grátis

— via @outletblackfriday""",
        "expected_ok": False,
        "expected_key": "tvbox",
        "expected_price": 350.0
    },
    {
        "label": "Ar Condicionado TCL 1438 (AR inverter match)",
        "text": """🔥 AR CONDICIONADO TCL SPLIT HI WALL ELITE INVERTER 12.000 BTUS TAC-12CSGV-INV 220V

💸 - R$1.438,10 à vista 
🎟 - Cupom: VEMQUEVEM 
🚚 - FRETE GRÁTIS!""",
        "expected_ok": True,
        "expected_key": "ar_inverter",
        "expected_price": 1438.10
    },
    {
        "label": "Smart TV Toshiba 1690 (TV >1000 -> IGNORE)",
        "text": """➡️ Smart TV QLED 55 4K Toshiba Google TV 3HDMI 2USB Wi-Fi

✅ R$ 1.690 😱😱 12x Sem Juros
🏷 Resgate cupom R$ 200 OFF aqui:
https://s.shopee.com.br/50Rg4YyBHr""",
        "expected_ok": False,
        "expected_key": "tv",
        "expected_price": 1690.0
    },
    {"label": "SSD Kingston 399.90 -> match", "text": "SSD Kingston NVMe M.2 1 TB por R$ 399,90. Frete grátis.", "expected_ok": True, "expected_key":"ssd:kingston:m2:1tb", "expected_price":399.90},
    {"label": "RAM 16GB DDR4 3200 289.90 -> match", "text":"Memória DDR4 16 GB 3200 MHz — várias marcas, R$ 289,90", "expected_ok":True, "expected_key":"ram:16gb3200","expected_price":289.90},
    {"label": "Monitor 24\" 199 -> small block", "text": "Monitor 24\" 199,00 promoção", "expected_ok":False, "expected_key":"monitor:block_small","expected_price":199.0},
    {"label": "Mobo B760M 599 -> match (<=600)", "text": "B760M ATX, placa-mãe B760 por R$ 599,00 - pronta entrega", "expected_ok":True, "expected_key":"mobo:lga1700","expected_price":599.0},
    {"label": "TV Box 150 -> match", "text": "Mi Box TV BOX R$ 150,00 promocao", "expected_ok":True, "expected_key":"tvbox","expected_price":150.0},
]

def approx(a, b, tol=0.5):
    if a is None and b is None: return True
    if isinstance(a, (int,float)) and isinstance(b, (int,float)):
        return abs(a - b) <= tol
    return False

def run_tests():
    passed = 0
    for tc in TESTS:
        ok, key, title, price, reason = classify_and_match(tc["text"])
        expect_ok = tc["expected_ok"]
        expect_key = tc["expected_key"]
        expect_price = tc["expected_price"]
        key_ok = (key == expect_key)
        price_ok = approx(price, expect_price)
        ok_ok = (ok == expect_ok)
        all_ok = key_ok and price_ok and ok_ok
        print("-"*80)
        print("TEST:", tc["label"])
        print("EXPECTED: ok=", expect_ok, " key=", expect_key, " price=", expect_price)
        print("GOT     : ok=", ok, " key=", key, " price=", price, " reason=", reason)
        print("RESULT  :", "PASS" if all_ok else "FAIL")
        if all_ok:
            passed += 1
    print("="*80)
    print(f"Passed {passed}/{len(TESTS)} tests")

if __name__ == "__main__":
    run_tests()
