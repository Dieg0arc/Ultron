# Jarvis

Asistente de voz local para macOS. Corre en segundo plano escuchando el
microfono y espera a que le digas **"estas ahi jarvis?"**.

## Flujo

1. Vos: **"estas ahi jarvis?"**
   Jarvis: *"Claro que si, señor. ¿Comenzamos?"*
2. Respondes:
   - **"si"**:
     1. Dice un saludo (con variantes aleatorias).
     2. Pregunta si quiere reproducir tu playlist de Spotify. Si decis
        que si, la abre y le da play.
     3. Pregunta con que queres comenzar: **"trabajo"** o **"paginas"**.
        - `trabajo` -> abre el link de Google Meet en Brave.
        - `paginas` -> abre Webture y tu GitHub en Brave.
   - **"no"**: cancela y vuelve a esperar.

En cualquier momento en que Jarvis este escuchando, decir **"no mas por
hoy jarvis"** hace que responda *"Okay, señor"* y cancele lo que este
preguntando/haciendo, sin dejar de escuchar.

## Como funciona

- `sounddevice` mantiene un unico stream de microfono continuo,
  alimentando un buffer circular en memoria.
- `faster-whisper` (modelo `small`, local, sin internet) transcribe
  ventanas de ese buffer para detectar las frases.
- `edge-tts` + `miniaudio` generan y reproducen la voz de Jarvis
  (`es-ES-AlvaroNeural`).
- `osascript` controla el volumen del sistema (para no contaminar el
  microfono mientras algo esta sonando) y Spotify.

## Uso

```bash
python3 -m venv venv
source venv/bin/activate
pip install sounddevice numpy edge-tts miniaudio faster-whisper

python3 jarvis_voice.py
```

Corre en primer plano por defecto; para dejarlo en segundo plano:

```bash
nohup python3 jarvis_voice.py > jarvis_voice.log 2>&1 & disown
```

Para detenerlo: `pkill -f jarvis_voice.py`.

## Requisitos

- macOS (usa `open`/`osascript` para controlar Brave y Spotify).
- [Brave Browser](https://brave.com/) y [Spotify](https://www.spotify.com/) instalados.
- Permiso de microfono para la terminal desde la que se ejecute.
