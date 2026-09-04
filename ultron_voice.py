#!/usr/bin/env python3
"""Ultron: escucha el microfono en segundo plano esperando "estas ahi ultron?".

Flujo:

  1. Usuario: "estas ahi ultron?"
     Ultron:  "Claro que si, señor. ¿Comenzamos?"
  2. Usuario responde:
     - "si"  -> arranca el ciclo del dia:
         a. Dice el saludo.
         b. Pregunta "señor, ¿quiere que reproduzca su playlist?". Si
            dice "si", abre Spotify y le da play a la playlist; si
            dice "no", no reproduce nada.
         c. Pregunta "con que comenzamos hoy, trabajo o paginas".
         d. Escucha la respuesta:
            - "trabajo"  -> abre el link de Google Meet en Brave.
            - "paginas"  -> abre webture.vercel.app y github.com/Dieg0arc.
            - si no entiende, repite la pregunta una vez; si sigue sin
              entender, cancela y vuelve a esperar "estas ahi ultron?".
     - "no"  -> cancela este arranque y vuelve a esperar "estas ahi ultron?".
     - no entendido -> repite "¿comenzamos?" una vez; si sigue sin
       entender, cancela.

  En CUALQUIER momento en que Ultron este escuchando (esperando el saludo,
  confirmando "¿comenzamos?", preguntando por la musica, o esperando
  "trabajo/paginas"), si el usuario dice "no mas por hoy ultron", Ultron
  responde "okay, señor", CANCELA lo que este preguntando/haciendo en ese
  momento y vuelve a esperar "estas ahi ultron?". El microfono NUNCA se
  apaga por esto -- Ultron sigue escuchando siempre, solo deja de
  insistir con la pregunta actual.

Arquitectura de audio: un unico InputStream continuo alimenta un buffer
circular (RingBuffer), del cual se leen ventanas periodicas o de duracion
fija para transcribir con Whisper. Todo corre en un solo proceso/hilo
principal, sin abrir el microfono en modo exclusivo mas de una vez.

Arquitectura del HUD: un HudBus levanta un servidor WebSocket local
(ws://localhost:8765) y transmite el estado actual ("booting", "listening",
"thinking", "speaking") a quien este conectado. `ultron_hud.py` (proceso
aparte, ventana pywebview) es el consumidor tipico: se lanza automaticamente
al arrancar y pinta un HUD que refleja en vivo si Ultron esta escuchando,
procesando o hablando -- incluyendo la envolvente de amplitud real de cada
frase para que el nucleo reaccione a la voz, no a una animacion falsa.
"""

import asyncio
import difflib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata

import edge_tts
import miniaudio
import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

try:
    import websockets
except ImportError:  # pragma: no cover - el HUD es opcional
    websockets = None

# --- Voz / frases -----------------------------------------------------
VOICE = "es-ES-AlvaroNeural"
PRESENCE_REPLY = "Claro que si, señor. ¿Comenzamos?"
CONFIRM_REPEAT = "¿Comenzamos? Digame si o no."

GREETING_VARIANTS = [
    "Hola, señor. Vamos a cumplir las metas del dia, le espera un gran dia.",
    "Buenos dias, señor. ¿Como esta? Espero que hoy este muy bien.",
    "Hola, señor. Espero que haya descansado bien, hoy nos espera un gran dia.",
    "Señor, es un placer. Vamos a por las metas de hoy, sera un gran dia.",
    "Buenos dias, señor. Que tenga un excelente dia, vamos a darlo todo.",
    "Hola, señor. Listo para arrancar, espero que tenga un dia muy productivo.",
]

MUSIC_PROMPT = "Señor, ¿quiere que reproduzca su playlist?"
MUSIC_PROMPT_REPEAT = "¿Reproduzco la playlist? Digame si o no."

QUESTION = "Bueno, con que comenzamos hoy, con trabajo o quieres hacer paginas?"
REPEAT_QUESTION = "Perdon, no te entendi. Trabajo, o paginas?"

CANCEL_ACK = "Okay, señor."


