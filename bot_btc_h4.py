"""
Bot de alertas de compra/venta de Bitcoin (BTC/USD).

Capas de trading: el sesgo direccional y los niveles de entrada/stop/take
profit salen de la estructura en una temporalidad "de sesgo" — H4 para
trades de plazo más largo, H1 para trades de plazo intermedio (agregada para
que lleguen alertas con más frecuencia, sin bajar a M15 standalone: entrar
en 15 min con take profit cerca queda descartado a propósito). Cada sesgo
queda "pendiente" hasta que M15 confirma con su propio cruce EMA9/EMA21 en
la misma dirección — M15 solo afina el momento de entrada, no genera trades
propios. La idea pendiente se descarta si el precio en la temporalidad de
sesgo ya tocó el stop loss calculado, si aparece un cruce EMA contrario, o
(como backstop de seguridad) si pasa más tiempo del configurado sin
confirmación — medido contra el reloj real, no contra la última vela
cerrada (eso retrasaba el backstop hasta el doble del límite si coincidía
con el borde de una vela).

Capa de tendencia: avisos de cambio de régimen en Diario y Semanal (ver más
abajo), independientes de las capas de trading.

Gestión de riesgo: cada señal de trading trae una sugerencia de tamaño de
posición según la regla del 2% (Elder, Muñoz) expresada como múltiplo de tu
capital, con ajuste anti-martingala según pérdidas ya cerradas ese día en
la bitácora.

Interacción por Telegram: además de recibir alertas, se puede mandar
"/estado" en cualquier momento para saber si el bot sigue en línea, y
responder con los botones "Tomé el trade" / "No lo tomé" en cada señal de
trading para que quede registrado en la bitácora. Estas respuestas se
procesan sondeando la API de Telegram (getUpdates) en cada corrida del cron,
así que pueden demorar hasta el intervalo del cron (~15 min) en reflejarse —
no es instantáneo.

Envía las alertas por Telegram. Diseñado para correr gratis y en tiempo real
vía GitHub Actions (cron cada 15 minutos), pero también funciona en loop local.

Requisitos:
    pip install requests

Variables de entorno necesarias:
    TELEGRAM_BOT_TOKEN  -> token del bot (te lo da @BotFather)
    TELEGRAM_CHAT_ID    -> tu chat_id (te lo da @userinfobot)

Uso:
    python bot_btc_h4.py            # corre una vez y sale (ideal para cron/GitHub Actions)
    python bot_btc_h4.py --loop     # corre en loop continuo, revisando cada 1 minuto
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timezone

import requests

from price_utils import extract_price_from_text

try:
    import saylor_bot
except Exception as _saylor_import_error:  # no debe romper las alertas de trading si falla
    saylor_bot = None
    print(f"[AVISO] No se pudo importar saylor_bot: {_saylor_import_error}")

try:
    import ds_bot
except Exception as _ds_import_error:  # no debe romper las alertas de trading si falla
    ds_bot = None
    print(f"[AVISO] No se pudo importar ds_bot: {_ds_import_error}")

EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
RCI_PERIOD = 9            # período corto, clásico para detectar giros de momentum
DIVERGENCE_LOOKBACK = 30  # velas hacia atrás donde buscar divergencia precio/RCI
PIVOT_WINDOW = 3          # velas a cada lado para confirmar un pivot (swing) de precio
ATR_PERIOD = 14
SWING_LOOKBACK = 10       # velas hacia atrás para buscar el swing low/high estructural
ATR_STOP_MIN_MULT = 0.5   # el SL nunca queda a menos de 0.5 ATR (evita stops pegados al precio)
ATR_STOP_MAX_MULT = 3.0   # ni a más de 3 ATR (evita stops absurdamente amplios)
ATR_STOP_BUFFER_MULT = 0.1  # colchón extra más allá del swing, para no quedar exacto sobre el nivel
RISK_REWARD_RATIO = 2.0   # take profit = 2x la distancia del stop loss
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
TRADES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades.json")

# Gestión de riesgo (regla del 2%, Elder / Muñoz): tope de capital a arriesgar
# por operación. No conocemos tu capital real, así que la alerta lo expresa
# como un múltiplo de tu capital total en vez de un monto en dólares. Ajuste
# anti-martingala: por cada pérdida ya cerrada HOY en la misma capa, se reduce
# a la mitad respecto de la base (nunca se sube por ganancias del día), con un
# piso para que no llegue a un número inservible.
RISK_RULE_PCT = 2.0
RISK_RULE_MIN_PCT = 0.5

# Disparador adicional en H4/H1 (además del cruce EMA9/21): canal de regresión
# lineal. Solo cuenta como toque válido si además coincide con un swing
# high/low real reciente (confluencia) — reduce falsos positivos de un canal
# que sea solo matemática sin respaldo en la estructura real del precio.
CHANNEL_LOOKBACK = 40             # velas para ajustar la recta de regresión
CHANNEL_STD_MULT = 2.0            # ancho del canal, en desvíos estándar sobre la recta
CHANNEL_TOUCH_TOLERANCE_ATR = 0.3 # qué tan cerca del límite cuenta como "toque"
CHANNEL_SWING_CONFLUENCE_ATR = 1.0  # qué tan cerca debe estar el límite del canal de un swing real

# Capas de trading: sesgo direccional y niveles desde H4 o H1, entrada afinada
# con confirmación en M15 (ver docstring del módulo).
H4_CONFIG = {"label": "H4", "kraken_interval": 240}
H1_CONFIG = {"label": "H1", "kraken_interval": 60}
M15_CONFIG = {"label": "M15", "kraken_interval": 15}
H4_PENDING_MAX_HOURS = 4     # backstop de seguridad para H4: no es el criterio principal de invalidación
H1_PENDING_MAX_HOURS = 1.5   # ídem para H1 (vela más corta, backstop más corto)
BIAS_KLINES_LIMIT = 150      # margen extra sobre CHANNEL_LOOKBACK/EMA_CONTEXT_SLOW para que no
                              # falten velas después de descartar la última (aún en formación)

# EMA de contexto (las más usadas junto al 50/200): puramente informativo, se
# muestra en cada señal H4/H1 para que veas si el precio está alineado con
# una tendencia intermedia, pero NO descarta ni filtra ninguna señal.
EMA_CONTEXT_FAST = 50
EMA_CONTEXT_SLOW = 100

# Apalancamiento sugerido (solo informativo en la alerta, no ejecuta nada).
# Se limita a MAX_LEVERAGE, y además se recorta si el stop loss está muy
# lejos, para que tocar el SL no te deje al borde de la liquidación: el SL
# nunca debería representar más de LEVERAGE_SAFETY_FRACTION de la distancia
# a la liquidación estimada.
MAX_LEVERAGE = 5
LEVERAGE_SAFETY_FRACTION = 0.5

# Capa B: cambios de tendencia de fondo, en Diario. Cruce EMA50/EMA200
# (golden cross / death cross) + RCI de período largo como confirmación.
TREND_TIMEFRAME = {"label": "D1", "kraken_interval": 1440}
EMA_TREND_FAST = 50
EMA_TREND_SLOW = 200
RCI_TREND_PERIOD = 26

# Capa C: visión de largo plazo (meses/años), en Semanal. Kraken no ofrece velas
# mensuales nativas, así que la Semanal es la temporalidad práctica más larga
# disponible. EMA10/EMA30 semanal equivale, en orden de magnitud, a EMA50/EMA200
# diario, pero mirando la tendencia de fondo a escala de meses.
LONGTERM_TIMEFRAME = {"label": "W1", "kraken_interval": 10080}
EMA_LT_FAST = 10
EMA_LT_SLOW = 30
RCI_LT_PERIOD = 12

# Si un check (H4c, H1c, D1_trend, W1_trend) falla esta cantidad de corridas
# SEGUIDAS, se manda un aviso por Telegram (además de quedar en el log de
# GitHub Actions) — evita spam por hipos de red transitorios, pero avisa si
# algo queda roto de verdad.
ERROR_ALERT_THRESHOLD = 3

# Marcos pidió (12/09) apagar las alertas de trading de corto plazo H4/H1 y
# las de cambio de tendencia D1/W1 — por ahora solo quiere recibir MS y DS.
# El código de esa capa queda intacto (por si algún día se reactiva), esta
# bandera simplemente la desactiva sin tocar nada más: no corre los checks
# H4c/H1c/D1_trend/W1_trend ni manda el reporte semanal (que es sobre esa
# capa). Los comandos/botones de MS y DS y el resto del bot siguen andando
# igual, process_telegram_updates no depende de esto.
ALERTAS_TRADING_ACTIVAS = False

# Nota: la API de Binance (api.binance.com) devuelve error 451 (bloqueo legal
# por región) para las IPs de los runners de GitHub Actions, así que usamos
# la API pública de Kraken, que no tiene esa restricción.
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_GET_UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"
TELEGRAM_ANSWER_CALLBACK_URL = "https://api.telegram.org/bot{token}/answerCallbackQuery"


def get_klines(pair="XBTUSD", interval_minutes=240, limit=100):
    """Trae velas históricas de BTC/USD desde Kraken (endpoint público, sin API key)."""
    params = {"pair": pair, "interval": interval_minutes}
    resp = requests.get(KRAKEN_OHLC_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken API error: {data['error']}")

    result = data["result"]
    pair_key = next(k for k in result.keys() if k != "last")
    raw = result[pair_key][-limit:]

    interval_ms = interval_minutes * 60 * 1000
    candles = []
    for row in raw:
        open_time = int(row[0]) * 1000
        candles.append({
            "open_time": open_time,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[6]),
            "close_time": open_time + interval_ms,
        })
    return candles


def only_closed_candles(candles):
    """Descarta la última vela si todavía está en formación (close_time futuro)."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if candles and candles[-1]["close_time"] > now_ms:
        return candles[:-1]
    return candles


