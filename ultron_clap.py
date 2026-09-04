#!/usr/bin/env python3
"""Escucha el microfono en segundo plano y reacciona a un aplauso:
1. Reproduce un saludo con voz masculina en espanol (edge-tts + miniaudio).
2. Abre webture.vercel.app en Brave.
3. Abre un video de YouTube en Brave.
"""

import asyncio
import collections
import os
import subprocess
import sys
import tempfile
import threading
import time

import edge_tts
import miniaudio
import numpy as np
import sounddevice as sd

VOICE = "es-ES-AlvaroNeural"
GREETING = "Hola Diego, vamos a cumplir las metas del dia, te espera un gran dia"
BRAVE_URL = "https://webture.vercel.app"
YOUTUBE_URL = "https://www.youtube.com/watch?v=21puudq4H7g&list=RD21puudq4H7g&start_radio=1"

SAMPLE_RATE = 44100
BLOCK_SIZE = 1024
CHANNELS = 1

BASELINE_BLOCKS = 40      # ventana de referencia de ruido ambiente (~0.9s)
MIN_PEAK = 0.15           # piso absoluto de amplitud para considerar un pico
PEAK_RATIO = 4.0          # el pico debe superar el ruido ambiente por este factor
COOLDOWN_SECONDS = 3.0    # ignora nuevos aplausos durante este tiempo tras disparar

LOG_PREFIX = "[ultron-clap]"


def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", flush=True)


def open_url(url: str) -> None:
    subprocess.Popen(["open", "-a", "Brave Browser", url])


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
    next(stream)  # arranca el generador
    device = miniaudio.PlaybackDevice(
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=info.nchannels,
        sample_rate=info.sample_rate,
    )
    device.start(stream)
    time.sleep(info.duration + 0.4)
    device.stop()


def react_to_clap() -> None:
    log("¡Aplauso detectado! Ejecutando reaccion...")

    mp3_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            mp3_path = tmp.name
        synthesize(GREETING, VOICE, mp3_path)
        play_mp3(mp3_path)
    except Exception as exc:
        log(f"Error generando/reproduciendo voz: {exc!r}")
    finally:
        if mp3_path:
            try:
                os.remove(mp3_path)
            except OSError:
                pass

    try:
        open_url(BRAVE_URL)
    except Exception as exc:
        log(f"Error abriendo {BRAVE_URL}: {exc!r}")

    try:
        open_url(YOUTUBE_URL)
    except Exception as exc:
        log(f"Error abriendo YouTube: {exc!r}")

    log("Reaccion completada.")


def main() -> None:
    log(f"Escuchando microfono ({SAMPLE_RATE} Hz, bloque {BLOCK_SIZE})... Ctrl+C para detener.")

    baseline = collections.deque(maxlen=BASELINE_BLOCKS)
    state = {"last_trigger": 0.0, "paused_until": 0.0}

    def handle_clap() -> None:
        state["paused_until"] = time.monotonic() + 1e9  # pausa mientras reacciona
        try:
            react_to_clap()
        finally:
            baseline.clear()
            state["paused_until"] = time.monotonic() + 1.0

    def callback(indata, frames, time_info, status):
        if status:
            log(f"Estado de audio: {status}")

        now = time.monotonic()
        if now < state["paused_until"]:
            return

        peak = float(np.max(np.abs(indata)))
        avg_baseline = (sum(baseline) / len(baseline)) if baseline else 0.0

        is_clap = (
            peak > MIN_PEAK
            and (avg_baseline < 1e-4 or peak > avg_baseline * PEAK_RATIO)
            and (now - state["last_trigger"]) > COOLDOWN_SECONDS
        )

        if peak < MIN_PEAK:
            baseline.append(peak)

        if is_clap:
            state["last_trigger"] = now
            log(f"Pico detectado: {peak:.3f} (ruido base {avg_baseline:.4f})")
            threading.Thread(target=handle_clap, daemon=True).start()

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        blocksize=BLOCK_SIZE,
        channels=CHANNELS,
        dtype="float32",
        callback=callback,
    ):
        while True:
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Detenido por el usuario.")
        sys.exit(0)