def random_greeting() -> str:
    return random.choice(GREETING_VARIANTS)

# --- URLs / apps ------------------------------------------------------------
SPOTIFY_PLAYLIST_URI = "spotify:playlist:33Ha8qpbbj5h0lKrFBdtu5"
MEET_URL = "https://meet.google.com/uhi-xjix-yeq?pli=1&authuser=5"
WEBTURE_URL = "https://webture.vercel.app"
GITHUB_URL = "https://github.com/Dieg0arc"

# --- Audio general ----------------------------------------------------------
SAMPLE_RATE = 16000
BLOCK_SIZE = 1024  # ~64ms por bloque del stream continuo
RING_CAPACITY_SECONDS = 12

# --- Deteccion de frase de voz (por lotes, sobre el buffer) ----------------
WAKE_POLL_INTERVAL = 2.0
WAKE_WINDOW_SECONDS = 4.0
SILENCE_RMS_FLOOR = 0.004  # bajo esto, ni se molesta en transcribir

# --- Fases de escucha de duracion fija --------------------------------------
CONFIRM_CHUNK_SECONDS = 5   # ventana para "si"/"no"
CHOICE_CHUNK_SECONDS = 7    # ventana para "trabajo"/"paginas"
PRE_LISTEN_DELAY = 0.6      # pausa tras hablar antes de empezar a "escuchar"
CHOICE_PEAK_FLOOR = 0.015   # bajo este pico, se asume silencio total

WHISPER_MODEL_SIZE = "small"
LANGUAGE = "es"
STT_PROMPT = "¿Estás ahí, Ultron? Sí. No. No más por hoy, Ultron. Trabajo o páginas."

LOG_PREFIX = "[ultron]"


def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", flush=True)


# --- HUD (WebSocket) ----------------------------------------------------------
HUD_WS_HOST = "localhost"
HUD_WS_PORT = 8765
HUD_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ultron_hud.py")


class HudBus:
    """Servidor WebSocket local que transmite el estado de Ultron al HUD.

    Corre en su propio hilo/loop de asyncio para no interferir con el hilo
    principal (que hace I/O de audio bloqueante). Si `websockets` no esta
    instalado o el HUD no esta corriendo, `broadcast` simplemente no tiene
    a quien mandarle nada y Ultron sigue funcionando igual sin HUD.
    """

    def __init__(self, host: str = HUD_WS_HOST, port: int = HUD_WS_PORT):
        self.host = host
        self.port = port
        self._clients = set()
        self._loop = None
        self._last_payload = {"state": "booting", "ts": time.time()}
        self._ready = threading.Event()
        self.paused = threading.Event()

    def start(self) -> None:
        if websockets is None:
            log("Modulo 'websockets' no instalado: el HUD no recibira estado.")
            return
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(timeout=5)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as exc:
            log(f"Error en servidor del HUD: {exc!r}")

    async def _serve(self) -> None:
        async def handler(ws, *_args):
            self._clients.add(ws)
            try:
                await ws.send(json.dumps(self._last_payload))
                async for raw in ws:
                    self._handle_message(raw)
            except Exception:
                pass
            finally:
                self._clients.discard(ws)

        async with websockets.serve(handler, self.host, self.port):
            self._ready.set()
            await asyncio.Future()  # correr para siempre

    def _handle_message(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if msg.get("cmd") != "toggle_pause":
            return
        if self.paused.is_set():
            self.paused.clear()
            log("Reanudado desde el HUD.")
            self.set_state("listening")
        else:
            self.paused.set()
            log("Pausado desde el HUD.")
            self.set_state("paused")

    def set_state(self, state: str, **extra) -> None:
        payload = {"state": state, "ts": time.time(), **extra}
        self._last_payload = payload
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._broadcast(payload), self._loop)

    async def _broadcast(self, payload: dict) -> None:
        if not self._clients:
            return
        data = json.dumps(payload)
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)


hud = HudBus()