def calculate_ema(values, period):
    ema = [None] * len(values)
    if len(values) < period:
        return ema
    k = 2 / (period + 1)
    sma = sum(values[:period]) / period
    ema[period - 1] = sma
    for i in range(period, len(values)):
        ema[i] = values[i] * k + ema[i - 1] * (1 - k)
    return ema


def calculate_rsi(values, period=RSI_PERIOD):
    rsi = [None] * len(values)
    if len(values) <= period:
        return rsi

    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi[period] = 100 - (100 / (1 + (avg_gain / avg_loss))) if avg_loss != 0 else 100

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
        rsi[i + 1] = 100 - (100 / (1 + rs))

    return rsi


def calculate_rci(candles, period=RCI_PERIOD):
    """
    Rank Correlation Index: mide qué tan bien correlacionan el orden temporal y el
    ranking de precios en la ventana. +100 = tendencia alcista perfecta, -100 =
    bajista perfecta, cerca de 0 = sin tendencia clara.
    """
    closes = [c["close"] for c in candles]
    n = len(closes)
    rci = [None] * n
    if n < period:
        return rci

    for i in range(period - 1, n):
        window = closes[i - period + 1:i + 1]  # más viejo -> más nuevo
        sorted_idx = sorted(range(period), key=lambda j: -window[j])  # precio descendente
        price_rank = [0] * period
        for rank, j in enumerate(sorted_idx, start=1):
            price_rank[j] = rank

        d_sq_sum = 0
        for j in range(period):
            date_rank = period - j  # más nuevo (j=period-1) => rank 1
            d = date_rank - price_rank[j]
            d_sq_sum += d * d

        rci[i] = (1 - 6 * d_sq_sum / (period * (period ** 2 - 1))) * 100

    return rci


def find_pivots(values, window=PIVOT_WINDOW):
    """Pivots (swing highs/lows) confirmados: el valor central es el máx/mín de su ventana."""
    n = len(values)
    highs, lows = [], []
    for i in range(window, n - window):
        seg = values[i - window:i + window + 1]
        if None in seg or values[i] is None:
            continue
        if values[i] == max(seg):
            highs.append((i, values[i]))
        if values[i] == min(seg):
            lows.append((i, values[i]))
    return highs, lows


def detect_rci_divergence(candles, rci, lookback=DIVERGENCE_LOOKBACK, window=PIVOT_WINDOW):
    """
    Divergencia bajista: precio marca un máximo más alto pero el RCI marca uno más
    bajo (el impulso alcista se está agotando). Divergencia alcista: análogo con
    mínimos. Compara los dos pivots de precio más recientes dentro del lookback.

    Los pivots se detectan sobre la serie completa (necesitan `window` velas de
    contexto a cada lado para confirmarse) y recién después se filtran los que
    caen dentro del lookback — recortar la serie antes de buscar pivots corta
    ese contexto y puede perder pivots cerca del borde.
    """
    n = len(candles)
    closes = [c["close"] for c in candles]
    cutoff = max(0, n - lookback)

    all_highs, all_lows = find_pivots(closes, window)
    price_highs = [(i, p) for i, p in all_highs if i >= cutoff]
    price_lows = [(i, p) for i, p in all_lows if i >= cutoff]

    if len(price_highs) >= 2:
        (i1, p1), (i2, p2) = price_highs[-2], price_highs[-1]
        r1, r2 = rci[i1], rci[i2]
        if r1 is not None and r2 is not None and p2 > p1 and r2 < r1:
            return "bearish"

    if len(price_lows) >= 2:
        (i1, p1), (i2, p2) = price_lows[-2], price_lows[-1]
        r1, r2 = rci[i1], rci[i2]
        if r1 is not None and r2 is not None and p2 < p1 and r2 > r1:
            return "bullish"

    return None


def calculate_atr(candles, period=ATR_PERIOD):
    """ATR (Average True Range) de Wilder, para dimensionar el stop según la volatilidad real."""
    n = len(candles)
    tr = [None] * n
    for i in range(1, n):
        high, low = candles[i]["high"], candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr[i] = max(high - low, abs(high - prev_close), abs(low - prev_close))

    atr = [None] * n
    if n <= period:
        return atr

    atr[period] = sum(tr[1:period + 1]) / period
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def swing_low(candles, end_idx, lookback=SWING_LOOKBACK):
    start = max(0, end_idx - lookback + 1)
    return min(c["low"] for c in candles[start:end_idx + 1])


def swing_high(candles, end_idx, lookback=SWING_LOOKBACK):
    start = max(0, end_idx - lookback + 1)
    return max(c["high"] for c in candles[start:end_idx + 1])


def calculate_trade_levels(candles, i, entry, atr_val, direction):
    """
    Stop loss 'lógico': más allá del swing low/high reciente (estructura de mercado,
    no un número arbitrario), con un colchón y límites basados en ATR para que no
    quede ni pegado al precio ni absurdamente lejos. Take profit a partir de un
    ratio riesgo:beneficio fijo, como haría un trader disciplinado.
    """
    if direction == "BUY":
        structural = swing_low(candles, i)
        raw_sl = structural - ATR_STOP_BUFFER_MULT * atr_val
        dist = entry - raw_sl
        dist = min(max(dist, ATR_STOP_MIN_MULT * atr_val), ATR_STOP_MAX_MULT * atr_val)
        sl = entry - dist
        tp = entry + RISK_REWARD_RATIO * dist
    else:  # SELL
        structural = swing_high(candles, i)
        raw_sl = structural + ATR_STOP_BUFFER_MULT * atr_val
        dist = raw_sl - entry
        dist = min(max(dist, ATR_STOP_MIN_MULT * atr_val), ATR_STOP_MAX_MULT * atr_val)
        sl = entry + dist
        tp = entry - RISK_REWARD_RATIO * dist

    return {"stop_loss": sl, "take_profit": tp, "risk": dist, "risk_reward": RISK_REWARD_RATIO}


def detect_signal(candles):
    """
    Devuelve ('BUY'|'SELL'|None, info_dict) evaluando la vela recién cerrada contra
    la anterior, para detectar un cruce fresco de EMA9/EMA21 filtrado por RSI.
    Si hay señal, agrega niveles de entrada/stop loss/take profit.
    """
    closes = [c["close"] for c in candles]
    ema_fast = calculate_ema(closes, EMA_FAST)
    ema_slow = calculate_ema(closes, EMA_SLOW)
    rsi = calculate_rsi(closes, RSI_PERIOD)
    atr = calculate_atr(candles, ATR_PERIOD)
    rci = calculate_rci(candles, RCI_PERIOD)

    i = len(closes) - 1  # última vela cerrada
    prev = i - 1

    if None in (ema_fast[i], ema_slow[i], ema_fast[prev], ema_slow[prev], rsi[i], atr[i], rci[i]):
        return None, {}

    crossed_up = ema_fast[prev] <= ema_slow[prev] and ema_fast[i] > ema_slow[i]
    crossed_down = ema_fast[prev] >= ema_slow[prev] and ema_fast[i] < ema_slow[i]
    divergence = detect_rci_divergence(candles, rci)

    entry = closes[i]
    info = {
        "close_time": candles[i]["close_time"],
        "price": entry,
        "ema_fast": round(ema_fast[i], 2),
        "ema_slow": round(ema_slow[i], 2),
        "rsi": round(rsi[i], 2),
        "atr": round(atr[i], 2),
        "rci": round(rci[i], 2),
        "divergence": divergence,
    }

    direction = None
    if crossed_up and rsi[i] < 70:
        direction = "BUY"
    elif crossed_down and rsi[i] > 30:
        direction = "SELL"
    else:
        return None, info

    # Filtro de confluencia RCI: si hay una divergencia que contradice la señal
    # (ej. EMA dice comprar pero el RCI muestra agotamiento alcista), se descarta
    # para evitar entrar justo antes de un giro. Si la confirma, se marca en el mensaje.
    if direction == "BUY" and divergence == "bearish":
        info["discarded_reason"] = "divergencia bajista de RCI contradice el cruce alcista"
        return None, info
    if direction == "SELL" and divergence == "bullish":
        info["discarded_reason"] = "divergencia alcista de RCI contradice el cruce bajista"
        return None, info

    info["divergence_confirms"] = (
        (direction == "BUY" and divergence == "bullish") or
        (direction == "SELL" and divergence == "bearish")
    )

    levels = calculate_trade_levels(candles, i, entry, atr[i], direction)
    info.update({
        "entry": round(entry, 2),
        "stop_loss": round(levels["stop_loss"], 2),
        "take_profit": round(levels["take_profit"], 2),
        "risk_reward": levels["risk_reward"],
    })
    return direction, info


