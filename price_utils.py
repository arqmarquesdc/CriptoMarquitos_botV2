"""
Parseo de precios en formato argentino/rioplatense, compartido entre todos
los módulos del bot (bot_btc_h4, saylor_bot, ds_bot) para no tener la misma
heurística duplicada y con pequeñas diferencias en cada uno.
"""

import re

_NUMERO_RE = re.compile(r"\d[\d.,]*")


def parse_price_ar(token):
    """
    Heurística para números en español rioplatense: si hay coma, el punto es
    separador de miles y la coma es decimal ("76.900,50" -> 76900.50). Si no
    hay coma pero hay un solo punto seguido de exactamente 3 dígitos, es
    separador de miles ("77.450" -> 77450), no decimal. Sirve tanto para
    precios de BTC (miles) como de MSTR (decimales tipo "131.36").
    """
    token = token.strip()
    if "," in token:
        token = token.replace(".", "").replace(",", ".")
    elif token.count(".") == 1 and len(token.split(".")[-1]) == 3:
        token = token.replace(".", "")
    return float(token)


def extract_price_from_text(text):
    """
    Busca un número simple en un texto corto (para cuando le pedimos a Marcos
    "decime el precio" y él responde solo con el número, con o sin texto
    alrededor, ej. "131.36" o "a 77450"). Devuelve el precio (float) o None
    si no encuentra nada parseable.
    """
    m = _NUMERO_RE.search(text)
    if not m:
        return None
    try:
        precio = parse_price_ar(m.group(0))
    except ValueError:
        return None
    return precio if precio > 0 else None
