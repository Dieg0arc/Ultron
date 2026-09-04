#!/usr/bin/env python3
"""Ventana del HUD de Ultron: pywebview sin bordes, siempre encima, que
pinta `hud/index.html`. Ese HTML se conecta por WebSocket a
ws://localhost:8765 (levantado por `ultron_voice.py`) y refleja en vivo
el estado del asistente (escuchando / pensando / hablando).

Se puede lanzar solo (`python3 ultron_hud.py`) para ver el HUD en modo
demo -- sin `ultron_voice.py` corriendo, el HTML detecta que no hay
servidor y se queda en un estado "reconectando" con la misma estetica.
"""

import os
import sys

import webview

HUD_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hud", "index.html")


def main() -> None:
    if not os.path.exists(HUD_HTML):
        print(f"[ultron-hud] No encuentro {HUD_HTML}", file=sys.stderr)
        sys.exit(1)

    window = webview.create_window(
        "Ultron",
        HUD_HTML,
        width=520,
        height=520,
        frameless=True,
        easy_drag=True,
        on_top=True,
        transparent=True,
        background_color="#0c1014",
    )
    webview.start()


if __name__ == "__main__":
    main()