def launch_hud() -> subprocess.Popen | None:
    """Lanza `ultron_hud.py` (ventana pywebview) como proceso aparte."""
    if not os.path.exists(HUD_SCRIPT):
        return None
    try:
        return subprocess.Popen([sys.executable, HUD_SCRIPT])
    except Exception as exc:
        log(f"No se pudo lanzar el HUD: {exc!r}")
        return None


# --- Buffer circular de audio ------------------------------------------------
class RingBuffer:
    def __init__(self, capacity_samples: int):
        self.capacity = capacity_samples
        self.buf = np.zeros(capacity_samples, dtype="float32")
        self.write_pos = 0
        self.total_written = 0
        self.lock = threading.Lock()

    def write(self, data: np.ndarray) -> None:
        with self.lock:
            n = len(data)
            if n >= self.capacity:
                self.buf[:] = data[-self.capacity:]
                self.write_pos = 0
            else:
                end = self.write_pos + n
                if end <= self.capacity:
                    self.buf[self.write_pos:end] = data
                else:
                    first = self.capacity - self.write_pos
                    self.buf[self.write_pos:] = data[:first]
                    self.buf[: end - self.capacity] = data[first:]
                self.write_pos = end % self.capacity
            self.total_written += n

    def read_last(self, n_samples: int) -> np.ndarray:
        with self.lock:
            n_samples = min(n_samples, self.capacity, self.total_written)
            if n_samples <= 0:
                return np.zeros(0, dtype="float32")
            start = (self.write_pos - n_samples) % self.capacity
            if start + n_samples <= self.capacity:
                return self.buf[start : start + n_samples].copy()
            first = self.capacity - start
            return np.concatenate([self.buf[start:], self.buf[: n_samples - first]])


# --- Texto ------------------------------------------------------------------
def normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_text = nfkd.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.lower()
    return re.sub(r"[^a-z0-9 ]+", " ", ascii_text)


def word_close_to(word: str, target: str, threshold: float = 0.65) -> bool:
    return difflib.SequenceMatcher(None, word, target).ratio() >= threshold


def any_word_close(words, target: str, threshold: float = 0.65) -> bool:
    return any(word_close_to(w, target, threshold) for w in words)


def has_presence_wake(norm_text: str) -> bool:
    """'estas ahi ultron?'"""
    words = norm_text.split()
    has_query = "estas" in words or "ahi" in words
    has_ultron = "ultron" in words or any_word_close(words, "ultron")
    return has_query and has_ultron


def has_shutdown_phrase(norm_text: str) -> bool:
    """'no mas por hoy ultron'"""
    words = norm_text.split()
    has_no = "no" in words
    has_mas = "mas" in words
    has_ultron = "ultron" in words or any_word_close(words, "ultron")
    return has_no and has_mas and has_ultron


def parse_yes_no(norm_text: str):
    words = norm_text.split()
    has_si = "si" in words
    has_no = "no" in words
    if has_si and not has_no:
        return "si"
    if has_no and not has_si:
        return "no"
    return None


# --- Sistema ------------------------------------------------------------------
def open_url(url: str) -> None:
    subprocess.Popen(["open", "-a", "Brave Browser", url])


def play_spotify_uri(uri: str) -> None:
    """Lanza Spotify (si no esta abierto) y fuerza la reproduccion del uri
    dado (cancion, playlist o album). `open` con un uri de playlist solo
    navega sin dar play, asi que usamos AppleScript para forzarlo."""
    subprocess.run(
        ["osascript", "-e", f'tell application "Spotify" to play track "{uri}"'],
        capture_output=True,
        text=True,
    )


def get_system_volume():
    result = subprocess.run(
        ["osascript", "-e", "output volume of (get volume settings)"],
        capture_output=True,
        text=True,
    )
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def set_system_volume(volume: int) -> None:
    subprocess.run(["osascript", "-e", f"set volume output volume {volume}"])


# --- Voz (TTS) ----------------------------------------------------------------
async def _synthesize(text: str, voice: str, out_path: str) -> None:
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(out_path)


def synthesize(text: str, voice: str, out_path: str) -> None:
    asyncio.run(_synthesize(text, voice, out_path))