def calculate_regression_channel(closes, lookback=CHANNEL_LOOKBACK, std_mult=CHANNEL_STD_MULT):
    """
    Canal de regresión lineal: ajusta una recta por mínimos cuadrados sobre las
    últimas `lookback` velas y traza límites paralelos a `std_mult` desvíos
    estándar de esa recta. Es el equivalente objetivo/automático a trazar un
    canal de tendencia a mano.
    """
    if len(closes) < lookback:
        return None

    window = closes[-lookback:]
    n = lookback
    mean_x = (n - 1) / 2
    mean_y = sum(window) / n

    num = sum((i - mean_x) * (window[i] - mean_y) for i in range(n))
    den = sum((i - mean_x) ** 2 for i in range(n))
    slope = num / den if den != 0 else 0
    intercept = mean_y - slope * mean_x

    line_last = slope * (n - 1) + intercept
    residuals = [window[i] - (slope * i + intercept) for i in range(n)]
    std = (sum(r * r for r in residuals) / n) ** 0.5

    return {
        "mid": line_last,
        "upper": line_last + std_mult * std,
        "lower": line_last - std_mult * std,
        "slope": slope,
        "std": std,
    }


def detect_channel_touch(candles):
    """
    Disparador adicional (no reemplaza el cruce EMA): toque del límite del
    canal de regresión, validado contra un swing high/low real reciente. Toque
    en el límite superior + confluencia con un swing high -> sesgo SELL
    (rechazo en resistencia). Toque en el límite inferior + confluencia con un
    swing low -> sesgo BUY (rechazo en soporte). Mismo filtro de divergencia
    RCI que usa el cruce EMA.
    """
    closes = [c["close"] for c in candles]
    if len(closes) < CHANNEL_LOOKBACK + 1:
        return None, {}

    channel = calculate_regression_channel(closes)
    if not channel:
        return None, {}

    atr = calculate_atr(candles, ATR_PERIOD)
    rci = calculate_rci(candles, RCI_PERIOD)
    rsi = calculate_rsi(closes, RSI_PERIOD)

    i = len(closes) - 1
    if atr[i] is None or rci[i] is None:
        return None, {}

    last = candles[i]
    tol = CHANNEL_TOUCH_TOLERANCE_ATR * atr[i]
    touched_upper = last["high"] >= channel["upper"] - tol
    touched_lower = last["low"] <= channel["lower"] + tol

    base_info = {"channel_upper": round(channel["upper"], 2), "channel_lower": round(channel["lower"], 2)}

    direction = None
    if touched_upper and not touched_lower:
        sw_high = swing_high(candles, i, lookback=SWING_LOOKBACK)
        if abs(sw_high - channel["upper"]) <= CHANNEL_SWING_CONFLUENCE_ATR * atr[i]:
            direction = "SELL"
    elif touched_lower and not touched_upper:
        sw_low = swing_low(candles, i, lookback=SWING_LOOKBACK)
        if abs(sw_low - channel["lower"]) <= CHANNEL_SWING_CONFLUENCE_ATR * atr[i]:
            direction = "BUY"

    if not direction:
        return None, base_info

    divergence = detect_rci_divergence(candles, rci)
    entry = closes[i]
    info = dict(base_info)
    info.update({
        "close_time": last["close_time"],
        "price": entry,
        "rsi": round(rsi[i], 2) if rsi[i] is not None else None,
        "atr": round(atr[i], 2),
        "rci": round(rci[i], 2),
        "divergence": divergence,
        "trigger_type": "channel_touch",
    })

    if direction == "SELL" and divergence == "bullish":
        info["discarded_reason"] = "divergencia alcista de RCI contradice el toque de resistencia del canal"
        return None, info
    if direction == "BUY" and divergence == "bearish":
        info["discarded_reason"] = "divergencia bajista de RCI contradice el toque de soporte del canal"
        return None, info

    info["divergence_confirms"] = (
        (direction == "BUY" and divergence == "bullish") or
        (direction == "SELL" and divergence == "bearish")
    )

    levels = calculate_trade_levels(candles, i, entry, atr[i], direction)
    info.update({
        "entry": round(entry, 2),
        "stop_loss": round(levels["stop_loss"], 2),
        "take_profit": round(levels["take_profit"], 2),
        "risk_reward": levels["risk_reward"],
    })
    return direction, info


def detect_h4_bias(candles):
    """
    Combina los dos disparadores de sesgo (sirve para H4 o H1, la función no
    depende de la temporalidad, solo de las velas que le pasen): primero el
    cruce EMA9/21 (el original, más probado); si no hay nada, prueba el
    toque de canal con confluencia de swing. Cualquiera de los dos puede
    abrir una idea pendiente de confirmar en M15.
    """
    signal, info = detect_signal(candles)
    if signal:
        info["trigger_type"] = "ema_cross"
        return signal, info
    return detect_channel_touch(candles)


def detect_m15_confirmation(candles, direction):
    """
    Confirmación de entrada en M15: mismo mecanismo de cruce EMA9/EMA21 + filtro
    RSI que usa la temporalidad de sesgo, pero acá solo sirve para afinar el
    momento de entrada de un sesgo ya definido — no genera SL/TP propios.
    """
    closes = [c["close"] for c in candles]
    ema_fast = calculate_ema(closes, EMA_FAST)
    ema_slow = calculate_ema(closes, EMA_SLOW)
    rsi = calculate_rsi(closes, RSI_PERIOD)

    i = len(closes) - 1
    prev = i - 1

    if None in (ema_fast[i], ema_slow[i], ema_fast[prev], ema_slow[prev], rsi[i]):
        return False, {}

    crossed_up = ema_fast[prev] <= ema_slow[prev] and ema_fast[i] > ema_slow[i]
    crossed_down = ema_fast[prev] >= ema_slow[prev] and ema_fast[i] < ema_slow[i]

    info = {
        "close_time": candles[i]["close_time"],
        "price": closes[i],
        "ema_fast": round(ema_fast[i], 2),
        "ema_slow": round(ema_slow[i], 2),
        "rsi": round(rsi[i], 2),
    }

    if direction == "BUY" and crossed_up and rsi[i] < 70:
        return True, info
    if direction == "SELL" and crossed_down and rsi[i] > 30:
        return True, info
    return False, info


def is_bias_invalidated(pending, bias_candles, pending_max_hours):
    """
    Invalida una idea de trade pendiente de confirmación por indicadores, no
    por reloj: si el precio en la temporalidad de sesgo ya tocó el stop loss
    calculado (la idea falló sola), o si aparece un cruce EMA9/EMA21
    contrario (cambió la lectura de fondo). El límite de horas es solo un
    backstop de seguridad aparte, medido contra el reloj real (no contra la
    última vela cerrada — usarla retrasaba el backstop hasta el doble del
    límite si coincidía con el borde de una vela; bug detectado en
    producción: una idea seguía "activa" a las 5.7h con backstop de 4h).
    """
    last = bias_candles[-1]
    direction = pending["direction"]
    sl = pending["stop_loss"]

    if direction == "BUY" and last["low"] <= sl:
        return True, "el precio ya tocó el stop loss calculado"
    if direction == "SELL" and last["high"] >= sl:
        return True, "el precio ya tocó el stop loss calculado"

    closes = [c["close"] for c in bias_candles]
    ema_fast = calculate_ema(closes, EMA_FAST)
    ema_slow = calculate_ema(closes, EMA_SLOW)
    i = len(closes) - 1
    prev = i - 1
    if None not in (ema_fast[i], ema_slow[i], ema_fast[prev], ema_slow[prev]):
        crossed_up = ema_fast[prev] <= ema_slow[prev] and ema_fast[i] > ema_slow[i]
        crossed_down = ema_fast[prev] >= ema_slow[prev] and ema_fast[i] < ema_slow[i]
        if direction == "BUY" and crossed_down:
            return True, "apareció un cruce EMA bajista (la lectura de fondo cambió)"
        if direction == "SELL" and crossed_up:
            return True, "apareció un cruce EMA alcista (la lectura de fondo cambió)"

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    age_hours = (now_ms - pending["bias_close_time"]) / (3600 * 1000)
    if age_hours > pending_max_hours:
        return True, f"pasaron más de {pending_max_hours}h sin confirmación (backstop de seguridad)"

    return False, None


