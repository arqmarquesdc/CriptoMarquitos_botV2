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
from datetime import datetime, timezone, date

import requests

from price_utils import parse_price_ar

CAPITAL_TOTAL = 8000.0
MAX_BALAS = 30
BALA_SIZE = round(CAPITAL_TOTAL / MAX_BALAS, 2)  # 266.67
LEVERAGE = 5
TAKE_PROFIT_MIN_PCT = 15
TAKE_PROFIT_MAX_PCT = 20
LIMITES_TABLA = [0, -5, -10, -20, -40]  # umbrales de la tabla de recarga, para el aviso "cerca de un límite"
CERCA_LIMITE_PCT = 1.0  # a menos de 1 punto porcentual de un límite, sugerir confirmar el ROI real

SAYLOR_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "saylor_state.json")
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"


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
        with open(SAYLOR_STATE_FILE, "r") as f:
            return json.load(f)
    # Estado vacío por defecto — hay que sembrarlo con los datos reales antes
    # de usarlo (ver saylor_state.json / README).
    return {
        "start_date": None,
        "balas_usadas": 0,
        "posicion_usd_acumulado": 0.0,
        "posicion_btc_acumulado": 0.0,
        "margen_extra_btc_acumulado": 0.0,
        "log": [],
        "_previous_snapshot": None,
    }


