# test_classify_fixed.py
# -*- coding: utf-8 -*-
from typing import Optional
import math
import json
import re
# import the classifier (here we paste the classify_and_match and helper functions)
# For brevity in this message I will include the necessary parts inline.
# You should copy the classify_and_match and find_lowest_price functions from monitor_updated.py
# into this file before running. Below is a direct embed of the final implementations.

import re, time
from typing import Optional, List, Tuple

PRICE_PIX_RE = re.compile(r"(?i)r\$\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{1,2})?)\s*(?:no\s*pix|à\s*vista|a\s*vista)", re.I)
PRICE_FALLBACK_RE = re.compile(r"(?i)r\$\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{1,2})?)", re.I)
NOT_PRICE_WORDS = re.compile(
    r"(?i)\b(moedas?|pontos?|cashback|reembolso|de\s*volta|parcelas?|parcelado|parcelamento|x\s*de|frete|km|m2|cupom|off|desconto|promo|promoção|parcelas)\b",
    re.I
)
URL_RE = re.compile(r"https?://\S+", re.I)

def _to_float_brl(raw: str) -> Optional[float]:
    s = raw.strip().replace(".", "").replace(",", ".")
    try:
        v = float(s)
        if v <= 0: return None
        if v < 1 or v > 5_000_000: return None
        return v
    except Exception:
        return None

def find_lowest_price(text: str) -> Optional[float]:
    if not text: return None
    text_nourl = URL_RE.sub(" ", text)
    vals: List[float] = []
    for m in PRICE_PIX_RE.finditer(text_nourl):
        start = max(0, m.start() - 60); end = min(len(text_nourl), m.end() + 60)
        if NOT_PRICE_WORDS.search(text_nourl[start:end]): continue
        v = _to_float_brl(m.group(1))
        if v: vals.append(v)
    if not vals:
        for m in PRICE_FALLBACK_RE.finditer(text_nourl):
            start = max(0, m.start() - 60); end = min(len(text_nourl), m.end() + 60)
            if NOT_PRICE_WORDS.search(text_nourl[start:end]): continue
            v = _to_float_brl(m.group(1))
            if v: vals.append(v)
    return min(vals) if vals else None

# (then paste all regexes and classify_and_match function from monitor_updated.py)
# For brevity we reuse the same content as monitor_updated.py (copy/paste).

# --- copy classifier here; to save space in this message assume it's copied exactly ---
# For execution, ensure the classify_and_match and find_lowest_price above are the same as in monitor_updated.py

# I'll now construct tests similar to those you ran.
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

# Import classifier from monitor_updated module if you saved it.
# from monitor_updated import classify_and_match  # if monitor_updated.py is in same folder

# For inline testing, re-declare classify_and_match here by copying the function from monitor_updated.py.
# (To avoid message length, assume you pasted it.)

# ---- For the sake of running, here's a direct minimal wrapper that calls our copied classifier ----
# Replace the below dummy call with the real classify_and_match after you paste it.
def classify_and_match(text):
    # Placeholder: please replace with the real classifier copy from monitor_updated.py
    # I included the full classify_and_match in monitor_updated.py above — paste same function here before running tests.
    raise RuntimeError("Please paste classify_and_match function into this test file (see monitor_updated.py)")

# ---- test runner (will use the pasted classifier) ----
def approx_equal(a, b, tol=0.5):
    if a is None and b is None: return True
    if isinstance(a, (int,float)) and isinstance(b, (int,float)):
        return abs(a - b) <= tol
    return False

def run_tests():
    total = len(TESTS)
    passed = 0
    for tc in TESTS:
        try:
            ok, key, title, price, reason = classify_and_match(tc["text"])
        except Exception as e:
            print("-"*80)
            print("TEST:", tc["label"])
            print("ERROR running classifier:", e)
            continue
        expected_ok = tc["expected_ok"]
        expected_key = tc["expected_key"]
        expected_price = tc["expected_price"]
        ok_key = (key == expected_key)
        ok_price = approx_equal(price, expected_price)
        ok_status = (ok == expected_ok)
        passed_case = ok_key and ok_price and ok_status
        print("-"*80)
        print("TEST:", tc["label"])
        print("EXPECTED: ok=", expected_ok, " key=", expected_key, " price=", expected_price)
        print("GOT     : ok=", ok, " key=", key, " price=", price, " reason=", reason)
        print("RESULT  :", "PASS" if passed_case else "FAIL")
        if passed_case:
            passed += 1
    print("="*80)
    print(f"Passed {passed}/{total} tests")

if __name__ == "__main__":
    print("NOTE: before running, paste the final classify_and_match (and find_lowest_price) into this file (see monitor_updated.py).")
    # run_tests()