def suggest_leverage(risk_pct, max_leverage=MAX_LEVERAGE, safety_fraction=LEVERAGE_SAFETY_FRACTION):
    """
    Apalancamiento sugerido, solo informativo. Se limita a max_leverage y además
    se recorta para que, si se llega a tocar el stop loss, la pérdida no
    represente más de `safety_fraction` de la distancia estimada a la
    liquidación (a más apalancamiento, más cerca queda la liquidación del
    precio de entrada, así que un SL amplio en % debe ir con menos leverage).
    """
    if risk_pct <= 0:
        return 1
    safe_leverage = safety_fraction / risk_pct
    return max(1, min(max_leverage, int(safe_leverage)))


def suggest_risk_rule_pct(trades, now_ms, layer="trading", base_pct=RISK_RULE_PCT,
                           min_pct=RISK_RULE_MIN_PCT):
    """
    Sugiere qué % del capital arriesgar en la próxima señal de esta capa. Parte
    de la regla del 2% (Elder, Muñoz) y aplica ajuste anti-martingala: por cada
    operación ya cerrada como LOSS hoy (día calendario UTC) en la misma capa,
    reduce la base a la mitad — nunca la sube por ganancias del día — hasta un
    piso mínimo. Devuelve (pct_sugerido, pérdidas_hoy, resultado_acumulado_hoy_pct).
    """
    day_start_ms = int(
        datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    )

    losses_today = 0
    cum_result = 0.0
    for t in trades:
        if t.get("layer") != layer:
            continue
        if t.get("status") in ("WIN", "LOSS") and t.get("closed_at") and t["closed_at"] >= day_start_ms:
            cum_result += t.get("result_pct") or 0.0
            if t["status"] == "LOSS":
                losses_today += 1

    pct = base_pct
    for _ in range(losses_today):
        pct /= 2
    pct = max(pct, min_pct)
    return round(pct, 2), losses_today, round(cum_result, 2)


def compute_context_ema(candles, fast_period=EMA_CONTEXT_FAST, slow_period=EMA_CONTEXT_SLOW):
    """
    EMA50/100 de contexto: puramente informativo. Devuelve None si todavía no
    hay suficientes velas (no debería pasar con BIAS_KLINES_LIMIT), en cuyo
    caso el mensaje simplemente no muestra esta línea.
    """
    closes = [c["close"] for c in candles]
    ema_fast = calculate_ema(closes, fast_period)
    ema_slow = calculate_ema(closes, slow_period)
    i = len(closes) - 1
    if ema_fast[i] is None or ema_slow[i] is None:
        return None
    return {"ema_context_fast": round(ema_fast[i], 2), "ema_context_slow": round(ema_slow[i], 2)}