def save_saylor_state(state):
    with open(SAYLOR_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


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
            "⚠️ *Estrategia Saylor BTC* — todavía no tengo un promedio cargado. "
            "Mandame la carga inicial, ej. \"Metí 2 balas a 77450\", para arrancar "
            "el seguimiento."
        )

    roi = estimar_roi_btc(price, promedio_inverso)
    balas_posicion, balas_margen = balas_a_agregar(roi)
    total_balas_hoy = balas_posicion + balas_margen
    balas_usd_hoy = round(total_balas_hoy * BALA_SIZE, 2)
    btc_a_depositar_hoy = round(balas_usd_hoy / price, 8)
    balas_restantes = MAX_BALAS - balas_usadas - total_balas_hoy

    avisos = []
    if cerca_de_limite(roi):
        avisos.append("⚠️ El ROI está cerca de un límite de la tabla — confirmá el ROI real del exchange antes de recargar.")
    if balas_restantes < 0:
        avisos.append(f"🛑 Esta recarga superaría el límite de {MAX_BALAS} balas / USD {CAPITAL_TOTAL:,.0f}. Revisá antes de ejecutar.")
    elif balas_restantes <= 3:
        avisos.append(f"⚠️ Quedan pocas balas ({balas_restantes}) para futuras recargas.")

    dia_txt = f"Día {dia}" if dia is not None else "Día s/d"
    recarga_txt = f"+{balas_posicion} balas a la posición"
    if balas_margen:
        margen_extra_btc = round(balas_margen * BALA_SIZE / price, 8)
        recarga_txt += (f" y +{balas_margen} directo al margen (situación crítica — "
                         f"≈{margen_extra_btc:.8f} BTC extra, no suma exposición, solo colchón)")

    lines = [
        f"📅 *Estrategia Saylor BTC — {dia_txt}*",
        f"Precio BTC/USD actual: ${price:,.2f} (Kraken)",
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
    if price and promedio_inverso:
        roi = estimar_roi_btc(price, promedio_inverso)
        lines.append(f"ROI estimado ahora: {roi:+.2f}% (BTC a ${price:,.2f})")
    return "\n".join(lines)


_BALAS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*balas?", re.IGNORECASE)
_PRECIO_RE = re.compile(
    r"(?:a|precio(?:\s+de)?|en)\s*(?:usd\$?|u\$s|\$)?\s*([\d]{1,3}(?:[.,]\d{2,3})*(?:[.,]\d+)?)",
    re.IGNORECASE,
)
_MARGEN_EXTRA_HINT_RE = re.compile(r"directo al margen|solo margen|margen extra|margen colch[oó]n", re.IGNORECASE)


def parse_recarga_text(text):
    """
    Devuelve (balas, precio, es_margen_extra) si el texto parece una
    confirmación de recarga ("Metí 2 balas a 77.450", "3 balas directo al
    margen a 76900"), o None si no matchea el patrón esperado.
    """
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
        return {
            "ok": False,
            "motivo": (f"Esto llevaría el total a {balas_previas + balas_confirmadas} balas, "
                       f"por encima del límite de {MAX_BALAS} (USD {CAPITAL_TOTAL:,.0f}). "
                       f"No se guardó — revisá los números."),
        }

    state["_previous_snapshot"] = copy.deepcopy({k: v for k, v in state.items() if k != "_previous_snapshot"})

    balas_usd = round(balas_confirmadas * BALA_SIZE, 2)
    btc_depositado = round(balas_usd / precio_confirmado, 8)

    if es_margen_extra:
        state["margen_extra_btc_acumulado"] = round(state.get("margen_extra_btc_acumulado", 0.0) + btc_depositado, 8)
    else:
        state["posicion_usd_acumulado"] = round(state.get("posicion_usd_acumulado", 0.0) + balas_usd, 2)
        state["posicion_btc_acumulado"] = round(state.get("posicion_btc_acumulado", 0.0) + btc_depositado, 8)

    state["balas_usadas"] = balas_previas + balas_confirmadas
    if not state.get("start_date"):
        state["start_date"] = date.today().isoformat()

    state.setdefault("log", []).append({
        "fecha": datetime.now(timezone.utc).isoformat(),
        "balas_confirmadas": balas_confirmadas,
        "precio_confirmado": precio_confirmado,
        "btc_depositado": btc_depositado,
        "tipo": "margen_extra" if es_margen_extra else "posicion",
    })

    promedio_inverso = calcular_promedio_inverso(state["posicion_usd_acumulado"], state["posicion_btc_acumulado"])

    return {
        "ok": True,
        "tipo": "margen_extra" if es_margen_extra else "posicion",
        "btc_depositado": btc_depositado,
        "promedio_inverso": promedio_inverso,
        "margen_total_btc": _margen_total_btc(state),
        "balas_usadas": state["balas_usadas"],
        "balas_restantes": MAX_BALAS - state["balas_usadas"],
    }


def deshacer_ultima_recarga(state):
    snapshot = state.get("_previous_snapshot")
    if not snapshot:
        return False
    restored = copy.deepcopy(snapshot)
    state.clear()
    state.update(restored)
    state["_previous_snapshot"] = None
    return True


def format_confirmacion_message(result):
    if not result["ok"]:
        return f"🛑 {result['motivo']}"

    if result["tipo"] == "margen_extra":
        return (
            f"✅ Listo, registrado como margen extra (colchón, no suma exposición).\n"
            f"BTC depositado ahora: {result['btc_depositado']:.8f} BTC\n"
            f"Margen BTC total (posición + extra): {result['margen_total_btc']:.8f} BTC\n"
            f"Balas usadas: {result['balas_usadas']}/{MAX_BALAS} (restantes: {result['balas_restantes']})\n\n"
            f"_Si algo no cierra, mandá \"/deshacer\" para revertir esta última carga._"
        )

    promedio_txt = f"${result['promedio_inverso']:,.2f}" if result["promedio_inverso"] else "s/d"
    return (
        f"✅ Listo, registrado.\n"
        f"BTC depositado ahora: {result['btc_depositado']:.8f} BTC\n"
        f"Nuevo promedio (contrato inverso): {promedio_txt}\n"
        f"Margen BTC total: {result['margen_total_btc']:.8f} BTC\n"
        f"Balas usadas: {result['balas_usadas']}/{MAX_BALAS} (restantes: {result['balas_restantes']})\n\n"
        f"_Si algo no cierra, mandá \"/deshacer\" para revertir esta última carga._"
    )


def handle_message(text):
    """
    Punto de entrada llamado desde bot_btc_h4.py para todo lo que no sea un
    comando de la capa de trading. Devuelve el texto a responder por
    Telegram, o None si el mensaje no correspondía a esta estrategia.
    """
    stripped = text.strip()
    lower = stripped.lower()

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
        if deshacer_ultima_recarga(state):
            save_saylor_state(state)
            return "↩️ Deshecho — volvió al estado anterior a la última carga confirmada."
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
