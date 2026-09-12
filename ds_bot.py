"""
Bot de seguimiento de la estrategia "Doble Saylor" (DS): long apalancado en
MSTR (ahora "Strategy Inc.", mismo ticker), en Bitget. 2 balas únicas para
toda la estrategia (no 30 como MS) — capital total / 2. Cada bala se carga
20% como monto de trade (con el apalancamiento configurado, informativo) y
80% como margen adicional extra, para alejar la liquidación.

Chequeo diario automático (ds_daily.yml, vía get_current_mstr_price/stooq):
si el rendimiento (ROE estimado, mismo criterio que muestra Bitget) cruza
-40%, manda un aviso 🔴 con un botón "Ya la sumé" — es la señal acordada con
Marcos para considerar la bala 2 (todavía no hay una regla de CUÁNDO usarla
más allá de ese umbral, eso lo decide él en el momento). Al tocar el botón,
bot_btc_h4.py le pide el precio y, apenas lo responde, se registra la bala
sola con registrar_bala (montos derivados de bala_size(), no hace falta
tipearlos). El mismo registro también se puede hacer a mano con
"/ds_bala2 <precio> [margen_usdt] [tamano_mstr]" si prefiere cargar los
datos reales del exchange.

Para evitar que un mensaje de DS se confunda con una confirmación de MS (los
dos usan la palabra "bala"), las interacciones con DS son por comando
explícito (/ds, /ds_bala2 ...), nunca texto libre — bot_btc_h4.py chequea
estos comandos ANTES de pasarle el mensaje a saylor_bot, así que un "/ds..."
nunca llega a intentar parsearse como MS.
"""

import os
import json
import copy
import requests
from datetime import datetime, timezone

from price_utils import parse_price_ar

DS_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ds_state.json")
STOOQ_URL = "https://stooq.com/q/l/"

CAPITAL_TOTAL_DEFAULT = 1200.0
MAX_BALAS = 2
TRADE_SPLIT_PCT = 20
MARGIN_SPLIT_PCT = 80
LEVERAGE = 10  # apalancamiento configurado en Bitget para el TRADE (no contra el margen
                # real de la posición, que incluye el colchón extra del 80%). Con la bala 1
                # real (entrada 131.36, tamaño 8.99 MSTR): notional ≈ 8.99*131.36 ≈ $1.181, y
                # trade_usd_teorico*leverage = 120*10 = $1.200 — coincide dentro del margen de
                # redondeo/comisiones. Por eso el "rendimiento" de DS se define como el ROE
                # que muestra el exchange: variación%(precio) × 10, NO como PnL/margen_real
                # (ese último da un número mucho más chico porque el margen real incluye el
                # colchón, y no es lo que Marcos ve en pantalla como "rendimiento").
ALERTA_RENDIMIENTO_PCT = -40.0  # umbral pedido por Marcos para el aviso automático


def estimar_rendimiento_ds(precio_actual, precio_entrada, leverage=LEVERAGE):
    """Rendimiento % lineal (no es contrato inverso, es un margen normal en USDT
    sobre MSTR) — mismo cálculo que el ROE que muestra Bitget."""
    if not precio_entrada:
        return None
    variacion_pct = (precio_actual - precio_entrada) / precio_entrada * 100
    return variacion_pct * leverage


def precio_entrada_ponderado(state):
    """Promedio ponderado (lineal) de las balas cargadas, por si algún día hay
    más de una a distinto precio."""
    log = state.get("log", [])
    if not log:
        return None
    total_peso = sum(e["bala_size_usd"] for e in log)
    if not total_peso:
        return None
    return sum(e["bala_size_usd"] * e["precio_entrada"] for e in log) / total_peso


def get_current_mstr_price():
    """
    Precio de MSTR vía stooq.com (CSV público, sin API key). AVISO: no pude
    verificar este endpoint en vivo esta sesión (falla de sandbox) — probalo
    primero con "/ds_chequeo" antes de confiar en la alerta automática de
    -40%, y avisame si el formato de la respuesta no coincide.
    """
    resp = requests.get(STOOQ_URL, params={"s": "mstr.us", "f": "sd2t2ohlcv", "e": "csv"}, timeout=15)
    resp.raise_for_status()
    lines = resp.text.strip().splitlines()
    if len(lines) < 2:
        raise RuntimeError(f"Respuesta inesperada de stooq: {resp.text[:200]!r}")
    header = lines[0].split(",")
    values = lines[1].split(",")
    row = dict(zip(header, values))
    close = row.get("Close")
    if not close or close in ("N/D", ""):
        raise RuntimeError(f"stooq no devolvió un precio válido: {row}")
    return float(close)


def load_ds_state():
    if os.path.exists(DS_STATE_FILE):
        with open(DS_STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "capital_total": CAPITAL_TOTAL_DEFAULT,
        "balas_usadas": 0,
        "log": [],
        "_previous_snapshot": None,
    }


