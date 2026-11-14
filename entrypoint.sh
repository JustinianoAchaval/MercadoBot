#!/usr/bin/env bash
set -e

export DISPLAY=${DISPLAY:-:99}
SCREEN_RESOLUTION=${SCREEN_RESOLUTION:-1920x1080x24}
VNC_PORT=${VNC_PORT:-5900}
NOVNC_PORT=${NOVNC_PORT:-6080}

echo "[entrypoint] Limpiando locks previos de X11 y Chromium..."
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
rm -f /app/chrome_profile/SingletonLock /app/chrome_profile/SingletonCookie /app/chrome_profile/SingletonSocket

mkdir -p /app/data /app/screenshots /app/chrome_profile

echo "[entrypoint] Iniciando Xvfb en DISPLAY=${DISPLAY} (${SCREEN_RESOLUTION})..."
Xvfb "${DISPLAY}" -screen 0 "${SCREEN_RESOLUTION}" -ac +extension GLX +render -noreset &
sleep 1

echo "[entrypoint] Iniciando gestor de ventanas ligero Fluxbox..."
fluxbox >/dev/null 2>&1 &
sleep 1

echo "[entrypoint] Iniciando servidor x11vnc en puerto ${VNC_PORT}..."
x11vnc -display "${DISPLAY}" -forever -shared -nopw -rfbport "${VNC_PORT}" -quiet &
sleep 1

echo "[entrypoint] Iniciando noVNC (websockify) en puerto ${NOVNC_PORT}..."
websockify --web=/usr/share/novnc/ "${NOVNC_PORT}" "localhost:${VNC_PORT}" >/dev/null 2>&1 &
sleep 1

echo "[entrypoint] Escritorio remoto disponible en http://<IP_SERVIDOR>:${NOVNC_PORT}/vnc.html"
echo "[entrypoint] Iniciando bot de Discord..."
exec python bot.py
