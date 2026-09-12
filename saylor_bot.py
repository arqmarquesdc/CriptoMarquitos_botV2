"""
Bot de seguimiento de la estrategia "Michael Saylor" BTC (long apalancado de
largo plazo, con recargas diarias en base al ROI % no realizado).

Independiente del bot de trading de corto plazo (bot_btc_h4.py), pero
comparte el mismo bot/chat de Telegram (mismos TELEGRAM_BOT_TOKEN /
TELEGRAM_CHAT_ID) y el mismo repo de GitHub como storage — un archivo más
(saylor_state.json), mismo patrón que state.json/trades.json.

Todo gratis: sin API de Claude, sin Notion. La confirmación de recargas es
por texto libre con un parser simple (regex), no IA — y las fotos del
exchange se cargan a mano (Marcos escribe los números, no se procesan
imágenes).

=== Contrato inverso (Coin-M): por qué la matemática es distinta ===
La posición pasó a fondearse en BTC (margen en BTC, contrato inverso), no en
USDT. Esto cambia dos cosas de fondo respecto a un contrato lineal (USD-M):

1. El PnL en BTC de un long inverso es notional_usd * (1/promedio - 1/actual),
   no lineal en precio. Por eso el ROI% que dispara la tabla de recarga usa:

       ROI% = leverage * (1 - promedio / precio_actual) * 100

   en vez de la fórmula lineal `leverage * (precio_actual/promedio - 1) * 100`
   que usaría un contrato USD-M. Para el mismo movimiento de precio da un
   número distinto (más negativo en pérdidas) — confirmado con Marcos antes
   de implementarlo, es un cambio de comportamiento real de la tabla.

2. Al combinar varias recargas en distintos precios, el promedio "correcto"
   para que el PnL en BTC salga bien NO es el promedio aritmético ponderado
   (ese es para contratos lineales) — es un promedio ponderado por notional
   de 1/precio (media armónica). La buena noticia: sale solo, sin fórmulas
   raras, con esta identidad:

       promedio_inverso = posicion_usd_acumulado / posicion_btc_acumulado

   donde `posicion_usd_acumulado` es la suma de las balas en dólares (valor
   nominal fijo, balas × 266,67) y `posicion_btc_acumulado` es la suma del
   BTC realmente depositado en cada recarga (balas_usd / precio_de_ese_día).
   Ese cociente ($/BTC) ya ES el promedio armónico correcto — no hace falta
   calcularlo aparte.

=== Caso "situación crítica" (ROI <= -40%) ===
La tabla pide "+3 balas a la posición y +3 balas directo al margen". Las
balas "directo al margen" son colchón extra: SUMAN BTC depositado (y cuentan
para el límite de 30 balas/USD 8.000) pero NO suman notional/exposición, así
que no entran en el cálculo de promedio_inverso ni de PnL — sí achican la
distancia a liquidación, porque hay más margen total respaldando la misma
exposición. Por eso se trackean aparte (`margen_extra_btc_acumulado`).

=== Arrancar y cerrar la estrategia ===
El capital total NO está fijo en el código — se define con
"/saylor_iniciar <capital_total>", que calcula el tamaño de bala
(capital/30) y la carga inicial (regla de "Inicio": 2 balas, no 1 como el
resto de los días en ROI positivo). Rechaza si ya hay una posición abierta,
para no pisar datos reales.

"/saylor_cerrar <precio>" liquida la posición al precio dado: calcula
ROI/PnL final, lo deja anotado en el log, y resetea los acumuladores para
poder volver a arrancar más adelante. A partir de ROI +10% el bot ya
empieza a sugerir evaluar el cierre (más fuerte desde +20%).

Uso:
    python saylor_bot.py --daily-check   # manda el chequeo diario (solo lectura)

La confirmación de recargas por texto libre y los comandos se manejan desde
bot_btc_h4.py (process_telegram_updates), que importa este módulo.
"""

import os
import re
import json
import copy
import argparse
from datetime import datetime, timezone, date, timedelta

import requests

from price_utils import parse_price_ar

