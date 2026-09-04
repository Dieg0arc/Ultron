# Ultron

Asistente de voz local para macOS. Corre en segundo plano escuchando el
microfono y espera a que le digas **"estas ahi ultron?"**. Además levanta un
HUD visual estilo J.A.R.V.I.S. que muestra en vivo si está escuchando,
procesando o hablando.

## Flujo

1. Vos: **"estas ahi ultron?"**
   Ultron: *"Claro que si, señor. ¿Comenzamos?"*
2. Respondes:
   - **"si"**:
     1. Dice un saludo (con variantes aleatorias).
     2. Pregunta si quiere reproducir tu playlist de Spotify. Si decis
        que si, la abre y le da play.
     3. Pregunta con que queres comenzar: **"trabajo"** o **"paginas"**.
        - `trabajo` -> abre el link de Google Meet en Brave.
        - `paginas` -> abre Webture y tu GitHub en Brave.
   - **"no"**: cancela y vuelve a esperar.

En cualquier momento en que Ultron este escuchando, decir **"no mas por
hoy ultron"** hace que responda *"Okay, señor"* y cancele lo que este
preguntando/haciendo, sin dejar de escuchar.

## HUD visual

Al arrancar, `ultron_voice.py` levanta un servidor WebSocket local
(`ws://localhost:8765`) y lanza automáticamente `ultron_hud.py`: una
ventana pywebview sin bordes, siempre encima, con un núcleo tipo
arc-reactor (`hud/index.html`) que cambia de aspecto según el estado:

- **Escuchando** (cian, respiración lenta): esperando que hables.
- **Procesando** (ámbar, barrido rápido): transcribiendo lo que dijiste.
- **Hablando** (blanco/cian, núcleo reactivo): el core reacciona a la
  **envolvente de amplitud real** de la voz sintetizada, no a una
  animación inventada — así se nota exactamente cuándo puede escucharte
  y cuándo está hablando.

Si `ultron_voice.py` no está corriendo (o el HUD pierde la conexión), el
HUD se queda en un estado "reconectando" con la misma estética, y
reintenta solo. Podés lanzar el HUD por separado en modo demo con
`python3 ultron_hud.py`.

**Botón de pausa**: abajo a la izquierda del HUD hay un botón (⏸ / ▶) que
manda `{"cmd":"toggle_pause"}` por el mismo WebSocket. Al pausar, Ultron
deja de reaccionar a "estás ahí, Ultron?" (el núcleo se apaga a gris y la
etiqueta pasa a **PAUSADO**) sin cortar en seco una transcripción que ya
estuviera en curso — el corte se aplica en el próximo chequeo del loop de
escucha (cada ~2s). Tocarlo de nuevo reanuda. Si el HUD está "reconectando"
o arrancando, el botón se ve apagado porque no hay con quién hablar.

## Como funciona

- `sounddevice` mantiene un unico stream de microfono continuo,
  alimentando un buffer circular en memoria.
- `faster-whisper` (modelo `small`, local, sin internet) transcribe
  ventanas de ese buffer para detectar las frases.
- `edge-tts` + `miniaudio` generan y reproducen la voz de Ultron
  (`es-ES-AlvaroNeural`).
- `osascript` controla el volumen del sistema (para no contaminar el
  microfono mientras algo esta sonando) y Spotify.
- `websockets` transmite el estado (escuchando/procesando/hablando) al
  HUD; `pywebview` (con PyObjC/WebKit) pinta la ventana nativa.

## Uso

```bash
python3 -m venv venv
source venv/bin/activate
pip install sounddevice numpy edge-tts miniaudio faster-whisper \
    websockets pywebview pyobjc-framework-Cocoa pyobjc-framework-WebKit

python3 ultron_voice.py
```

Corre en primer plano por defecto (y abre el HUD junto con él); para
dejarlo en segundo plano:

```bash
nohup python3 ultron_voice.py > ultron_voice.log 2>&1 & disown
```

Para detenerlo: `pkill -f ultron_voice.py; pkill -f ultron_hud.py`.

> **Ojo con procesos viejos tras un rename**: si el script se venía
> ejecutando desde el archivo con el nombre anterior (`jarvis_voice.py`),
> matar por el nombre nuevo no lo toca — el proceso sigue vivo con el
> mic abierto aunque el archivo ya no exista con ese nombre en disco. Si
> Ultron parece seguir "escuchando" después de apagarlo, revisá con
> `ps aux | grep -iE "jarvis|ultron"` y matalo por PID.

## Requisitos

- macOS (usa `open`/`osascript` para controlar Brave y Spotify, y
  PyObjC/WebKit para el HUD).
- [Brave Browser](https://brave.com/) y [Spotify](https://www.spotify.com/) instalados.
- Permiso de microfono para la terminal desde la que se ejecute.

## Bitácora: qué se hizo hasta ahora

- Proyecto renombrado de Jarvis a **Ultron** (`jarvis_voice.py` ->
  `ultron_voice.py`, `jarvis_clap.py` -> `ultron_clap.py`).
- Flujo de voz completo: wake word ("estás ahí, Ultron?"), confirmación,
  saludo, pregunta de playlist de Spotify, elección trabajo/páginas, y la
  frase de apagado ("no más por hoy, Ultron") que cancela sin dejar de
  escuchar.
- HUD visual (`hud/index.html` + `ultron_hud.py`, ventana pywebview sin
  bordes) conectado por WebSocket a `ultron_voice.py`, con estados
  Escuchando / Procesando / Hablando y reacción en vivo a la envolvente
  real de la voz sintetizada.
- Botón de pausa/reanudar en el HUD, con su comando de ida y vuelta por el
  mismo WebSocket (ver arriba).
- Probado end-to-end corriendo en segundo plano (`nohup ... & disown`),
  con el HUD abriéndose solo y respondiendo a voz real.
- Detectado y resuelto un proceso viejo (`jarvis_voice.py`, previo al
  rename) que había quedado corriendo en segundo plano por horas después
  del cambio de nombre, ignorando los `pkill` apuntados al nombre nuevo —
  documentado en "Uso" para no repetirlo.

## Visión: el objetivo final

La idea de fondo es que Ultron deje de ser un árbol de decisiones con
frases fijas y se convierta en algo más parecido al **JARVIS de Tony
Stark**: poder hablarle en lenguaje natural, en cualquier momento y sobre
cualquier tema, y que él entienda y conteste como en una conversación real
-- no solo elegir entre "trabajo" o "páginas".

Para eso el flujo actual (wake word -> Whisper -> lógica fija -> TTS) va a
necesitar sumar una capa de razonamiento real: en vez de solo comparar la
transcripción contra frases esperadas, mandarla a un modelo de lenguaje
(LLM) que genere la respuesta, y sintetizar esa respuesta con `edge-tts`
como ya se hace. La infraestructura que ya existe (mic en buffer circular,
Whisper local, TTS, HUD con estados y ahora pausa) queda como la base;
lo que falta es reemplazar la lógica de "árbol de frases" por una
conversación abierta con memoria de contexto.
