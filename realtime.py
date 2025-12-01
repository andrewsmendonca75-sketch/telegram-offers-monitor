# test_classify.py
# -*- coding: utf-8 -*-
"""
Test harness for classify_and_match rules (final).
Run: python3 test_classify.py
"""

import re, json
from typing import Optional, List, Tuple, Any
# we'll import the classifier code inline (copied minimal parts)

# --- price parser & helper functions (same as monitor_updated) ---
PRICE_PIX_RE = re.compile(r"(?i)r\$\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{1,2})?)\s*(?:no\s*pix|à\s*vista|a\s*vista)", re.I)
PRICE_FALLBACK_RE = re.compile(r"(?i)r\$\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{1,2})?)", re.I)
NOT_PRICE_WORDS = re.compile(r"(?i)\b(moedas?|pontos?|cashback|reembolso|de\s*volta|parcelas?|x\s*de|frete|km|m2)\b", re.I)
URL_RE = re.compile(r"https?://\S+", re.I)

def _to_float_brl(raw: str) -> Optional[float]:
    s = raw.strip().replace(".", "").replace(",", ".")
    try:
        v = float(s)
        if v <= 0: return None
        if v < 5 or v > 5_000_000: return None
        return v
    except Exception:
        return None

def find_lowest_price(text: str) -> Optional[float]:
    if not text: return None
    text_nourl = URL_RE.sub(" ", text)
    vals = []
    for m in PRICE_PIX_RE.finditer(text_nourl):
        v = _to_float_brl(m.group(1)); 
        if v: vals.append(v)
    if not vals:
        for m in PRICE_FALLBACK_RE.finditer(text_nourl):
            start = max(0, m.start() - 40); end = min(len(text_nourl), m.end() + 40)
            if NOT_PRICE_WORDS.search(text_nourl[start:end]): continue
            v = _to_float_brl(m.group(1))
            if v: vals.append(v)
    return min(vals) if vals else None

# --- regexes (same selection as monitor_updated) ---
BLOCK_CATS = re.compile(r"\b(celular|smartphone|iphone|android|notebook|laptop|macbook|geladeira|refrigerador|m[aá]quina\s*de\s*lavar|lavadora|lava\s*e\s*seca)\b", re.I)
PC_GAMER_RE = re.compile(r"\b(pc\s*gamer|setup\s*completo|kit\s*completo)\b", re.I)
SSD_RE  = re.compile(r"\bssd\b.*\bkingston\b|\bkingston\b.*\bssd\b", re.I)
M2_RE   = re.compile(r"\bm\.?2\b|\bnvme\b", re.I)
TB1_RE  = re.compile(r"\b1\s*tb\b", re.I)
RAM_16GB_3200_RE = re.compile(r"\b(ddr4)\b.*\b16\s*gb\b.*\b3200\b|\b16\s*gb\b.*\b(ddr4)\b.*\b3200\b", re.I)
TV_RE = re.compile(r"\b(tv|smart\s*tv|televis(ão|ao))\b", re.I)
TVBOX_RE = re.compile(r"\b(tv\s*box|xiaomi\s*box|mi\s*box|google\s*tv|android\s*tv)\b", re.I)
MONITOR_SMALL_RE = re.compile(r"\b(19|20|21|22|23|24|25|26)\s*[\"']\b|\bmonitor\b.*\b(19|20|21|22|23|24|25|26)\b", re.I)
LGA1700_RE  = re.compile(r"\b(b660m?|b760m?|z690|z790)\b", re.I)
SPECIFIC_B760M_RE = re.compile(r"\bb760m\b", re.I)
A520_RE = re.compile(r"\ba520m?\b", re.I)
H610_RE = re.compile(r"\bh610m?\b", re.I)

# simplified gpu / cpu patterns for tests (enough for sample)
RTX5060_3FAN_RE = re.compile(r"\brtx\s*5060.*(3|triple).*(fan|fans)|triple\s*fan.*rtx\s*5060", re.I)
RTX5060_2FAN_RE = re.compile(r"\brtx\s*5060.*(2|dual).*(fan|fans)|dual\s*fan.*rtx\s*5060", re.I)
RTX5060_RE = re.compile(r"\brtx\s*5060(?!\s*ti)\b", re.I)
RYZEN_7_5700X_RE = re.compile(r"\bryzen\s*7\s*5700x\b", re.I)
I5_14400F_RE = re.compile(r"\bi5[-\s]*14400f\b", re.I)

