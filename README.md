# Bot de alertas BTC (H4 + H1 + M15) por Telegram — gratis

Bot de trading/inversión en BTC/USD que corre gratis y en tiempo real en **GitHub
Actions** (sin servidor propio, sin PC prendida) y avisa por **Telegram**. Combina
varias capas independientes:

- **Trading (H4 y H1, confirmadas en M15)**: ideas de entrada con dirección, precio
  de entrada, stop loss, take profit, apalancamiento sugerido y tamaño de posición
  según la regla del 2%.
- **Tendencia (Diario y Semanal)**: avisos de cambio de tendencia mayor, para
  decisiones de más largo plazo (no son entradas puntuales).
- **Bitácora**: registro automático de cada señal en `trades.json`, con
  seguimiento de resultado (WIN/LOSS) sin importar si la tomaste o no — y
  botones de Telegram para que quede registrado si la tomaste.
- **Reporte semanal**: resumen automático de la bitácora todos los lunes.

## Cómo funciona la capa de trading (H4/H1 + M15)

La idea central: **la temporalidad grande (H4 o H1) define la dirección y los
niveles del trade, M15 solo afina el momento de entrada.**

1. En H4 (trade de plazo más largo) o H1 (trade de plazo intermedio, para que
   lleguen alertas con más frecuencia) se detecta un **sesgo**: cruce de
   EMA9/EMA21, o un toque de los límites de un canal de regresión lineal
   validado contra un swing real (soporte/resistencia). Ambos disparadores se
   filtran con RSI y con divergencia RCI/precio.
2. Ese sesgo queda **pendiente**, esperando que M15 confirme con su propio
   cruce EMA9/EMA21 en la misma dirección — M15 solo afina el momento de
   entrada, no genera trades propios.
3. Si se confirma: la **entrada** y el **stop loss** salen de la estructura en
   M15 (ajustado, cerca del precio — mejora mucho el ratio riesgo:beneficio),
   pero el **take profit** es el objetivo original calculado en H4/H1 (no se
   recalcula) — así el trade mantiene el objetivo de plazo más largo con un
   riesgo bien acotado.
4. Si nunca llega la confirmación: la idea pendiente se descarta si el precio
   ya tocó el stop loss, si aparece un cruce EMA contrario, o (como backstop
   de seguridad, no el criterio principal) si pasa demasiado tiempo sin
   confirmar — 4h para H4, 1.5h para H1.

Entrar directo en M15 con take profit cercano queda **descartado a propósito**:
da mal ratio riesgo:beneficio y muchas señales falsas por el ruido propio de
temporalidades chicas.

Cada señal también trae, solo a modo informativo (no descarta nada): el
**contexto EMA50/EMA100**, para ver si el precio está alineado con una
tendencia intermedia.

## Gestión de riesgo

Cada señal de trading sugiere cuánto arriesgar según la **regla del 2%**
(Elder, Muñoz): como no conocemos tu capital real, se expresa como un
múltiplo de tu capital total, no como un monto. Si ya veniste perdiendo ese
mismo día (según la bitácora), la sugerencia baja a la mitad por cada pérdida
cerrada — **anti-martingala**: nunca sube por ganancias, solo baja por
pérdidas.

También se sugiere un apalancamiento máximo (hasta 5x, configurable),
recortado para que el stop loss nunca represente más de la mitad de la
distancia estimada a la liquidación.

## Capa de tendencia (Diario y Semanal)

Independiente de la capa de trading: avisa cuando cambia la tendencia mayor
(cruce EMA50/EMA200 en Diario — el clásico "golden cross"/"death cross" —, y
EMA10/EMA30 en Semanal para visión de meses/años), confirmado con RCI de
período largo. No es una entrada puntual, es información para decidir si
conviene operar a favor o en contra de la tendencia de fondo.

## Bitácora y botones de Telegram

Cada señal (de cualquier capa) queda registrada en `trades.json`, sin
importar si la tomaste o no — así se puede medir la efectividad real del bot,
no solo lo que vos operaste. El bot revisa las velas siguientes y marca cada
trade como WIN o LOSS automáticamente en cuanto el precio toca el take profit
o el stop loss.

Las señales de trading (H4c/H1c) además traen dos botones en Telegram:
**"✅ Tomé el trade"** / **"❌ No lo tomé"**. Tu respuesta se guarda en la
bitácora (`user_response`). No es instantáneo: como el bot no queda
escuchando todo el tiempo, la respuesta se procesa en la próxima corrida del
cron (hasta ~15 min de demora).