def play_mp3(path: str) -> None:
    info = miniaudio.mp3_get_file_info(path)
    stream = miniaudio.stream_file(
        path,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=info.nchannels,
        sample_rate=info.sample_rate,
    )
    next(stream)
    device = miniaudio.PlaybackDevice(
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=info.nchannels,
        sample_rate=info.sample_rate,
    )
    device.start(stream)
    time.sleep(info.duration + 0.4)
    device.stop()


VOICE_ENVELOPE_CHUNK_MS = 60


def voice_envelope(path: str, chunk_ms: int = VOICE_ENVELOPE_CHUNK_MS):
    """Decodifica el mp3 y devuelve una envolvente de amplitud (0..1) por
    ventanas de `chunk_ms`, para que el HUD reaccione a la voz real en vez
    de una animacion inventada."""
    decoded = miniaudio.decode_file(path, output_format=miniaudio.SampleFormat.FLOAT32)
    samples = np.array(decoded.samples, dtype="float32")
    if decoded.nchannels > 1:
        samples = samples.reshape(-1, decoded.nchannels).mean(axis=1)

    chunk = max(1, int(decoded.sample_rate * chunk_ms / 1000))
    levels = []
    for i in range(0, len(samples), chunk):
        seg = samples[i : i + chunk]
        if len(seg) == 0:
            continue
        levels.append(float(np.sqrt(np.mean(np.square(seg)))))

    if levels:
        peak = max(levels) or 1.0
        # curva perceptual: comprime los picos, realza los niveles bajos
        levels = [min(1.0, (lvl / peak) ** 0.6) for lvl in levels]
    return levels


def broadcast_voice_envelope(levels, chunk_ms: int = VOICE_ENVELOPE_CHUNK_MS) -> None:
    interval = chunk_ms / 1000.0
    for level in levels:
        hud.set_state("speaking", level=round(level, 3))
        time.sleep(interval)


def say(text: str) -> None:
    mp3_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            mp3_path = tmp.name
        synthesize(text, VOICE, mp3_path)

        levels = []
        try:
            levels = voice_envelope(mp3_path)
        except Exception as exc:
            log(f"No se pudo calcular la envolvente de voz: {exc!r}")

        hud.set_state("speaking", text=text, level=0.0)
        if levels:
            threading.Thread(
                target=broadcast_voice_envelope, args=(levels,), daemon=True
            ).start()

        play_mp3(mp3_path)
    except Exception as exc:
        log(f"Error generando/reproduciendo voz: {exc!r}")
    finally:
        hud.set_state("listening")
        if mp3_path:
            try:
                os.remove(mp3_path)
            except OSError:
                pass


# --- STT ------------------------------------------------------------------
def transcribe(model: WhisperModel, audio: np.ndarray, vad_filter: bool = True) -> str:
    segments, _info = model.transcribe(
        audio,
        language=LANGUAGE,
        beam_size=1,
        vad_filter=vad_filter,
        initial_prompt=STT_PROMPT,
    )
    return " ".join(seg.text for seg in segments).strip()


# --- Espera de "estas ahi ultron?" / "no mas por hoy ultron" ---------------
def wait_for_presence_or_shutdown(model: WhisperModel, ring: RingBuffer) -> str:
    log("Esperando 'estas ahi ultron?'...")
    while True:
        if hud.paused.is_set():
            hud.set_state("paused")
            time.sleep(WAKE_POLL_INTERVAL)
            continue

        audio = ring.read_last(int(WAKE_WINDOW_SECONDS * SAMPLE_RATE))
        if len(audio) == 0:
            time.sleep(WAKE_POLL_INTERVAL)
            continue
        rms = float(np.sqrt(np.mean(np.square(audio))))
        if rms < SILENCE_RMS_FLOOR:
            time.sleep(WAKE_POLL_INTERVAL)
            continue

        hud.set_state("thinking")
        text = transcribe(model, audio)
        hud.set_state("listening")
        if text:
            log(f"oido: {text!r}")
            norm = normalize(text)
            if has_shutdown_phrase(norm):
                return "cancelar"
            if has_presence_wake(norm):
                return "presencia"

        time.sleep(WAKE_POLL_INTERVAL)