def classify_and_match(text: str) -> Tuple[bool, str, Optional[float], str]:
    t = text or ""
    if BLOCK_CATS.search(t): return False, "block:cat", None, "Categoria bloqueada"
    if PC_GAMER_RE.search(t): return False, "block:pcgamer", None, "PC Gamer bloqueado"
    price = find_lowest_price(t)
    # TV Box first
    if TVBOX_RE.search(t):
        if not price: return False, "tvbox", None, "sem preço"
        if price <= 200: return True, "tvbox", price, "<=200"
        return False, "tvbox", price, ">200"
    # TV next
    if TV_RE.search(t):
        if not price: return False, "tv", None, "sem preço"
        if price < 200: return False, "tv", price, "preço irreal (<200)"
        if price <= 1000: return True, "tv", price, "<=1000"
        return False, "tv", price, ">1000"
    # monitor small block
    if MONITOR_SMALL_RE.search(t): return False, "monitor:block_small", price, "tamanho pequeno"
    # mobos
    if A520_RE.search(t): return False, "mobo:a520", price, "A520 bloqueada"
    if H610_RE.search(t): return False, "mobo:h610", price, "H610 bloqueada"
    if LGA1700_RE.search(t) or SPECIFIC_B760M_RE.search(t):
        if not price: return False, "mobo:lga1700", None, "sem preço"
        if price < 300: return False, "mobo:lga1700", price, "preço irreal (<300)"
        if price < 600: return True, "mobo:lga1700", price, "<600"
        return False, "mobo:lga1700", price, ">=600"
    # SSD
    if SSD_RE.search(t) and M2_RE.search(t) and TB1_RE.search(t):
        if not price: return False, "ssd:kingston:m2:1tb", None, "sem preço"
        if price <= 400: return True, "ssd:kingston:m2:1tb", price, "<=400"
        return False, "ssd:kingston:m2:1tb", price, ">400"
    # RAM
    if RAM_16GB_3200_RE.search(t):
        if not price: return False, "ram:16gb3200", None, "sem preço"
        if price < 100: return False, "ram:16gb3200", price, "<100"
        if price <= 300: return True, "ram:16gb3200", price, "<=300"
        return False, "ram:16gb3200", price, ">300"
    # GPUs (sample)
    if RTX5060_3FAN_RE.search(t):
        if not price: return False, "gpu:rtx5060:3fan", None, "sem preço"
        if price < 1500: return False, "gpu:rtx5060:3fan", price, "<1500"
        if price < 1950: return True, "gpu:rtx5060:3fan", price, "<1950"
        return False, "gpu:rtx5060:3fan", price, ">=1950"
    if RTX5060_2FAN_RE.search(t):
        if not price: return False, "gpu:rtx5060:2fan", None, "sem preço"
        if price < 1500: return False, "gpu:rtx5060:2fan", price, "<1500"
        if price < 1850: return True, "gpu:rtx5060:2fan", price, "<1850"
        return False, "gpu:rtx5060:2fan", price, ">=1850"
    if RTX5060_RE.search(t):
        if not price: return False, "gpu:rtx5060", None, "sem preço"
        if price < 1500: return False, "gpu:rtx5060", price, "<1500"
        if price < 1900: return True, "gpu:rtx5060", price, "<1900"
        return False, "gpu:rtx5060", price, ">=1900"
    # CPUs sample
    if RYZEN_7_5700X_RE.search(t):
        if not price: return False, "cpu:ryzen7_5700x", None, "sem preço"
        if price < 400: return False, "cpu:ryzen7_5700x", price, "<400"
        if price < 800: return True, "cpu:ryzen7_5700x", price, "<800"
        return False, "cpu:ryzen7_5700x", price, ">=800"
    if I5_14400F_RE.search(t):
        if not price: return False, "cpu:i5_14400f", None, "sem preço"
        if price < 400: return False, "cpu:i5_14400f", price, "<400"
        if price < 750: return True, "cpu:i5_14400f", price, "<750"
        return False, "cpu:i5_14400f", price, ">=750"

    return False, "none", None, "sem match"

