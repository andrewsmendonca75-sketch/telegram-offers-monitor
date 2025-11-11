def parse_preco_br(texto):
    candidatos = re.findall(r'(?:R\$ ?)(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)', texto)
    precos = []
    for c in candidatos:
        v = float(c.replace('.', '').replace(',', '.'))
        precos.append(v)
    # remove preços muito baixos que “cheiram” a cupom/frete
    precos = [p for p in precos if p >= piso_categoria_estimada(texto)]
    return min(precos) if precos else None

def match_gpu(texto):
    m = re.search(r'\b(rtx|rx)\s*(\d{3,4}0(?:\s*ti)?)\b', texto, re.I)
    if not m: return None
    sku = normaliza_sku(m.group(1), m.group(2))
    return sku if sku in SKUS_INTERESSE else None

def aprovado_por_preco(sku, preco):
    regras = {
        'gpu:rtx5060': 1899,
        'gpu:rtx5070': 3859,
        'gpu:rx7600': 1699,
        # ...
    }
    if preco in (None, 0, 100.00): return False
    teto = regras.get(sku)
    return teto is not None and preco <= teto