def save_ds_state(state):
    with open(DS_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def bala_size(state):
    return round(state.get("capital_total", CAPITAL_TOTAL_DEFAULT) / MAX_BALAS, 2)


def registrar_bala(state, precio_entrada, margen_usdt=None, tamano_mstr=None, liquidacion_exchange=None):
    balas_previas = state.get("balas_usadas", 0)
    if balas_previas >= MAX_BALAS:
        return {"ok": False, "motivo": f"Ya se usaron las {MAX_BALAS} balas de DS, no hay más para cargar."}

    state["_previous_snapshot"] = copy.deepcopy({k: v for k, v in state.items() if k != "_previous_snapshot"})

    size = bala_size(state)
    trade_usd = round(size * TRADE_SPLIT_PCT / 100, 2)
    margen_usd_teorico = round(size * MARGIN_SPLIT_PCT / 100, 2)

    entry = {
        "fecha": datetime.now(timezone.utc).isoformat(),
        "bala": balas_previas + 1,
        "precio_entrada": precio_entrada,
        "bala_size_usd": size,
        "trade_usd_teorico": trade_usd,
        "margen_usd_teorico": margen_usd_teorico,
        "margen_usdt_real": margen_usdt,
        "tamano_mstr_real": tamano_mstr,
        "liquidacion_exchange": liquidacion_exchange,
    }
    state.setdefault("log", []).append(entry)
    state["balas_usadas"] = balas_previas + 1

    return {
        "ok": True,
        "entry": entry,
        "balas_usadas": state["balas_usadas"],
        "balas_restantes": MAX_BALAS - state["balas_usadas"],
    }


def deshacer_ultima_bala(state):
    snapshot = state.get("_previous_snapshot")
    if not snapshot:
        return False
    restored = copy.deepcopy(snapshot)
    state.clear()
    state.update(restored)
    state["_previous_snapshot"] = None
    return True


def format_status_message(state, precio_actual=None):
    balas_usadas = state.get("balas_usadas", 0)
    size = bala_size(state)
    lines = [
        "📊 *Estado — Estrategia DS (Doble Saylor, MSTR, Bitget)*",
        f"Capital total: USD {state.get('capital_total', CAPITAL_TOTAL_DEFAULT):,.2f} en {MAX_BALAS} balas de USD {size:,.2f}",
        f"Balas usadas: {balas_usadas}/{MAX_BALAS}",
    ]
    for e in state.get("log", []):
        liq = e.get("liquidacion_exchange")
        lines.append(
            f"— Bala {e['bala']}: entrada ${e['precio_entrada']:,.2f} | "
            f"margen real: {e.get('margen_usdt_real') if e.get('margen_usdt_real') is not None else 's/d'} USDT | "
            f"tamaño: {e.get('tamano_mstr_real') if e.get('tamano_mstr_real') is not None else 's/d'} MSTR | "
            f"liq. (exchange): {'$' + format(liq, ',.2f') if liq else 's/d'}"
        )
    if precio_actual is not None:
        precio_prom = precio_entrada_ponderado(state)
        rendimiento = estimar_rendimiento_ds(precio_actual, precio_prom)
        if rendimiento is not None:
            aviso = " 🔴 ¡Por debajo del umbral de -40%!" if rendimiento <= ALERTA_RENDIMIENTO_PCT else ""
            lines.append(
                f"\nPrecio actual: ${precio_actual:,.2f} (entrada prom.: ${precio_prom:,.2f})\n"
                f"Rendimiento estimado (ROE, x{LEVERAGE}): {rendimiento:+.1f}%{aviso}"
            )
    if balas_usadas < MAX_BALAS:
        lines.append(
            f"\nBala {balas_usadas + 1} pendiente — todavía no hay regla automática de cuándo "
            f"usarla, se define caso a caso con Marcos. Para cargarla: "
            f"\"/ds_bala2 <precio_entrada> [margen_usdt] [tamano_mstr]\"."
        )
    return "\n".join(lines)


def format_alerta_rendimiento(state, precio_actual, rendimiento):
    precio_prom = precio_entrada_ponderado(state)
    return (
        f"🔴 *Alerta DS — rendimiento en {rendimiento:+.1f}%*\n"
        f"Precio actual: ${precio_actual:,.2f} (entrada prom.: ${precio_prom:,.2f})\n"
        f"Cruzó el umbral de {ALERTA_RENDIMIENTO_PCT:.0f}% que definiste. "
        f"Todavía no hay una regla automática para la bala 2 — es tu turno de decidir si conviene recargar ahora."
    )


def check_rendimiento_alert(state, precio_actual):
    """
    Devuelve el texto de alerta si el rendimiento cruzó -40% por primera vez
    desde la última vez que se avisó (evita re-alertar en cada corrida
    mientras el precio se mantenga por debajo del umbral), o None si no hay
    nada para avisar. Actualiza state["_alerta_rendimiento_activa"].
    """
    if state.get("balas_usadas", 0) == 0:
        return None
    precio_prom = precio_entrada_ponderado(state)
    rendimiento = estimar_rendimiento_ds(precio_actual, precio_prom)
    if rendimiento is None:
        return None

    ya_activa = state.get("_alerta_rendimiento_activa", False)
    cruzo_umbral = rendimiento <= ALERTA_RENDIMIENTO_PCT

    if cruzo_umbral and not ya_activa:
        state["_alerta_rendimiento_activa"] = True
        return format_alerta_rendimiento(state, precio_actual, rendimiento)
    if not cruzo_umbral and ya_activa:
        state["_alerta_rendimiento_activa"] = False
        return (
            f"🟢 DS — el rendimiento volvió a estar por encima de {ALERTA_RENDIMIENTO_PCT:.0f}% "
            f"(ahora: {rendimiento:+.1f}%)."
        )
    return None


def run_daily_check():
    # import local para evitar import circular (bot_btc_h4 importa ds_bot)
    from bot_btc_h4 import send_telegram_message, build_confirm_ds_keyboard

    state = load_ds_state()
    if state.get("balas_usadas", 0) == 0:
        return  # nada cargado todavía, no hay nada que chequear

    try:
        precio = get_current_mstr_price()
    except Exception as e:
        send_telegram_message(
            f"⚠️ DS: no pude obtener el precio de MSTR para el chequeo diario ({e}). "
            f"Probá \"/ds_chequeo <precio>\" a mano."
        )
        return

    aviso = check_rendimiento_alert(state, precio)
    save_ds_state(state)
    if aviso:
        keyboard = None
        balas_usadas = state.get("balas_usadas", 0)
        # El botón "Ya la sumé" solo tiene sentido en el aviso de cruce hacia
        # abajo (🔴, corresponde considerar la próxima bala), no en el de
        # recuperación (🟢) ni si ya no queda ninguna bala por cargar.
        if aviso.startswith("🔴") and balas_usadas < MAX_BALAS:
            keyboard = build_confirm_ds_keyboard(balas_usadas + 1)
        send_telegram_message(aviso, reply_markup=keyboard)


def format_registro_message(result):
    if not result.get("ok"):
        return f"🛑 {result.get('motivo', 'No se pudo registrar.')}"
    e = result["entry"]
    return (
        f"✅ Bala {e['bala']} de DS registrada.\n"
        f"Entrada: ${e['precio_entrada']:,.2f}\n"
        f"Tamaño teórico: ${e['bala_size_usd']:,.2f} ({TRADE_SPLIT_PCT}% trade / {MARGIN_SPLIT_PCT}% margen)\n"
        f"Balas usadas: {result['balas_usadas']}/{MAX_BALAS} (restantes: {result['balas_restantes']})"
    )


def handle_command(text):
    """
    Comandos explícitos únicamente. Devuelve el texto a responder, o None si
    el mensaje no era un comando de DS (para que bot_btc_h4.py siga probando
    con saylor_bot).
    """
    stripped = text.strip()
    lower = stripped.lower()

    if lower in ("/ds", "/ds_estado"):
        state = load_ds_state()
        precio_actual = None
        try:
            precio_actual = get_current_mstr_price()
        except Exception:
            pass
        return format_status_message(state, precio_actual)

    if lower.startswith("/ds_chequeo"):
        partes = stripped.split()
        state = load_ds_state()
        if len(partes) >= 2:
            try:
                precio_actual = parse_price_ar(partes[1])
            except ValueError:
                return "No pude leer el precio — uso: /ds_chequeo [precio] (si no ponés precio, intento buscarlo solo)"
        else:
            try:
                precio_actual = get_current_mstr_price()
            except Exception as e:
                return f"⚠️ No pude obtener el precio de MSTR automáticamente ({e}). Probá: /ds_chequeo <precio>"
        aviso = check_rendimiento_alert(state, precio_actual)
        save_ds_state(state)
        status = format_status_message(state, precio_actual)
        return status + (f"\n\n{aviso}" if aviso else "")

    if lower == "/ds_deshacer":
        state = load_ds_state()
        if deshacer_ultima_bala(state):
            save_ds_state(state)
            return "↩️ Deshecho — DS volvió al estado anterior a la última bala cargada."
        return "No hay nada para deshacer en DS."

    if lower.startswith("/ds_bala"):
        partes = stripped.split()
        if len(partes) < 2:
            return ("Uso: /ds_bala2 <precio> [margen_usdt] [tamano_mstr]\n"
                     "Ej: /ds_bala2 125.40 598.09 8.99")
        try:
            precio = parse_price_ar(partes[1])
            margen = parse_price_ar(partes[2]) if len(partes) > 2 else None
            tamano = parse_price_ar(partes[3]) if len(partes) > 3 else None
        except ValueError:
            return "No pude leer los números — revisá el formato: /ds_bala2 <precio> [margen_usdt] [tamano_mstr]"

        state = load_ds_state()
        result = registrar_bala(state, precio, margen, tamano)
        if result["ok"]:
            save_ds_state(state)
        return format_registro_message(result)

    return None


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--daily-check", action="store_true")
    args = parser.parse_args()
    if args.daily_check:
        run_daily_check()


if __name__ == "__main__":
    main()