# --- Confirmacion "si"/"no" ---------------------------------------------------
def listen_confirm_once(model: WhisperModel, ring: RingBuffer):
    log(f"Escuchando confirmacion (si / no), ventana {CONFIRM_CHUNK_SECONDS}s...")
    time.sleep(CONFIRM_CHUNK_SECONDS)
    audio = ring.read_last(int(CONFIRM_CHUNK_SECONDS * SAMPLE_RATE))
    rms = float(np.sqrt(np.mean(np.square(audio)))) if len(audio) else 0.0
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    log(f"nivel de audio -> rms={rms:.4f} peak={peak:.4f}")

    if peak < CHOICE_PEAK_FLOOR:
        log("No se detecto voz (silencio).")
        return None

    hud.set_state("thinking")
    text = transcribe(model, audio, vad_filter=False)
    hud.set_state("listening")
    norm = normalize(text)
    log(f"oido confirmacion: {text!r}")

    if has_shutdown_phrase(norm):
        return "cancelar"
    return parse_yes_no(norm)


def confirm_or_shutdown(model: WhisperModel, ring: RingBuffer):
    result = listen_confirm_once(model, ring)
    if result is not None:
        return result

    say(CONFIRM_REPEAT)
    time.sleep(PRE_LISTEN_DELAY)
    result = listen_confirm_once(model, ring)
    return result  # puede ser None -> se trata como cancelar


# --- Confirmacion de musica "si"/"no" -----------------------------------------
def listen_music_confirm_once(model: WhisperModel, ring: RingBuffer):
    log(f"Escuchando si reproducir musica (si / no), ventana {CONFIRM_CHUNK_SECONDS}s...")
    time.sleep(CONFIRM_CHUNK_SECONDS)
    audio = ring.read_last(int(CONFIRM_CHUNK_SECONDS * SAMPLE_RATE))
    rms = float(np.sqrt(np.mean(np.square(audio)))) if len(audio) else 0.0
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    log(f"nivel de audio -> rms={rms:.4f} peak={peak:.4f}")

    if peak < CHOICE_PEAK_FLOOR:
        log("No se detecto voz (silencio).")
        return None

    hud.set_state("thinking")
    text = transcribe(model, audio, vad_filter=False)
    hud.set_state("listening")
    norm = normalize(text)
    log(f"oido musica: {text!r}")

    if has_shutdown_phrase(norm):
        return "cancelar"
    return parse_yes_no(norm)


def confirm_music(model: WhisperModel, ring: RingBuffer):
    result = listen_music_confirm_once(model, ring)
    if result is not None:
        return result

    say(MUSIC_PROMPT_REPEAT)
    time.sleep(PRE_LISTEN_DELAY)
    result = listen_music_confirm_once(model, ring)
    return result  # puede ser None -> se trata como "no"


# --- Fase de respuesta (trabajo / paginas) -----------------------------------
def ask_choice_once(model: WhisperModel, ring: RingBuffer):
    log(f"Escuchando respuesta (trabajo / paginas), ventana {CHOICE_CHUNK_SECONDS}s...")

    prev_volume = get_system_volume()
    if prev_volume is not None:
        set_system_volume(0)
    try:
        time.sleep(CHOICE_CHUNK_SECONDS)
    finally:
        if prev_volume is not None:
            set_system_volume(prev_volume)

    audio = ring.read_last(int(CHOICE_CHUNK_SECONDS * SAMPLE_RATE))
    rms = float(np.sqrt(np.mean(np.square(audio)))) if len(audio) else 0.0
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    log(f"nivel de audio -> rms={rms:.4f} peak={peak:.4f}")

    if peak < CHOICE_PEAK_FLOOR:
        log("No se detecto voz (silencio). No transcribo para evitar alucinaciones.")
        return None

    hud.set_state("thinking")
    text = transcribe(model, audio, vad_filter=False)
    hud.set_state("listening")
    norm = normalize(text)
    log(f"oido respuesta: {text!r}")

    if has_shutdown_phrase(norm):
        return "cancelar"

    words = norm.split()
    has_trabajo = "trabajo" in norm or any_word_close(words, "trabajo")
    has_paginas = "pagina" in norm or any_word_close(words, "paginas")

    if has_trabajo and not has_paginas:
        return "trabajo"
    if has_paginas and not has_trabajo:
        return "paginas"
    return None