def format_bias_entry_message(direction, info, bias_label, horizon_desc):
    emoji = "🟢" if direction == "BUY" else "🔴"
    accion = "COMPRA" if direction == "BUY" else "VENTA"

    entry = info["entry"]
    sl = info["stop_loss"]
    tp = info["take_profit"]
    riesgo_pct = abs(entry - sl) / entry * 100
    beneficio_pct = abs(tp - entry) / entry * 100

    confluencia = ""
    if info.get("divergence_confirms"):
        tipo = "alcista" if direction == "BUY" else "bajista"
        confluencia = f"✅ Confirmado por divergencia {tipo} de RCI en {bias_label}\n"

    trigger_label = {
        "ema_cross": "cruce EMA9/EMA21",
        "channel_touch": "toque de límite de canal (regresión + confluencia de swing)",
    }.get(info.get("trigger_type"), "cruce EMA9/EMA21")

    canal_linea = ""
    if info.get("trigger_type") == "channel_touch":
        canal_linea = (f"Canal {bias_label}: soporte ${info['channel_lower']:,.2f} / "
                        f"resistencia ${info['channel_upper']:,.2f}\n")

    ts_bias = datetime.fromtimestamp(info["bias_close_time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ts_m15 = datetime.fromtimestamp(info["close_time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    contexto_linea = ""
    cf, cs = info.get("ema_context_fast"), info.get("ema_context_slow")
    if cf is not None and cs is not None:
        alineado = (cf > cs) == (direction == "BUY")
        estado = "a favor ✅" if alineado else "en contra ⚠️"
        contexto_linea = (
            f"Contexto EMA{EMA_CONTEXT_FAST}/{EMA_CONTEXT_SLOW} en {bias_label}: "
            f"${cf:,.2f} / ${cs:,.2f} ({estado} de la señal, solo informativo)\n"
        )

    gestion_linea = _format_risk_rule_section(info, riesgo_pct)

    return (
        f"{emoji} Señal de {accion} - BTC/USD ({bias_label}, confirmada en M15)\n"
        f"Disparador: {trigger_label}\n"
        f"Sesgo detectado en {bias_label}: {ts_bias}\n"
        f"Confirmado en M15: {ts_m15}\n"
        f"RSI {bias_label}: {info['rsi']} | RCI {bias_label}: {info['rci']} | ATR {bias_label}: {info['atr']:,.2f}\n"
        f"{contexto_linea}"
        f"{canal_linea}"
        f"{confluencia}\n"
        f"📍 Entrada (afinada en M15): ${entry:,.2f}\n"
        f"🛑 Stop loss (estructura M15, ajustado): ${sl:,.2f} (-{riesgo_pct:.2f}%)\n"
        f"🎯 Take profit (objetivo {bias_label}): ${tp:,.2f} (+{beneficio_pct:.2f}%)\n"
        f"⚖️ Ratio riesgo:beneficio real: 1:{info.get('actual_risk_reward', '?')}\n"
        f"📊 Apalancamiento sugerido: hasta {info['leverage']}x\n"
        f"{gestion_linea}\n"
        f"Trade de plazo {horizon_desc}: la dirección y los niveles vienen de la estructura en "
        f"{bias_label}, M15 solo afina el momento de entrada. El apalancamiento amplifica tanto "
        f"ganancias como pérdidas y acerca el precio de liquidación — el valor sugerido "
        f"deja margen respecto de la distancia al stop loss, pero no elimina el riesgo de "
        f"liquidación (funding, mecha de precio, o slippage pueden variarlo). Usalo solo si "
        f"entendés cómo funciona el margen en tu exchange. No es consejo financiero."
    )


def _format_risk_rule_section(info, riesgo_pct):
    """
    Reseña de gestión de riesgo (regla del 2%, Elder/Muñoz): como no conocemos
    tu capital real, expresa el tamaño de posición sugerido como un múltiplo de
    tu capital total, no como un monto. Si el día ya viene en pérdida (según la
    bitácora), la base del 2% se reduce a la mitad por cada pérdida cerrada hoy
    (anti-martingala) y se avisa en el mensaje.
    """
    risk_rule_pct = info.get("risk_rule_pct", RISK_RULE_PCT)
    losses_today = info.get("losses_today", 0)
    cum_result_today = info.get("cum_result_today", 0.0)

    multiplier = risk_rule_pct / riesgo_pct if riesgo_pct > 0 else None
    linea = (
        f"🧮 Gestión de riesgo (regla del {risk_rule_pct:g}%): con este stop, tamaño de "
        f"posición sugerido ≈ {multiplier:.2f}× tu capital (sin apalancamiento), para no "
        f"arriesgar más del {risk_rule_pct:g}% de tu cuenta si toca el stop."
        if multiplier else ""
    )

    if losses_today > 0:
        linea += (
            f"\n📉 Ya venís con {losses_today} pérdida(s) cerrada(s) hoy (resultado del día: "
            f"{cum_result_today:+.2f}%) — por eso se bajó del {RISK_RULE_PCT:g}% base al "
            f"{risk_rule_pct:g}% (anti-martingala: nunca se sube el tamaño después de perder)."
        )

    return linea


def detect_regime_change(candles, ema_fast_period, ema_slow_period, rci_period):
    """
    Cambio de régimen de tendencia de fondo: cruce de EMAs (golden/death cross),
    confirmado con RCI de período largo. A diferencia de detect_signal, esto no es
    una entrada de trading puntual, sino un aviso de que la tendencia mayor cambió.
    Genérica: se usa tanto para la capa Diaria (Capa B) como Semanal (Capa C), con
    distintos períodos según el horizonte.
    """
    closes = [c["close"] for c in candles]
    ema_fast = calculate_ema(closes, ema_fast_period)
    ema_slow = calculate_ema(closes, ema_slow_period)
    rci = calculate_rci(candles, rci_period)
    atr = calculate_atr(candles, ATR_PERIOD)

    i = len(closes) - 1
    prev = i - 1

    if None in (ema_fast[i], ema_slow[i], ema_fast[prev], ema_slow[prev], rci[i], atr[i]):
        return None, {}

    crossed_up = ema_fast[prev] <= ema_slow[prev] and ema_fast[i] > ema_slow[i]
    crossed_down = ema_fast[prev] >= ema_slow[prev] and ema_fast[i] < ema_slow[i]

    if not (crossed_up or crossed_down):
        return None, {
            "rci": round(rci[i], 2),
            "ema_fast": round(ema_fast[i], 2),
            "ema_slow": round(ema_slow[i], 2),
        }

    direction = "BULLISH" if crossed_up else "BEARISH"
    entry = closes[i]
    info = {
        "close_time": candles[i]["close_time"],
        "price": entry,
        "ema_fast": round(ema_fast[i], 2),
        "ema_slow": round(ema_slow[i], 2),
        "rci": round(rci[i], 2),
        "atr": round(atr[i], 2),
    }

    levels = calculate_trade_levels(candles, i, entry, atr[i], "BUY" if direction == "BULLISH" else "SELL")
    info.update({
        "entry": round(entry, 2),
        "stop_loss": round(levels["stop_loss"], 2),
        "take_profit": round(levels["take_profit"], 2),
        "risk_reward": levels["risk_reward"],
    })
    return direction, info


def format_regime_message(direction, info, horizon_label, ema_fast_period, ema_slow_period,
                           rci_period, note):
    bullish = direction == "BULLISH"
    emoji = "🚀" if bullish else "⚠️"
    titulo = "posible cambio a tendencia ALCISTA" if bullish else "posible cambio a tendencia BAJISTA"
    ts = datetime.fromtimestamp(info["close_time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    riesgo_pct = abs(info["entry"] - info["stop_loss"]) / info["entry"] * 100
    gestion_linea = _format_risk_rule_section(info, riesgo_pct)

    return (
        f"{emoji} CAMBIO DE TENDENCIA — BTC/USD ({horizon_label})\n"
        f"{titulo}\n\n"
        f"Precio: ${info['price']:,.2f}\n"
        f"EMA{ema_fast_period}: ${info['ema_fast']:,.2f} | EMA{ema_slow_period}: ${info['ema_slow']:,.2f}\n"
        f"RCI({rci_period}): {info['rci']}\n\n"
        f"📍 Referencia: ${info['entry']:,.2f}\n"
        f"🛑 Stop estructural: ${info['stop_loss']:,.2f}\n"
        f"🎯 Objetivo (1:{info['risk_reward']:.0f}): ${info['take_profit']:,.2f}\n"
        f"{gestion_linea}\n"
        f"Vela cerrada: {ts}\n\n"
        f"{note} No es consejo financiero."
    )


def load_state():
    """
    "H4_pending"/"H1_pending" guardan la idea de trade pendiente de
    confirmación en M15 para cada temporalidad de sesgo (o {"active": False}
    si no hay ninguna). Las claves *_trend guardan la última vela ya alertada
    por cada capa de tendencia, para no repetir. "checks_status" trackea
    fallos consecutivos por check, para avisar por Telegram si algo queda
    roto. "telegram_update_offset" es el cursor de getUpdates (botones y
    comandos como /estado). "last_run_at" queda para poder responder /estado.
    """
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    else:
        state = {}

    state.setdefault("H4_pending", {"active": False})
    state.setdefault("H4_last_bias_close_time", None)
    state.setdefault("H1_pending", {"active": False})
    state.setdefault("H1_last_bias_close_time", None)
    state.setdefault(TREND_TIMEFRAME["label"] + "_trend", {"last_alerted_close_time": None})
    state.setdefault(LONGTERM_TIMEFRAME["label"] + "_trend", {"last_alerted_close_time": None})
    state.setdefault("checks_status", {})
    state.setdefault("last_run_at", None)
    state.setdefault("telegram_update_offset", None)
    # Cuando se toca un botón "Ya la cargué/sumé" (MS o DS), acá se guarda qué
    # se está esperando confirmar mientras se le pide el precio a Marcos.
    state.setdefault("pending_confirmation", None)

    return state


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def send_telegram_message(text, reply_markup=None):
    """Devuelve el message_id de Telegram si se pudo mandar (o None), para poder
    referenciarlo después (ej. guardarlo en la bitácora)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[AVISO] Faltan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. No se envía Telegram.")
        print(text)
        return None
    url = TELEGRAM_API_URL.format(token=token)
    # Sin parse_mode (texto plano): con Markdown, Telegram trata "_" y "*"
    # como marcadores de cursiva/negrita y los "come" del texto cuando hay
    # varios en el mismo mensaje (ej. nombres de comando como
    # "/saylor_promedio") — pasó de verdad y rompió el menú. Más vale texto
    # plano sin sorpresas que negrita con riesgo de comerse guiones bajos.
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    resp = requests.post(url, json=payload, timeout=15)
    if resp.status_code != 200:
        print(f"[ERROR] Telegram respondió {resp.status_code}: {resp.text}")
        return None
    print("[OK] Alerta enviada por Telegram.")
    return resp.json().get("result", {}).get("message_id")


def build_help_message():
    # Nota: la capa de trading H4/H1/M15 está apagada (ALERTAS_TRADING_ACTIVAS
    # = False) — Marcos pidió (12/09) recibir solo MS y DS, así que el menú no
    # la menciona. /estado sigue andando por si hace falta diagnosticar el bot.
    return (
        "👋 ¿Qué puedo hacer?\n\n"
        "₿ Estrategia MS\n"
        "/saylor_iniciar <capital> → arranca de cero, calcula bala y carga inicial.\n"
        "/saylor → tu estado: BTC acumulado, promedio, margen, liquidación.\n"
        "/chequeo → precio y ROI ahora, y si conviene recargar.\n"
        "/saylor_cerrar <precio> → cierra la posición y calcula el resultado.\n"
        "/saylor_promedio <precio> → ajusta el promedio real (por fees/slippage), sin tocar las balas usadas.\n"
        "/deshacer → revierte la última carga.\n\n"
        "📊 Estrategia DS\n"
        "/ds → tu estado: balas, entrada, margen real, rendimiento.\n"
        "/ds_chequeo → rendimiento ahora (con precio a mano o automático).\n"
        "/ds_deshacer → revierte la última bala.\n"
        "/ds_bala2 <precio> [margen_usdt] [tamaño_mstr] → carga a mano.\n\n"
        "Cada carga sugerida te llega con un botón para confirmarla — lo "
        "tocás, te pido el precio, lo mandás y queda registrada sola.\n\n"
        "\"hola\" reabre este menú, \"cancelar\" descarta un precio pedido."
    )


def build_help_keyboard():
    """Botones inline para no tener que tipear los comandos a mano. El
    callback_data lleva el comando real (con prefijo cmd:) y se rutea con la
    misma lógica que si lo hubieras escrito."""
    return {
        "inline_keyboard": [
            [{"text": "📈 /estado", "callback_data": "cmd:/estado"}],
            [
                {"text": "₿ /saylor", "callback_data": "cmd:/saylor"},
                {"text": "₿ /chequeo", "callback_data": "cmd:/chequeo"},
            ],
            [
                {"text": "📊 /ds", "callback_data": "cmd:/ds"},
                {"text": "📊 /ds_chequeo", "callback_data": "cmd:/ds_chequeo"},
            ],
        ]
    }


def build_confirm_ms_keyboard(balas_posicion, balas_margen=0):
    """Botón "Ya la cargué" para la recarga sugerida de MS. El callback_data
    lleva los balas a confirmar (posición y, si aplica, margen extra de
    situación crítica) para no tener que recalcularlos después."""
    return {
        "inline_keyboard": [[
            {"text": "✅ Ya la cargué", "callback_data": f"confirm:MS:{balas_posicion}:{balas_margen}"}
        ]]
    }


def build_confirm_ds_keyboard(bala_numero):
    """Botón "Ya la sumé" para la próxima bala de DS."""
    return {
        "inline_keyboard": [[
            {"text": "✅ Ya la sumé", "callback_data": f"confirm:DS:{bala_numero}"}
        ]]
    }


def resolve_pending_confirmation(pending, precio):
    """
    Aplica la confirmación pendiente (venía de tocar un botón "Ya la
    cargué/sumé") ahora que Marcos mandó el precio. Devuelve el texto de
    respuesta para Telegram.
    """
    estrategia = pending.get("strategy")

    if estrategia == "MS" and saylor_bot is not None:
        state_ms = saylor_bot.load_saylor_state()
        balas_posicion = pending.get("balas_posicion", 0)
        balas_margen = pending.get("balas_margen", 0)
        mensajes = []
        posicion_ok = True
        if balas_posicion:
            result = saylor_bot.confirmar_recarga(state_ms, balas_posicion, precio, es_margen_extra=False)
            posicion_ok = result.get("ok", False)
            mensajes.append(saylor_bot.format_confirmacion_message(result))
        if balas_margen and posicion_ok:
            result_margen = saylor_bot.confirmar_recarga(state_ms, balas_margen, precio, es_margen_extra=True)
            mensajes.append(saylor_bot.format_confirmacion_message(result_margen))
        saylor_bot.save_saylor_state(state_ms)
        return "\n\n".join(mensajes) if mensajes else "No había nada pendiente para confirmar."

    if estrategia == "DS" and ds_bot is not None:
        state_ds = ds_bot.load_ds_state()
        result = ds_bot.registrar_bala(state_ds, precio)
        if result.get("ok"):
            ds_bot.save_ds_state(state_ds)
        return ds_bot.format_registro_message(result)

    return "No pude procesar la confirmación (módulo de la estrategia no disponible)."


def build_trade_response_keyboard(trade_id):
    """Botones inline "Tomé / No tomé el trade" — el callback_data lleva el id
    del trade para poder correlacionarlo directo con la bitácora."""
    return {
        "inline_keyboard": [[
            {"text": "✅ Tomé el trade", "callback_data": f"take:{trade_id}"},
            {"text": "❌ No lo tomé", "callback_data": f"skip:{trade_id}"},
        ]]
    }


def get_telegram_updates(offset=None):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return []
    url = TELEGRAM_GET_UPDATES_URL.format(token=token)
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(url, params=params, timeout=15)
    if resp.status_code != 200:
        print(f"[ERROR] getUpdates respondió {resp.status_code}: {resp.text}")
        return []
    data = resp.json()
    return data.get("result", []) if data.get("ok") else []


def answer_telegram_callback(callback_query_id, text=None):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    url = TELEGRAM_ANSWER_CALLBACK_URL.format(token=token)
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    requests.post(url, json=payload, timeout=15)


def build_health_status_message(state, trades):
    """Arma la respuesta a "/estado": si el bot sigue corriendo, cuándo corrió
    la última vez, si hay ideas pendientes, y si algún check viene fallando."""
    last_run = state.get("last_run_at")
    last_run_txt = (
        datetime.fromtimestamp(last_run / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if last_run else "todavía no corrió ninguna vez"
    )

    problems = [
        f"⚠️ {label}: {status['consecutive_failures']} fallos seguidos — último error: {status.get('last_error')}"
        for label, status in state.get("checks_status", {}).items()
        if status.get("consecutive_failures", 0) > 0
    ]

    open_trades = [t for t in trades if t.get("status") == "OPEN"]

    pending_lines = []
    for pending_key, bias_label in (("H4_pending", "H4"), ("H1_pending", "H1")):
        p = state.get(pending_key) or {}
        if p.get("active"):
            pending_lines.append(f"{bias_label}: {p['direction']} esperando confirmación en M15")

    lines = [
        "⚠️ El bot está en línea, pero con problemas:" if problems else "✅ El bot está en línea y corriendo normal.",
        f"Última corrida: {last_run_txt}",
        f"Trades abiertos en la bitácora: {len(open_trades)}",
        "Ideas pendientes: " + (" | ".join(pending_lines) if pending_lines else "ninguna ahora mismo"),
    ]
    if problems:
        lines.append("\n".join(problems))

    return "\n".join(lines)


def route_command_text(state, trades, text):
    """
    Procesa un texto de comando (venga de un mensaje tipeado o de apretar un
    botón del menú) y manda la respuesta correspondiente por Telegram. Único
    punto de ruteo — así el menú con botones y el texto libre usan exactamente
    la misma lógica y nunca se desincronizan.
    """
    lower = text.lower()
    if lower in ("hola", "/hola", "/help", "/ayuda", "/start"):
        send_telegram_message(build_help_message(), reply_markup=build_help_keyboard())
    elif lower in ("/estado", "/status"):
        send_telegram_message(build_health_status_message(state, trades))
    elif ds_bot is not None and lower.startswith("/ds"):
        # Comandos de DS son siempre explícitos (empiezan con "/ds"), se
        # chequean ANTES que saylor_bot para que nunca lleguen a intentar
        # parsearse como una confirmación de recarga de MS.
        try:
            reply = ds_bot.handle_command(text)
            if reply:
                send_telegram_message(reply)
        except Exception as e:
            print(f"[ERROR] [ds_bot] {e}")
    elif saylor_bot is not None:
        # Todo lo que no es un comando de la capa de trading se prueba
        # contra la estrategia Saylor (comandos propios o confirmación de
        # recarga por texto libre). handle_message devuelve None si el
        # texto no le corresponde.
        try:
            reply = saylor_bot.handle_message(text)
        except Exception as e:
            reply = None
            print(f"[ERROR] [saylor_bot] {e}")
        if reply:
            send_telegram_message(reply)
        elif text.startswith("/"):
            # Comando con "/" que nadie reconoció (típico: falta un guion
            # bajo, ej. "/saylorpromedio" en vez de "/saylor_promedio") —
            # antes esto se quedaba mudo, sin avisar que el comando no existe.
            send_telegram_message(
                "🤔 No reconozco ese comando. Escribí \"hola\" para ver el menú completo."
            )


def process_telegram_updates(state, trades):
    """
    Revisa mensajes/botones nuevos de Telegram desde la última corrida. No es
    instantáneo: como el bot no queda escuchando todo el tiempo, la respuesta
    puede demorar hasta el intervalo del cron (~15 min). Maneja:
    - "hola" / "/help": manda el menú con los comandos disponibles (con botones).
    - "/estado" (o "/status"): responde con un resumen de si el bot está bien.
    - Botones del menú ("cmd:..."): ejecutan el mismo comando que si se
      hubiera tipeado.
    - Botones "Ya la cargué/sumé" de MS/DS ("confirm:..."): piden el precio y
      lo dejan pendiente en state["pending_confirmation"] hasta que Marcos
      responda con el número (o "cancelar").
    - Botones "Tomé/No tomé el trade": marca la respuesta en la bitácora.
    """
    offset = state.get("telegram_update_offset")
    updates = get_telegram_updates(offset)

    for update in updates:
        state["telegram_update_offset"] = update["update_id"] + 1

        callback = update.get("callback_query")
        if callback:
            data = callback.get("data", "")
            action, _, payload = data.partition(":")

            if action == "cmd":
                answer_telegram_callback(callback["id"])
                try:
                    route_command_text(state, trades, payload)
                except Exception as e:
                    print(f"[ERROR] [menu_button:{payload}] {e}")
                continue

            if action == "confirm":
                answer_telegram_callback(callback["id"])
                partes = payload.split(":")
                estrategia = partes[0]
                if estrategia == "MS":
                    balas_posicion = int(partes[1]) if len(partes) > 1 else 0
                    balas_margen = int(partes[2]) if len(partes) > 2 else 0
                    state["pending_confirmation"] = {
                        "strategy": "MS",
                        "balas_posicion": balas_posicion,
                        "balas_margen": balas_margen,
                    }
                    send_telegram_message(
                        "💬 Decime el precio de BTC al que la cargaste (solo el número, "
                        "ej. \"77450\"), o \"cancelar\" si no querés registrar nada."
                    )
                elif estrategia == "DS":
                    bala_numero = int(partes[1]) if len(partes) > 1 else None
                    state["pending_confirmation"] = {"strategy": "DS", "bala_numero": bala_numero}
                    send_telegram_message(
                        "💬 Decime el precio de MSTR al que la cargaste (solo el número, "
                        "ej. \"128.50\"), o \"cancelar\" si no querés registrar nada."
                    )
                continue

            trade = next((t for t in trades if t.get("id") == payload), None)
            if trade and action in ("take", "skip"):
                trade["user_response"] = "tomado" if action == "take" else "no_tomado"
                ack = "👍 Anotado: tomaste el trade." if action == "take" else "👌 Anotado: no lo tomaste."
                answer_telegram_callback(callback["id"], text=ack)
                print(f"[TELEGRAM] Respuesta registrada en trade {payload}: {trade['user_response']}")
            else:
                answer_telegram_callback(callback["id"])
            continue

        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        if not text:
            continue

        pending = state.get("pending_confirmation")
        if pending:
            lower_text = text.lower()
            if lower_text in ("cancelar", "/cancelar"):
                state["pending_confirmation"] = None
                send_telegram_message("Cancelado — no se registró nada.")
                continue
            if text.startswith("/"):
                # Se fue a otro comando sin responder el precio — se cancela
                # solo, no lo dejamos bloqueado esperando un número.
                state["pending_confirmation"] = None
                send_telegram_message("↩️ Cancelé el pedido de precio anterior (mandaste otro comando).")
                route_command_text(state, trades, text)
                continue
            precio = extract_price_from_text(text)
            if precio is None:
                send_telegram_message(
                    "No pude leer un precio ahí — mandame solo el número (ej. \"77450\" o "
                    "\"128.50\"), o \"cancelar\" para no registrar nada."
                )
                continue
            try:
                reply = resolve_pending_confirmation(pending, precio)
            except Exception as e:
                reply = f"⚠️ Error al registrar la confirmación: {e}"
                print(f"[ERROR] [resolve_pending_confirmation] {e}")
            state["pending_confirmation"] = None
            if reply:
                send_telegram_message(reply)
            continue

        route_command_text(state, trades, text)


def record_check_result(state, label, error=None):
    """
    Trackea fallos consecutivos por check. Si un check falla ERROR_ALERT_THRESHOLD
    veces seguidas, manda un aviso por Telegram (una sola vez, no en cada corrida
    mientras siga fallando) — así te enterás de un problema real sin depender del
    mail de GitHub Actions. Si se recupera después de haber alertado, también avisa.
    """
    status = state["checks_status"].setdefault(
        label, {"consecutive_failures": 0, "last_error": None, "alerted": False})

    if error is None:
        if status["alerted"]:
            send_telegram_message(f"✅ {label} se recuperó, ya está funcionando de nuevo.")
        status["consecutive_failures"] = 0
        status["last_error"] = None
        status["alerted"] = False
    else:
        status["consecutive_failures"] += 1
        status["last_error"] = str(error)
        if status["consecutive_failures"] >= ERROR_ALERT_THRESHOLD and not status["alerted"]:
            send_telegram_message(
                f"⚠️ {label} falló {status['consecutive_failures']} veces seguidas.\n"
                f"Último error: {error}\n\nRevisá el log en GitHub Actions."
            )
            status["alerted"] = True


def load_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "r") as f:
            return json.load(f)
    return []


def save_trades(trades):
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=None)


def log_trade(trades, layer, timeframe_label, direction, info):
    """Registra una señal (de cualquier capa) en la bitácora, sin importar si el
    usuario la toma o no — así se puede medir la efectividad real del bot."""
    trade = {
        "id": f"{timeframe_label}-{direction}-{info['close_time']}",
        "layer": layer,  # "trading" (H4c/H1c) | "trend" (D1/W1)
        "timeframe": timeframe_label,
        "direction": direction,
        "entry": info.get("entry"),
        "stop_loss": info.get("stop_loss"),
        "take_profit": info.get("take_profit"),
        "opened_at": info["close_time"],
        "status": "OPEN",
        "closed_at": None,
        "closed_price": None,
        "result_pct": None,
        "indicators": {
            "rsi": info.get("rsi"),
            "rci": info.get("rci"),
            "atr": info.get("atr"),
            "divergence": info.get("divergence"),
        },
        "user_response": None,          # "tomado" / "no_tomado", vía botones de Telegram
        "telegram_message_id": None,    # se completa después de mandar el mensaje
    }
    trades.append(trade)
    return trade


def update_open_trades(trades, timeframe_label, candles):
    """
    Revisa los trades abiertos de esa temporalidad contra las velas ya traídas
    (sin pedir datos extra a la API) y los marca WIN/LOSS apenas el precio toca
    el take profit o el stop loss. Si un trade quedó abierto más tiempo del que
    cubre el historial que trajimos, se marca aparte en vez de arriesgar un
    resultado inventado.
    """
    if not candles:
        return

    oldest_open_time = candles[0]["open_time"]

    for t in trades:
        if t["status"] != "OPEN" or t["timeframe"] != timeframe_label:
            continue

        if t["opened_at"] < oldest_open_time:
            t["status"] = "SIN_DATOS"
            continue

        is_buy = t["direction"] in ("BUY", "BULLISH")

        for c in candles:
            if c["close_time"] <= t["opened_at"]:
                continue

            hit_tp = c["high"] >= t["take_profit"] if is_buy else c["low"] <= t["take_profit"]
            hit_sl = c["low"] <= t["stop_loss"] if is_buy else c["high"] >= t["stop_loss"]

            if hit_tp and hit_sl:
                # Tocó los dos niveles en la misma vela: con datos OHLC no se puede saber
                # el orden exacto intra-vela. Conservador: se cuenta como pérdida.
                t["status"], t["closed_at"], t["closed_price"] = "LOSS", c["close_time"], t["stop_loss"]
                break
            if hit_tp:
                t["status"], t["closed_at"], t["closed_price"] = "WIN", c["close_time"], t["take_profit"]
                break
            if hit_sl:
                t["status"], t["closed_at"], t["closed_price"] = "LOSS", c["close_time"], t["stop_loss"]
                break

        if t["status"] != "OPEN":
            entry, closed = t["entry"], t["closed_price"]
            pct = (closed - entry) / entry * 100 if is_buy else (entry - closed) / entry * 100
            t["result_pct"] = round(pct, 2)
            print(f"[BITÁCORA] Trade {t['id']} cerrado: {t['status']} ({t['result_pct']}%)")


def check_bias_entry(state, trades, bias_cfg, horizon_desc, pending_key, last_bias_key, pending_max_hours):
    """
    Capa de trading genérica: sesgo direccional desde `bias_cfg` (H4 o H1),
    entrada afinada con confirmación en M15. Ver docstring del módulo.
    """
    bias_label = bias_cfg["label"]
    label = bias_label + "c"  # ej. "H4c", "H1c" -> temporalidad de sesgo confirmada en M15
    bias_candles = only_closed_candles(
        get_klines(interval_minutes=bias_cfg["kraken_interval"], limit=BIAS_KLINES_LIMIT))

    update_open_trades(trades, label, bias_candles)

    pending = state.get(pending_key) or {"active": False}

    if pending.get("active"):
        invalidated, reason = is_bias_invalidated(pending, bias_candles, pending_max_hours)

        if invalidated:
            print(f"[{label}] Idea de {pending['direction']} descartada: {reason}")
            pending = {"active": False}
            state[pending_key] = pending
        else:
            m15_candles = only_closed_candles(get_klines(interval_minutes=M15_CONFIG["kraken_interval"], limit=100))
            confirmed, m15_info = detect_m15_confirmation(m15_candles, pending["direction"])

            if confirmed:
                direction = pending["direction"]
                entry_final = m15_info["price"]

                # Stop ajustado en M15 (más cerca de la entrada), take profit proyectado
                # desde la temporalidad de sesgo (el objetivo grande, sin recalcular).
                i_m15 = len(m15_candles) - 1
                atr_m15 = calculate_atr(m15_candles, ATR_PERIOD)[i_m15]
                m15_levels = calculate_trade_levels(m15_candles, i_m15, entry_final, atr_m15, direction)
                stop_loss = m15_levels["stop_loss"]
                take_profit = pending["take_profit"]

                valid = (stop_loss < entry_final < take_profit if direction == "BUY"
                          else take_profit < entry_final < stop_loss)

                if not valid:
                    print(f"[{label}] Confirmación en M15 descartada: el precio ya se movió "
                          f"demasiado y el stop/take profit quedarían del lado equivocado. "
                          f"Sigue esperando la próxima vela M15.")
                else:
                    risk_abs = abs(entry_final - stop_loss)
                    reward_abs = abs(take_profit - entry_final)
                    risk_pct = risk_abs / entry_final

                    full_info = dict(pending)
                    full_info.update({
                        "entry": round(entry_final, 2),
                        "stop_loss": round(stop_loss, 2),
                        "take_profit": round(take_profit, 2),
                        "close_time": m15_info["close_time"],
                        "leverage": suggest_leverage(risk_pct),
                        "risk_pct": round(risk_pct * 100, 2),
                        "actual_risk_reward": round(reward_abs / risk_abs, 2) if risk_abs > 0 else None,
                    })

                    risk_rule_pct, losses_today, cum_result_today = suggest_risk_rule_pct(
                        trades, full_info["close_time"], layer="trading")
                    full_info.update({
                        "risk_rule_pct": risk_rule_pct,
                        "losses_today": losses_today,
                        "cum_result_today": cum_result_today,
                    })

                    context = compute_context_ema(bias_candles)
                    if context:
                        full_info.update(context)

                    # Se registra en la bitácora ANTES de mandar el mensaje, para poder
                    # incluir el id del trade en los botones "Tomé/No tomé el trade".
                    trade = log_trade(trades, "trading", label, direction, full_info)
                    message = format_bias_entry_message(direction, full_info, bias_label, horizon_desc)
                    message_id = send_telegram_message(message, reply_markup=build_trade_response_keyboard(trade["id"]))
                    trade["telegram_message_id"] = message_id

                    pending = {"active": False}
                    state[pending_key] = pending

            if pending.get("active"):
                print(f"[{label}] Sesgo {pending['direction']} en {bias_label} activo, esperando "
                      f"confirmación en M15 (RSI M15={m15_info.get('rsi')}).")

    if not pending.get("active"):
        signal, info = detect_h4_bias(bias_candles)
        already_used = info.get("close_time") == state.get(last_bias_key)
        if signal and not already_used:
            state[pending_key] = {
                "active": True,
                "direction": signal,
                "trigger_type": info.get("trigger_type", "ema_cross"),
                "bias_close_time": info["close_time"],
                "stop_loss": info["stop_loss"],       # referencia de sesgo, usada para invalidar la idea
                "take_profit": info["take_profit"],   # objetivo de sesgo, se mantiene sin recalcular
                "rsi": info["rsi"],
                "rci": info["rci"],
                "atr": info["atr"],
                "divergence": info["divergence"],
                "divergence_confirms": info.get("divergence_confirms", False),
                "channel_upper": info.get("channel_upper"),
                "channel_lower": info.get("channel_lower"),
            }
            state[last_bias_key] = info["close_time"]
            print(f"[{label}] Nueva idea de {signal} en {bias_label} ({info.get('trigger_type')}), "
                  f"esperando confirmación en M15.")
        elif signal and already_used:
            print(f"[{label}] La vela {bias_label} ya generó una idea antes (confirmada o descartada), "
                  f"no se repite hasta la próxima vela.")
        else:
            print(f"[{datetime.now(timezone.utc).isoformat()}] [{label}] Sin sesgo nuevo en {bias_label}. "
                  f"RSI={info.get('rsi')} EMA9={info.get('ema_fast')} EMA21={info.get('ema_slow')}")


def check_h4_entry(state, trades):
    """Trade de plazo más largo: sesgo en H4, confirmación en M15."""
    check_bias_entry(state, trades, H4_CONFIG, "más largo", "H4_pending",
                      "H4_last_bias_close_time", H4_PENDING_MAX_HOURS)


def check_h1_entry(state, trades):
    """Trade de plazo intermedio: sesgo en H1, confirmación en M15. Agregado
    para que lleguen alertas con más frecuencia que solo con H4."""
    check_bias_entry(state, trades, H1_CONFIG, "intermedio", "H1_pending",
                      "H1_last_bias_close_time", H1_PENDING_MAX_HOURS)


def check_regime_change(state, trades, timeframe_cfg, ema_fast_period, ema_slow_period, rci_period,
                         horizon_label, note, klines_limit=300):
    label = timeframe_cfg["label"] + "_trend"
    candles = only_closed_candles(get_klines(interval_minutes=timeframe_cfg["kraken_interval"], limit=klines_limit))

    update_open_trades(trades, label, candles)

    direction, info = detect_regime_change(candles, ema_fast_period, ema_slow_period, rci_period)

    if not direction:
        print(f"[{datetime.now(timezone.utc).isoformat()}] [{label}] Sin cambio de tendencia. "
              f"RCI({rci_period})={info.get('rci')} EMA{ema_fast_period}={info.get('ema_fast')} "
              f"EMA{ema_slow_period}={info.get('ema_slow')}")
        return

    if state[label].get("last_alerted_close_time") == info["close_time"]:
        print(f"[{label}] Cambio de tendencia ya alertado para esta vela, no se repite.")
        return

    risk_rule_pct, losses_today, cum_result_today = suggest_risk_rule_pct(
        trades, info["close_time"], layer="trend")
    info.update({
        "risk_rule_pct": risk_rule_pct,
        "losses_today": losses_today,
        "cum_result_today": cum_result_today,
    })

    message = format_regime_message(direction, info, horizon_label, ema_fast_period,
                                     ema_slow_period, rci_period, note)
    send_telegram_message(message)
    log_trade(trades, "trend", label, direction, info)

    state[label]["last_alerted_close_time"] = info["close_time"]
    state[label]["last_direction"] = direction


def run_once():
    state = load_state()
    trades = load_trades()

    try:
        process_telegram_updates(state, trades)
    except Exception as e:
        print(f"[ERROR] [telegram_updates] {e}")

    checks = []
    if ALERTAS_TRADING_ACTIVAS:
        checks = [
            ("H4c", lambda: check_h4_entry(state, trades)),
            ("H1c", lambda: check_h1_entry(state, trades)),
            ("D1_trend", lambda: check_regime_change(
                state, trades, TREND_TIMEFRAME, EMA_TREND_FAST, EMA_TREND_SLOW, RCI_TREND_PERIOD,
                horizon_label="Diario",
                note=("Aviso de cambio de régimen de mercado de fondo (poco frecuente, no es una "
                      "entrada inmediata como M15/H1/H4). Útil para decidir si conviene operar a favor "
                      "o en contra de la tendencia mayor."),
            )),
            ("W1_trend", lambda: check_regime_change(
                state, trades, LONGTERM_TIMEFRAME, EMA_LT_FAST, EMA_LT_SLOW, RCI_LT_PERIOD,
                horizon_label="Semanal — visión de largo plazo",
                note=("Visión de largo plazo (meses/años), pensada para decisiones de inversión, no "
                      "de trading. Kraken no ofrece velas mensuales nativas, así que se usa la "
                      "Semanal como la temporalidad práctica más larga disponible."),
                klines_limit=500,
            )),
        ]

    for label, fn in checks:
        try:
            fn()
            record_check_result(state, label)
        except Exception as e:
            print(f"[ERROR] [{label}] {e}")
            record_check_result(state, label, error=e)

    state["last_run_at"] = int(datetime.now(timezone.utc).timestamp() * 1000)

    save_state(state)
    save_trades(trades)


def compute_bitacora_stats(trades, layer_filter=None, since_ms=None):
    """
    Resumen de la bitácora: cantidad de señales, winrate sobre las cerradas,
    resultado acumulado, y cuántas señales tomaste vos según los botones de
    Telegram. `since_ms` filtra por fecha de apertura (para "última semana"),
    dejarlo en None para el histórico completo.
    """
    filtered = [t for t in trades if layer_filter is None or t.get("layer") == layer_filter]
    if since_ms is not None:
        filtered = [t for t in filtered if (t.get("opened_at") or 0) >= since_ms]

    wins = [t for t in filtered if t.get("status") == "WIN"]
    losses = [t for t in filtered if t.get("status") == "LOSS"]
    open_trades = [t for t in filtered if t.get("status") == "OPEN"]
    closed = wins + losses
    winrate = (len(wins) / len(closed) * 100) if closed else None
    cum_result = sum(t.get("result_pct") or 0 for t in closed)
    taken = [t for t in filtered if t.get("user_response") == "tomado"]

    return {
        "total": len(filtered),
        "wins": len(wins),
        "losses": len(losses),
        "open": len(open_trades),
        "winrate": winrate,
        "cum_result": round(cum_result, 2),
        "taken": len(taken),
    }


def _format_bitacora_stats_line(stats):
    wr = f"{stats['winrate']:.0f}%" if stats["winrate"] is not None else "s/d"
    return (f"{stats['total']} señales | {stats['wins']}W-{stats['losses']}L "
            f"({wr} winrate) | {stats['open']} abiertas | "
            f"resultado acumulado: {stats['cum_result']:+.2f}% | "
            f"tomadas por vos: {stats['taken']}")


def format_weekly_report(trades, now_ms):
    """
    Reporte semanal de la capa de trading (H4c/H1c): resumen de los últimos 7
    días y el histórico completo, para poder juzgar la efectividad real sin
    depender de revisar trades.json a mano. La capa de tendencia (D1/W1) no
    entra acá — son avisos muy poco frecuentes, no tiene sentido un reporte
    semanal para eso.
    """
    since_week = now_ms - 7 * 24 * 3600 * 1000
    week = compute_bitacora_stats(trades, layer_filter="trading", since_ms=since_week)
    overall = compute_bitacora_stats(trades, layer_filter="trading")

    lines = [
        "📊 Reporte semanal — bot BTC (capa de trading H4/H1)",
        "",
        f"Últimos 7 días: {_format_bitacora_stats_line(week)}",
        f"Histórico completo: {_format_bitacora_stats_line(overall)}",
        "",
    ]

    if overall["total"] < 20:
        lines.append(
            f"Todavía es poca muestra ({overall['total']} señales en total) para sacar "
            f"conclusiones estadísticas confiables — seguimos acumulando datos antes de "
            f"pensar en plata real."
        )

    lines.append("\nReporte automático generado desde la bitácora. No es consejo financiero.")
    return "\n".join(lines)


def send_weekly_report():
    """Manda el reporte semanal por Telegram. Pensado para correr desde un
    workflow de GitHub Actions aparte, con su propio cron semanal."""
    if not ALERTAS_TRADING_ACTIVAS:
        return  # capa de trading apagada — no tiene sentido un reporte de 0 señales
    trades = load_trades()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    send_telegram_message(format_weekly_report(trades, now_ms))


def test_telegram():
    """Manda un mensaje de prueba, sin depender de que haya una señal real. Sirve
    para verificar que el token/chat_id están bien configurados."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    send_telegram_message(
        f"✅ Test de conexión — el bot de alertas BTC (H4/H1/M15) está andando bien.\n"
        f"Hora: {ts}"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Correr en loop continuo (revisa cada 1 min)")
    parser.add_argument("--test-telegram", action="store_true", help="Manda un mensaje de prueba y sale, sin chequear señales")
    parser.add_argument("--weekly-report", action="store_true", help="Manda el reporte semanal de la bitácora y sale")
    args = parser.parse_args()

    if args.test_telegram:
        test_telegram()
    elif args.weekly_report:
        send_weekly_report()
    elif args.loop:
        print("Bot corriendo en loop. Ctrl+C para detener.")
        while True:
            try:
                run_once()
            except Exception as e:
                print(f"[ERROR] {e}")
            time.sleep(60)
    else:
        run_once()


if __name__ == "__main__":
    main()
