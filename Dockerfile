FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DISPLAY=:99 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Instalar Xvfb, gestor de ventanas Fluxbox, x11vnc, noVNC y utilidades base
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    fluxbox \
    x11vnc \
    novnc \
    websockify \
    ca-certificates \
    curl \
    fonts-liberation \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Crear redirección automática de / hacia /vnc.html en noVNC para comodidad del usuario
RUN if [ -f /usr/share/novnc/vnc.html ] && [ ! -f /usr/share/novnc/index.html ]; then \
        ln -s /usr/share/novnc/vnc.html /usr/share/novnc/index.html; \
    fi

# Instalar dependencias de Python y Chromium de Playwright con sus librerías del sistema
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt && \
    playwright install --with-deps chromium

# Copiar el código de la aplicación
COPY database.py vision_ai.py scraper_worker.py bot.py entrypoint.sh /app/

# Normalizar saltos de línea (por si se clona/edita en Windows) y dar permisos de ejecución
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

EXPOSE 6080

ENTRYPOINT ["/app/entrypoint.sh"]