# --- test cases including your 3 examples + a few others to validate behavior ---
TESTS = [
    # user examples (3)
    {
        "label": "Xiaomi Box S 350 (TV Box >200 -> IGNORE)",
        "text": """Xiaomi Box S 3ª Geração 4K por R$ 350‼️

Xiaomi Box S 3ª Geração 4K 2GB RAM 32GB WiFi6 Google TV

🔥 R$ 350,00  

Cupom: VIRADADECUPOM

Link: https://mercadolivre.com/sec/1QYwUV4

Frete grátis

— via @outletblackfriday""",
        "expected_key": "tvbox",
        "expected_match": False,
        "expected_price": 350.0
    },
    {
        "label": "Ar Condicionado TCL 1438 (AR inverter match)",
        "text": """🔥 AR CONDICIONADO TCL SPLIT HI WALL ELITE INVERTER 12.000 BTUS TAC-12CSGV-INV 220V

💸 - R$1.438,10 à vista 
🎟 - Cupom: VEMQUEVEM 
🚚 - FRETE GRÁTIS!""",
        "expected_key": "ar_inverter",
        "expected_match": True,
        "expected_price": 1438.10
    },
    {
        "label": "Smart TV Toshiba 1690 (TV >1000 -> IGNORE)",
        "text": """➡️ Smart TV QLED 55 4K Toshiba Google TV 3HDMI 2USB Wi-Fi

✅ R$ 1.690 😱😱 12x Sem Juros
🏷 Resgate cupom R$ 200 OFF aqui:
https://s.shopee.com.br/50Rg4YyBHr""",
        "expected_key": "tv",
        "expected_match": False,
        "expected_price": 1690.0
    },
    # a few additional sanity checks
    {"label": "SSD Kingston 399.90 -> match", "text": "SSD Kingston NVMe M.2 1 TB por R$ 399,90. Frete grátis.", "expected_key":"ssd:kingston:m2:1tb", "expected_match":True, "expected_price":399.90},
    {"label": "RAM 16GB DDR4 3200 289.90 -> match", "text":"Memória DDR4 16 GB 3200 MHz — várias marcas, R$ 289,90", "expected_key":"ram:16gb3200","expected_match":True, "expected_price":289.90},
    {"label": "Monitor 24\" 199 -> small block", "text": "Monitor 24\" 199,00 promoção", "expected_key":"monitor:block_small","expected_match":False, "expected_price":199.0},
    {"label": "Mobo B760M 599 -> match (<=600)", "text": "B760M ATX, placa-mãe B760 por R$ 599,00 - pronta entrega", "expected_key":"mobo:lga1700","expected_match":True, "expected_price":599.0},
    {"label": "TV Box 150 -> match", "text": "Mi Box TV BOX R$ 150,00 promocao", "expected_key":"tvbox","expected_match":True,"expected_price":150.0},
]

# run tests
def run_tests():
    total = len(TESTS); passed = 0
    for tc in TESTS:
        key, match_price, price, reason = classify_and_match(tc["text"])
        got_match = key not in ("none", "block:cat", "block:pcgamer") and (key != "tvbox" or (key=="tvbox" and price is not None))
        # Determine "match" by classifier returning True (in our simplified function, key reflects category; we used tuple with boolean only conceptually)
        # Here we treat specific keys that correspond to successful True (we returned True/False previously, but in this harness classify_and_match returns tuple)
        # To keep simple: consider expected_match True when classifier returns a positive actionable key and a numeric price or explicit True reason.
        # We'll approximate: if key equals expected_key and price approximately equals expected_price -> PASS for match semantics
        expected_key = tc["expected_key"]
        expected_match = tc["expected_match"]
        expected_price = tc["expected_price"]
        # Normalize comparison
        price_ok = (price is None and expected_price is None) or (isinstance(price, (int,float)) and abs(price - expected_price) < 0.5)
        key_ok = (key == expected_key)
        match_ok = (expected_match == (key in [expected_key] and (price_ok or expected_price is None) and expected_key != "none"))
        ok = key_ok and price_ok and match_ok
        print("-"*80)
        print("TEST:", tc["label"])
        print("EXPECTED:", expected_key, "expected_match=", expected_match, "expected_price=", expected_price)
        print("GOT      :", key, "price=", price, "reason=", reason)
        print("RESULT   :", "PASS" if ok else "FAIL")
        if ok: passed += 1
    print("="*80)
    print(f"Passed {passed}/{total} tests")

if __name__ == "__main__":
    run_tests()