CAPITAL_TOTAL_DEFAULT = 8000.0  # fallback si por algún motivo state no tiene capital_total todavía
MAX_BALAS = 30
BALAS_INICIO = 2  # regla de "Inicio" de la planilla: la primera carga es 2 balas, no 1
LEVERAGE = 5
TAKE_PROFIT_MIN_PCT = 10  # desde acá, sugerir evaluar el cierre de la estrategia
TAKE_PROFIT_MAX_PCT = 20  # zona ideal de cierre (recomendación más fuerte)
LIMITES_TABLA = [0, -5, -10, -20, -40]  # umbrales de la tabla de recarga, para el aviso "cerca de un límite"
CERCA_LIMITE_PCT = 1.0  # a menos de 1 punto porcentual de un límite, sugerir confirmar el ROI real

SAYLOR_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "saylor_state.json")
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
_AR_TZ = timezone(timedelta(hours=-3))  # Argentina, sin horario de verano


def _hora_actual_ar():
    return datetime.now(_AR_TZ).strftime("%H:%M hs")


def balas_a_agregar(roi_pct):
    """
    Tabla de recarga diaria según ROI % no realizado estimado (ahora en
    términos de BTC, contrato inverso — ver docstring del módulo). Devuelve
    (balas_a_la_posicion, balas_directo_al_margen) — el segundo valor solo es
    distinto de 0 en el caso crítico (ROI <= -40%).

    Escalones (confirmados con la planilla "Michael Saylor.xlsx" de Marcos):
    Positivo: 1 | hasta -5%: 2 | hasta -10%: 3 | de -10% a -20%: 4 |
    desde -20%: 5 | crítica desde -40%: 3 a la posición + 3 a margen.
    El tramo de -15% a -20% se extiende con el mismo valor que -10%/-15%
    (4 balas) — la planilla no marca un escalón propio ahí, el próximo
    escalón real es -20%, no -15%.
    """
    if roi_pct <= -40:
        return 3, 3
    if roi_pct <= -20:
        return 5, 0
    if roi_pct < -10:
        return 4, 0
    if roi_pct < -5:
        return 3, 0
    if roi_pct < 0:
        return 2, 0
    return 1, 0


def cerca_de_limite(roi_pct, umbral=CERCA_LIMITE_PCT):
    return min(abs(roi_pct - l) for l in LIMITES_TABLA) < umbral


def nota_cierre(roi_pct):
    """
    Sugerencia de evaluar el cierre de la estrategia cuando el ROI ya está en
    zona de ganancia — pedido explícito de Marcos: a partir de +10% avisar
    que es un buen momento para considerarlo, con un aviso más fuerte a
    partir de +20% (zona ideal de cierre). Devuelve None si no aplica.
    """
    if roi_pct >= TAKE_PROFIT_MAX_PCT:
        return (f"🎯 ROI en {roi_pct:+.2f}% — ya estás en la zona ideal de cierre "
                f"({TAKE_PROFIT_MIN_PCT:.0f}-{TAKE_PROFIT_MAX_PCT:.0f}%+), fuerte candidato a tomar ganancias.")
    if roi_pct >= TAKE_PROFIT_MIN_PCT:
        return (f"💰 ROI en {roi_pct:+.2f}% — superó el {TAKE_PROFIT_MIN_PCT:.0f}%, "
                f"empezá a evaluar si conviene cerrar la estrategia (\"/saylor_cerrar <precio>\").")
    return None


def calcular_promedio_inverso(posicion_usd_acumulado, posicion_btc_acumulado):
    """
    $/BTC de la posición, ponderado correctamente para contrato inverso (ver
    docstring del módulo — es el cociente de los dos acumulados, no hace
    falta una fórmula de media armónica aparte). None si todavía no hay
    ninguna bala de posición cargada.
    """
    if not posicion_btc_acumulado:
        return None
    return posicion_usd_acumulado / posicion_btc_acumulado


def estimar_roi_btc(precio_actual, promedio_inverso, leverage=LEVERAGE):
    """
    ROI % del contrato inverso: leverage * (1 - promedio/precio_actual) * 100.
    Positivo cuando el precio subió (ganancia en un long), como se espera.
    """
    if not promedio_inverso:
        return None
    return leverage * (1 - promedio_inverso / precio_actual) * 100


def estimar_liquidacion_inversa(promedio_inverso, posicion_btc_acumulado, margen_total_btc, leverage=LEVERAGE):
    """
    Estimación gruesa (ignora funding, fees y margen de mantenimiento real
    del exchange — el valor exacto lo muestra el exchange). Tiene en cuenta
    el margen extra depositado en la situación crítica (más margen total
    respaldando la misma exposición = liquidación más lejos).
    """
    if not promedio_inverso or not posicion_btc_acumulado:
        return None
    denom = 1 + margen_total_btc / (posicion_btc_acumulado * leverage)
    return round(promedio_inverso / denom, 2)