def handle_choice(model: WhisperModel, ring: RingBuffer):
    choice = ask_choice_once(model, ring)

    if choice == "cancelar":
        return "cancelar"

    if choice is None:
        say(REPEAT_QUESTION)
        time.sleep(PRE_LISTEN_DELAY)
        choice = ask_choice_once(model, ring)
        if choice == "cancelar":
            return "cancelar"

    if choice == "trabajo":
        log("Opcion elegida: trabajo -> abriendo Google Meet")
        open_url(MEET_URL)
        return "trabajo"
    elif choice == "paginas":
        log("Opcion elegida: paginas -> abriendo Webture y GitHub")
        open_url(WEBTURE_URL)
        open_url(GITHUB_URL)
        return "paginas"
    else:
        log("No entendi la respuesta dos veces. Cancelo y vuelvo a esperar el disparador.")
        return None


# --- Main ------------------------------------------------------------------
def main() -> None:
    hud.start()
    hud_process = launch_hud()

    hud.set_state("booting")
    log(f"Cargando modelo Whisper ({WHISPER_MODEL_SIZE})...")
    model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    log("Modelo listo.")

    ring = RingBuffer(SAMPLE_RATE * RING_CAPACITY_SECONDS)

    def audio_callback(indata, frames, time_info, status):
        if status:
            log(f"Estado de audio: {status}")
        mono = indata[:, 0] if indata.ndim > 1 else indata
        ring.write(mono.astype("float32", copy=True))

    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        blocksize=BLOCK_SIZE,
        channels=1,
        dtype="float32",
        callback=audio_callback,
    )
    stream.start()
    hud.set_state("listening")
    log("Ultron activo. Di 'estas ahi ultron?' para comenzar.")

    try:
        while True:
            trigger = wait_for_presence_or_shutdown(model, ring)
            if trigger == "cancelar":
                say(CANCEL_ACK)
                log("'No mas por hoy ultron' detectado en espera. Sigo escuchando 'estas ahi ultron?'.")
                continue

            log("Presencia detectada.")
            say(PRESENCE_REPLY)
            answer = confirm_or_shutdown(model, ring)

            if answer == "cancelar":
                say(CANCEL_ACK)
                log("'No mas por hoy ultron' detectado en confirmacion. Cancelo y sigo escuchando.")
            elif answer == "si":
                say(random_greeting())
                time.sleep(PRE_LISTEN_DELAY)

                say(MUSIC_PROMPT)
                music_answer = confirm_music(model, ring)

                if music_answer == "cancelar":
                    say(CANCEL_ACK)
                    log("'No mas por hoy ultron' detectado al preguntar musica. Cancelo y sigo escuchando.")
                    continue

                if music_answer == "si":
                    log("Reproduciendo la playlist en Spotify.")
                    play_spotify_uri(SPOTIFY_PLAYLIST_URI)
                else:
                    log("No se reproduce musica.")

                time.sleep(PRE_LISTEN_DELAY)
                say(QUESTION)
                time.sleep(PRE_LISTEN_DELAY)
                result = handle_choice(model, ring)
                if result == "cancelar":
                    say(CANCEL_ACK)
                    log("'No mas por hoy ultron' detectado durante el ciclo. Cancelo y sigo escuchando.")
            elif answer == "no":
                log("Usuario dijo que no. Vuelvo a esperar 'estas ahi ultron?'.")
            else:
                log("No entendi si comenzamos. Vuelvo a esperar 'estas ahi ultron?'.")
    finally:
        stream.stop()
        stream.close()
        if hud_process is not None:
            hud_process.terminate()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Detenido por el usuario.")
        sys.exit(0)