## Preguntarle al bot si está en línea

En cualquier momento podés mandarle **"/estado"** al bot por Telegram y te
contesta si sigue corriendo, cuándo fue la última corrida, si hay ideas
pendientes, y si algún chequeo viene fallando. Tampoco es instantáneo (mismo
motivo que los botones: hasta ~15 min de demora).

Además, si algún chequeo (H4c, H1c, tendencia Diaria o Semanal) falla 3
corridas seguidas, el bot te avisa por Telegram directamente — no hace falta
que revises el mail de GitHub Actions. También avisa cuando se recupera.

## Reporte semanal

Todos los lunes (10am hora Argentina) llega un resumen por Telegram con la
cantidad de señales de trading de los últimos 7 días y el histórico completo:
winrate, resultado acumulado, y cuántas tomaste vos.

## Archivos

- `bot_btc_h4.py` — el bot (Python, solo depende de `requests`)
- `requirements.txt`
- `state.json` — estado del bot (ideas pendientes, últimas alertas, salud de cada chequeo)
- `trades.json` — la bitácora
- `.github/workflows/alertas_btc.yml` — corre el bot cada 15 minutos
- `.github/workflows/reporte_semanal.yml` — manda el reporte semanal los lunes
- `.github/workflows/test_telegram.yml` — prueba manual de la conexión con Telegram

## Paso 1 — Crear el bot de Telegram

1. Abrí Telegram y buscá **@BotFather**.
2. Enviale `/newbot` y seguí los pasos (nombre y username del bot).
3. Te va a dar un **token** con este formato: `123456789:ABCdefGhIJKlmNoPQRstuVWXyz`. Guardalo.
4. Buscá **@userinfobot**, iniciá el chat y te va a devolver tu **chat_id** (un número).
5. Muy importante: iniciá una conversación con tu bot (mandale cualquier mensaje, ej. `/start`) para habilitar que te pueda escribir.

## Paso 2 — Subir el proyecto a GitHub

Creá un repositorio (puede ser privado) y subí todos los archivos de la
sección anterior, manteniendo la estructura de carpetas (`.github/workflows/`
tiene que quedar en esa ruta exacta).

## Paso 3 — Configurar los secrets

En el repo: **Settings → Secrets and variables → Actions → New repository secret**

- `TELEGRAM_BOT_TOKEN` = el token del Paso 1
- `TELEGRAM_CHAT_ID` = tu chat_id del Paso 1

## Paso 4 — Activar los workflows

1. Andá a la pestaña **Actions** del repo (habilitá los workflows si te lo pide).
2. Corré manualmente **"Test Telegram"** primero, para confirmar que el token/chat_id están bien.
3. Corré manualmente **"Alertas BTC H4 + H1 + M15"** una vez, para confirmar que no tira errores.
4. A partir de ahí corre solo cada 15 minutos. El reporte semanal ("Reporte semanal BTC") corre solo los lunes, no hace falta tocarlo.

## Probarlo en tu propia PC (opcional)

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="tu_token"
export TELEGRAM_CHAT_ID="tu_chat_id"
python bot_btc_h4.py                # corre una vez (todas las capas)
python bot_btc_h4.py --loop         # corre en loop, revisando cada 1 min
python bot_btc_h4.py --test-telegram   # prueba la conexión con Telegram
python bot_btc_h4.py --weekly-report   # manda el reporte semanal ahora mismo
```

## Notas importantes

- Los datos vienen de la API pública de **Kraken** (`api.kraken.com`), sin necesidad de API key ni cuenta. (Se probó primero con Binance, pero Binance bloquea las IPs de los runners de GitHub Actions con un error 451).
- Esto es una herramienta de apoyo técnico, **no es asesoramiento financiero**. Es una fase de **validación**: la idea es acumular suficientes señales cerradas en la bitácora antes de pensar en operar con dinero real, y mucho menos en automatizar la ejecución.
- Si más adelante querés otra estrategia, otra temporalidad, u otro canal de notificación (WhatsApp, Discord), avisame y lo ajustamos — aunque ojo, WhatsApp Business API no es gratis para mensajes que inicia el bot (solo lo son las respuestas dentro de una conversación que arrancás vos), así que Telegram sigue siendo la opción sin costo.