def load_saylor_state():
    if os.path.exists(SAYLOR_STATE_FILE):
        state = json.load(open(SAYLOR_STATE_FILE, "r"))
    else:
        state = {}
    state.setdefault("capital_total", None)  # se fija con /saylor_iniciar <capital>
    state.setdefault("start_date", None)
    state.setdefault("balas_usadas", 0)
    state.setdefault("posicion_usd_acumulado", 0.0)
    state.setdefault("posicion_btc_acumulado", 0.0)
    state.setdefault("margen_extra_btc_acumulado", 0.0)
    state.setdefault("log", [])
    state.setdefault("_previous_snapshot", None)
    state.setdefault("contador_operaciones", 0)  # para los ids MS-01, MS-02, ...
    return state


def _siguiente_id_operacion(state):
    """
    Da el próximo id secuencial (MS-01, MS-02, ...). El contador vive DENTRO
    del state, así que si se deshace una operación (que restaura el state
    completo desde el snapshot previo) el contador también vuelve para atrás
    — el próximo id real reutiliza el número que quedó libre, en vez de
    seguir sumando huecos.
    """
    state["contador_operaciones"] = state.get("contador_operaciones", 0) + 1
    return f"MS-{state['contador_operaciones']:02d}"


def save_saylor_state(state):
    with open(SAYLOR_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def bala_size(state):
    capital_total = state.get("capital_total") or CAPITAL_TOTAL_DEFAULT
    return round(capital_total / MAX_BALAS, 2)


def get_current_btc_price():
    """Último precio de cierre de BTC/USD en Kraken (vela de 15 min más reciente)."""
    resp = requests.get(KRAKEN_OHLC_URL, params={"pair": "XBTUSD", "interval": 15}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken API error: {data['error']}")
    result = data["result"]
    pair_key = next(k for k in result.keys() if k != "last")
    last_candle = result[pair_key][-1]
    return float(last_candle[4])  # close


def day_number(state, today=None):
    today = today or date.today()
    start = state.get("start_date")
    if not start:
        return None
    start_date = date.fromisoformat(start)
    return (today - start_date).days + 1


def _margen_total_btc(state):
    return round(state.get("posicion_btc_acumulado", 0.0) + state.get("margen_extra_btc_acumulado", 0.0), 8)


def format_daily_check_message(state, price):
    posicion_usd = state.get("posicion_usd_acumulado", 0.0)
    posicion_btc = state.get("posicion_btc_acumulado", 0.0)
    balas_usadas = state.get("balas_usadas", 0)
    dia = day_number(state)

    promedio_inverso = calcular_promedio_inverso(posicion_usd, posicion_btc)
    if promedio_inverso is None:
        return (
            "⚠️ *Estrategia Saylor BTC* — todavía no arrancaste. Mandame "
            "\"/saylor_iniciar <capital_total>\" (ej. \"/saylor_iniciar 8000\") "
            "para que te calcule la carga inicial."
        )

    size = bala_size(state)
    roi = estimar_roi_btc(price, promedio_inverso)
    balas_posicion, balas_margen = balas_a_agregar(roi)
    total_balas_hoy = balas_posicion + balas_margen
    balas_usd_hoy = round(total_balas_hoy * size, 2)
    btc_a_depositar_hoy = round(balas_usd_hoy / price, 8)
    balas_restantes = MAX_BALAS - balas_usadas - total_balas_hoy

    avisos = []
    if cerca_de_limite(roi):
        avisos.append("⚠️ El ROI está cerca de un límite de la tabla — confirmá el ROI real del exchange antes de recargar.")
    if balas_restantes < 0:
        capital_total = state.get("capital_total") or CAPITAL_TOTAL_DEFAULT
        avisos.append(f"🛑 Esta recarga superaría el límite de {MAX_BALAS} balas / USD {capital_total:,.0f}. Revisá antes de ejecutar.")
    elif balas_restantes <= 3:
        avisos.append(f"⚠️ Quedan pocas balas ({balas_restantes}) para futuras recargas.")
    nota = nota_cierre(roi)
    if nota:
        avisos.append(nota)

    dia_txt = f"Día {dia}" if dia is not None else "Día s/d"
    recarga_txt = f"+{balas_posicion} balas a la posición"
    if balas_margen:
        margen_extra_btc = round(balas_margen * size / price, 8)
        recarga_txt += (f" y +{balas_margen} directo al margen (situación crítica — "
                         f"≈{margen_extra_btc:.8f} BTC extra, no suma exposición, solo colchón)")

    lines = [
        f"📅 *Estrategia Saylor BTC — {dia_txt}*",
        f"Precio BTC/USD actual: ${price:,.2f} (Kraken, {_hora_actual_ar()})",
        f"Promedio (contrato inverso): ${promedio_inverso:,.2f}",
        f"ROI estimado (BTC, contrato inverso): {roi:+.2f}%",
        f"Recarga sugerida: {recarga_txt}",
        f"Equivale a: {balas_usd_hoy:,.2f} USD nominal → ≈{btc_a_depositar_hoy:.8f} BTC a depositar como margen al precio actual",
        f"Balas usadas: {balas_usadas} → quedarían {balas_restantes} de {MAX_BALAS}",
    ]
    if avisos:
        lines.append("")
        lines.extend(avisos)
    lines.append(
        "\n_El BTC a depositar es una referencia al precio de AHORA — el monto real se "
        "fija cuando confirmes (\"Metí X balas a PRECIO\") con el precio real de tu "
        "operación. Cargá el margen como TAMAÑO DE POSICIÓN a abrir, el exchange ya "
        "multiplica x5. No es consejo financiero._"
    )
    return "\n".join(lines)


def _format_log_line(entry):
    eid = entry.get("id", "?")
    tipo = entry.get("tipo")
    if tipo == "cierre":
        return (f"— {eid} (cierre): precio ${entry['precio_cierre']:,.2f}, "
                f"ROI {entry['roi_pct']:+.2f}%, PnL {entry['pnl_btc']:+.8f} BTC")
    etiqueta = "margen extra" if tipo == "margen_extra" else "posición"
    return (f"— {eid} ({etiqueta}): {entry['balas_confirmadas']} balas a "
            f"${entry['precio_confirmado']:,.2f} → {entry['btc_depositado']:.8f} BTC")


def format_status_message(state, price=None):
    posicion_usd = state.get("posicion_usd_acumulado", 0.0)
    posicion_btc = state.get("posicion_btc_acumulado", 0.0)
    margen_extra_btc = state.get("margen_extra_btc_acumulado", 0.0)
    balas_usadas = state.get("balas_usadas", 0)
    dia = day_number(state)

    promedio_inverso = calcular_promedio_inverso(posicion_usd, posicion_btc)
    margen_total_btc = _margen_total_btc(state)
    liquidacion = estimar_liquidacion_inversa(promedio_inverso, posicion_btc, margen_total_btc)

    lines = [
        "📊 *Estado — Estrategia Saylor BTC*",
        f"Día: {dia if dia is not None else 's/d'}",
        f"Promedio (contrato inverso): {'$' + format(promedio_inverso, ',.2f') if promedio_inverso else 's/d'}",
        f"Balas usadas: {balas_usadas}/{MAX_BALAS} (restantes: {MAX_BALAS - balas_usadas})",
        f"Margen BTC depositado — posición: {posicion_btc:.8f} BTC" + (f" | extra (colchón): {margen_extra_btc:.8f} BTC" if margen_extra_btc else ""),
        f"Margen BTC total: {margen_total_btc:.8f} BTC",
        f"Liquidación estimada (aprox., no exacta): {'$' + format(liquidacion, ',.2f') if liquidacion else 's/d'}",
    ]
    if price:
        lines.append(f"Precio BTC/USD actual: ${price:,.2f} (Kraken, {_hora_actual_ar()})")
        if promedio_inverso:
            roi = estimar_roi_btc(price, promedio_inverso)
            lines.append(f"ROI estimado ahora: {roi:+.2f}%")
            nota = nota_cierre(roi)
            if nota:
                lines.append(nota)
    if not promedio_inverso:
        lines.append("\nTodavía no arrancaste — mandá \"/saylor_iniciar <capital_total>\" para empezar.")
    log = state.get("log", [])
    if log:
        lines.append("\nHistorial (últimas 10):")
        lines.extend(_format_log_line(e) for e in log[-10:])
    return "\n".join(lines)


_BALAS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*balas?", re.IGNORECASE)
_PRECIO_RE = re.compile(
    # Captura el número completo desde el primer dígito (con o sin
    # separadores de miles/decimales) — antes se cortaba a los primeros 3
    # dígitos si el precio no tenía puntos (ej. "77309" se leía como "773").
    r"(?:a|precio(?:\s+de)?|en)\s*(?:usd\$?|u\$s|\$)?\s*(\d[\d.,]*)",
    re.IGNORECASE,
)
_MARGEN_EXTRA_HINT_RE = re.compile(r"directo al margen|solo margen|margen extra|margen colch[oó]n", re.IGNORECASE)

_NUMERO_PALABRAS = {
    "una": "1", "un": "1", "uno": "1", "dos": "2", "tres": "3", "cuatro": "4",
    "cinco": "5", "seis": "6", "siete": "7", "ocho": "8", "nueve": "9", "diez": "10",
}
_NUMERO_PALABRAS_RE = re.compile(r"\b(" + "|".join(_NUMERO_PALABRAS.keys()) + r")\b", re.IGNORECASE)


def _reemplazar_numeros_en_palabras(text):
    """Convierte "dos balas" -> "2 balas" etc., para que el parser de
    recargas entienda tanto dígitos como números escritos en letras."""
    return _NUMERO_PALABRAS_RE.sub(lambda m: _NUMERO_PALABRAS[m.group(0).lower()], text)


def parse_recarga_text(text):
    """
    Devuelve (balas, precio, es_margen_extra) si el texto parece una
    confirmación de recarga ("Metí 2 balas a 77.450", "Metí dos balas a
    77450", "3 balas directo al margen a 76900"), o None si no matchea el
    patrón esperado.
    """
    text = _reemplazar_numeros_en_palabras(text)
    m_balas = _BALAS_RE.search(text)
    m_precio = _PRECIO_RE.search(text)
    if not m_balas or not m_precio:
        return None
    try:
        balas = float(m_balas.group(1).replace(",", "."))
        precio = parse_price_ar(m_precio.group(1))
    except ValueError:
        return None
    if precio <= 0 or balas <= 0:
        return None
    es_margen_extra = bool(_MARGEN_EXTRA_HINT_RE.search(text))
    return balas, precio, es_margen_extra


def looks_like_recarga(text):
    return parse_recarga_text(text) is not None


def confirmar_recarga(state, balas_confirmadas, precio_confirmado, es_margen_extra=False):
    """
    Aplica una recarga confirmada. Si es_margen_extra=True (situación
    crítica, "directo al margen"), suma BTC de colchón sin sumar exposición
    ni afectar el promedio/ROI. Guarda un snapshot previo (para /deshacer).
    Rechaza si se supera el límite de MAX_BALAS.
    """
    balas_previas = state.get("balas_usadas", 0)
    if balas_previas + balas_confirmadas > MAX_BALAS:
        capital_total = state.get("capital_total") or CAPITAL_TOTAL_DEFAULT
        return {
            "ok": False,
            "motivo": (f"Esto llevaría el total a {balas_previas + balas_confirmadas} balas, "
                       f"por encima del límite de {MAX_BALAS} (USD {capital_total:,.0f}). "
                       f"No se guardó — revisá los números."),
        }

    state["_previous_snapshot"] = copy.deepcopy({k: v for k, v in state.items() if k != "_previous_snapshot"})

    balas_usd = round(balas_confirmadas * bala_size(state), 2)
    btc_depositado = round(balas_usd / precio_confirmado, 8)

    if es_margen_extra:
        state["margen_extra_btc_acumulado"] = round(state.get("margen_extra_btc_acumulado", 0.0) + btc_depositado, 8)
    else:
        state["posicion_usd_acumulado"] = round(state.get("posicion_usd_acumulado", 0.0) + balas_usd, 2)
        state["posicion_btc_acumulado"] = round(state.get("posicion_btc_acumulado", 0.0) + btc_depositado, 8)

    state["balas_usadas"] = balas_previas + balas_confirmadas
    if not state.get("start_date"):
        state["start_date"] = date.today().isoformat()

    op_id = _siguiente_id_operacion(state)
    state.setdefault("log", []).append({
        "id": op_id,
        "fecha": datetime.now(timezone.utc).isoformat(),
        "balas_confirmadas": balas_confirmadas,
        "precio_confirmado": precio_confirmado,
        "btc_depositado": btc_depositado,
        "tipo": "margen_extra" if es_margen_extra else "posicion",
    })

    promedio_inverso = calcular_promedio_inverso(state["posicion_usd_acumulado"], state["posicion_btc_acumulado"])

    return {
        "ok": True,
        "id": op_id,
        "tipo": "margen_extra" if es_margen_extra else "posicion",
        "btc_depositado": btc_depositado,
        "promedio_inverso": promedio_inverso,
        "margen_total_btc": _margen_total_btc(state),
        "balas_usadas": state["balas_usadas"],
        "balas_restantes": MAX_BALAS - state["balas_usadas"],
    }


def deshacer_ultima_recarga(state):
    """Deshace la última operación (recarga o cierre) y devuelve la entry del
    log que se deshizo (con su id), o None si no había nada para deshacer."""
    snapshot = state.get("_previous_snapshot")
    if not snapshot:
        return None
    log = state.get("log", [])
    deshecho = log[-1] if log else None
    restored = copy.deepcopy(snapshot)
    state.clear()
    state.update(restored)
    state["_previous_snapshot"] = None
    return deshecho


def format_confirmacion_message(result):
    if not result["ok"]:
        return f"🛑 {result['motivo']}"

    if result["tipo"] == "margen_extra":
        return (
            f"✅ {result['id']} registrada como margen extra (colchón, no suma exposición).\n"
            f"BTC depositado ahora: {result['btc_depositado']:.8f} BTC\n"
            f"Margen BTC total (posición + extra): {result['margen_total_btc']:.8f} BTC\n"
            f"Balas usadas: {result['balas_usadas']}/{MAX_BALAS} (restantes: {result['balas_restantes']})\n\n"
            f"_Si algo no cierra, mandá \"/deshacer\" para revertir esta última carga._"
        )

    promedio_txt = f"${result['promedio_inverso']:,.2f}" if result["promedio_inverso"] else "s/d"
    return (
        f"✅ {result['id']} registrada.\n"
        f"BTC depositado ahora: {result['btc_depositado']:.8f} BTC\n"
        f"Nuevo promedio (contrato inverso): {promedio_txt}\n"
        f"Margen BTC total: {result['margen_total_btc']:.8f} BTC\n"
        f"Balas usadas: {result['balas_usadas']}/{MAX_BALAS} (restantes: {result['balas_restantes']})\n\n"
        f"_Si algo no cierra, mandá \"/deshacer\" para revertir esta última carga._"
    )


def iniciar_estrategia(state, capital_total, precio_actual=None):
    """
    Arranca la estrategia desde cero: fija el capital total (define el
    tamaño de bala = capital/30) y calcula cuánto entrar en la carga
    inicial (regla de "Inicio" de la planilla: 2 balas, no 1 como el resto
    de los días con ROI positivo). El margen a depositar es en USD (fijo,
    valor nominal de las balas) y también se muestra su equivalente en BTC
    al precio actual, ya que la idea es comprar BTC con BTC. Rechaza si ya
    hay una posición abierta, para no pisar datos reales por error.
    """
    if state.get("balas_usadas", 0) > 0:
        return {
            "ok": False,
            "motivo": (f"Ya hay una posición abierta ({state['balas_usadas']} balas usadas) — "
                       f"no se puede reiniciar así para no pisar datos reales. Si de verdad "
                       f"arrancás de cero, primero hay que resetear el archivo a mano."),
        }
    state["capital_total"] = capital_total
    size = bala_size(state)
    margen_usd = round(size * BALAS_INICIO, 2)
    tamano_posicion_usd = round(margen_usd * LEVERAGE, 2)
    margen_btc = round(margen_usd / precio_actual, 8) if precio_actual else None
    return {
        "ok": True,
        "capital_total": capital_total,
        "bala_size": size,
        "balas_inicio": BALAS_INICIO,
        "margen_usd": margen_usd,
        "tamano_posicion_usd": tamano_posicion_usd,
        "precio_actual": precio_actual,
        "margen_btc": margen_btc,
    }


def format_inicio_message(result):
    if not result["ok"]:
        return f"🛑 {result['motivo']}"
    lines = [
        "🚀 *Estrategia MS iniciada*",
        f"Capital total: USD {result['capital_total']:,.2f} en {MAX_BALAS} balas de "
        f"USD {result['bala_size']:,.2f} c/u.",
        f"Regla de Inicio: {result['balas_inicio']} balas de entrada.",
    ]
    if result.get("precio_actual"):
        lines.append(f"Precio BTC/USD actual: ${result['precio_actual']:,.2f} (Kraken, {_hora_actual_ar()})")
    margen_txt = f"Margen a depositar: USD {result['margen_usd']:,.2f}"
    if result.get("margen_btc"):
        margen_txt += f" ≈ {result['margen_btc']:.8f} BTC al precio actual"
    lines.append(margen_txt + ".")
    lines.append(f"Tamaño de posición resultante ({LEVERAGE}x en el exchange): USD {result['tamano_posicion_usd']:,.2f}.")
    lines.append(
        f"\nCuando compres el BTC y abras la posición, mandame \"Metí "
        f"{result['balas_inicio']} balas a PRECIO\" con el precio real al que "
        f"entraste, y lo registro (el BTC de arriba es solo una referencia al "
        f"precio de AHORA — el monto real se fija con el precio de tu operación)."
    )
    return "\n".join(lines)


def calcular_pnl_btc(posicion_usd_acumulado, promedio_inverso, precio_cierre):
    """PnL en BTC de un long inverso: notional_usd * (1/entrada - 1/salida) — ver
    docstring del módulo. No incluye funding/fees reales del exchange."""
    if not promedio_inverso:
        return None
    return posicion_usd_acumulado * (1 / promedio_inverso - 1 / precio_cierre)


def cerrar_estrategia(state, precio_cierre):
    """
    Cierra la posición: calcula el resultado final (ROI, PnL en BTC) al
    precio dado, lo deja anotado en el log, y resetea los acumuladores para
    poder arrancar de nuevo más adelante con /saylor_iniciar. El capital
    total configurado se mantiene salvo que /saylor_iniciar lo cambie.
    """
    posicion_usd = state.get("posicion_usd_acumulado", 0.0)
    posicion_btc = state.get("posicion_btc_acumulado", 0.0)
    promedio_inverso = calcular_promedio_inverso(posicion_usd, posicion_btc)
    if promedio_inverso is None:
        return {"ok": False, "motivo": "No hay ninguna posición cargada todavía — no hay nada para cerrar."}

    roi = estimar_roi_btc(precio_cierre, promedio_inverso)
    pnl_btc = calcular_pnl_btc(posicion_usd, promedio_inverso, precio_cierre)
    margen_total_btc = _margen_total_btc(state)
    btc_final_estimado = round(margen_total_btc + pnl_btc, 8)
    balas_usadas_anteriores = state.get("balas_usadas", 0)

    state["_previous_snapshot"] = copy.deepcopy({k: v for k, v in state.items() if k != "_previous_snapshot"})
    op_id = _siguiente_id_operacion(state)
    state.setdefault("log", []).append({
        "id": op_id,
        "fecha": datetime.now(timezone.utc).isoformat(),
        "tipo": "cierre",
        "precio_cierre": precio_cierre,
        "promedio_inverso": promedio_inverso,
        "roi_pct": roi,
        "pnl_btc": round(pnl_btc, 8),
        "btc_final_estimado": btc_final_estimado,
    })

    state["balas_usadas"] = 0
    state["posicion_usd_acumulado"] = 0.0
    state["posicion_btc_acumulado"] = 0.0
    state["margen_extra_btc_acumulado"] = 0.0
    state["start_date"] = None

    return {
        "ok": True,
        "id": op_id,
        "precio_cierre": precio_cierre,
        "promedio_inverso": promedio_inverso,
        "roi_pct": roi,
        "pnl_btc": pnl_btc,
        "margen_total_btc": margen_total_btc,
        "btc_final_estimado": btc_final_estimado,
        "balas_usadas_anteriores": balas_usadas_anteriores,
    }


def format_cierre_message(result):
    if not result["ok"]:
        return f"🛑 {result['motivo']}"
    return (
        f"🏁 *{result['id']} — Estrategia MS cerrada*\n"
        f"Precio de cierre: ${result['precio_cierre']:,.2f}\n"
        f"Promedio de entrada (contrato inverso): ${result['promedio_inverso']:,.2f}\n"
        f"ROI final (BTC, contrato inverso): {result['roi_pct']:+.2f}%\n"
        f"PnL estimado: {result['pnl_btc']:+.8f} BTC\n"
        f"Margen total antes del cierre: {result['margen_total_btc']:.8f} BTC\n"
        f"BTC final estimado (margen + PnL): {result['btc_final_estimado']:.8f} BTC\n\n"
        f"_Estimación aproximada — no incluye funding/fees reales del exchange, "
        f"confirmá el resultado real ahí. Balas usadas antes de cerrar: "
        f"{result['balas_usadas_anteriores']}. Para arrancar de nuevo: "
        f"\"/saylor_iniciar <capital_total>\"._"
    )


def handle_message(text):
    """
    Punto de entrada llamado desde bot_btc_h4.py para todo lo que no sea un
    comando de la capa de trading. Devuelve el texto a responder por
    Telegram, o None si el mensaje no correspondía a esta estrategia.
    """
    stripped = text.strip()
    lower = stripped.lower()

    if lower.startswith("/saylor_iniciar"):
        partes = stripped.split()
        if len(partes) < 2:
            return "Uso: /saylor_iniciar <capital_total_usd>\nEj: /saylor_iniciar 8000"
        try:
            capital = parse_price_ar(partes[1])
        except ValueError:
            return "No pude leer el capital — uso: /saylor_iniciar <capital_total_usd>"
        state = load_saylor_state()
        try:
            precio_actual = get_current_btc_price()
        except Exception:
            precio_actual = None
        result = iniciar_estrategia(state, capital, precio_actual)
        if result["ok"]:
            save_saylor_state(state)
        return format_inicio_message(result)

    if lower.startswith("/saylor_cerrar"):
        partes = stripped.split()
        if len(partes) < 2:
            return "Uso: /saylor_cerrar <precio_btc_actual>\nEj: /saylor_cerrar 92000"
        try:
            precio = parse_price_ar(partes[1])
        except ValueError:
            return "No pude leer el precio — uso: /saylor_cerrar <precio_btc_actual>"
        state = load_saylor_state()
        result = cerrar_estrategia(state, precio)
        if result["ok"]:
            save_saylor_state(state)
        return format_cierre_message(result)

    if lower in ("/saylor", "/saylor_estado", "/posicion"):
        state = load_saylor_state()
        try:
            price = get_current_btc_price()
        except Exception:
            price = None
        return format_status_message(state, price)

    if lower in ("/saylor_chequeo", "/chequeo"):
        state = load_saylor_state()
        try:
            price = get_current_btc_price()
        except Exception as e:
            return f"⚠️ No pude traer el precio de BTC ahora mismo ({e}). Probá de nuevo en un rato."
        return format_daily_check_message(state, price)

    if lower in ("/deshacer", "/saylor_deshacer"):
        state = load_saylor_state()
        deshecho = deshacer_ultima_recarga(state)
        if deshecho:
            save_saylor_state(state)
            return f"↩️ Deshecho {deshecho.get('id', '?')} — volvió al estado anterior a esa operación."
        return "No hay nada para deshacer (no hay una carga previa registrada)."

    parsed = parse_recarga_text(stripped)
    if parsed:
        balas, precio, es_margen_extra = parsed
        state = load_saylor_state()
        result = confirmar_recarga(state, balas, precio, es_margen_extra)
        if result["ok"]:
            save_saylor_state(state)
        return format_confirmacion_message(result)

    return None


def run_daily_check():
    """Manda el chequeo diario por Telegram, con un botón "Ya la cargué" para
    confirmar la recarga sugerida sin tener que escribir todo el texto libre
    (solo hace falta responder con el precio). Es de solo lectura sobre
    saylor_state.json — el estado solo cambia cuando Marcos confirma
    (por botón+precio, o por texto libre como antes)."""
    # Import local para evitar import circular (bot_btc_h4 importa este módulo).
    from bot_btc_h4 import send_telegram_message, build_confirm_ms_keyboard

    state = load_saylor_state()
    price = get_current_btc_price()
    texto = format_daily_check_message(state, price)

    promedio_inverso = calcular_promedio_inverso(
        state.get("posicion_usd_acumulado", 0.0), state.get("posicion_btc_acumulado", 0.0)
    )
    keyboard = None
    if promedio_inverso is not None:
        roi = estimar_roi_btc(price, promedio_inverso)
        balas_posicion, balas_margen = balas_a_agregar(roi)
        keyboard = build_confirm_ms_keyboard(balas_posicion, balas_margen)

    send_telegram_message(texto, reply_markup=keyboard)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--daily-check", action="store_true", help="Manda el chequeo diario y sale")
    args = parser.parse_args()
    if args.daily_check:
        run_daily_check()


if __name__ == "__main__":
    main()
