# 🤖 Discord Price Tracker 24/7 (Playwright + Gemini Vision AI + noVNC)

Bot de Discord en Python 3.11+ para monitoreo de precios 24/7 pensado para desplegarse con Docker en un homelab. Utiliza **Playwright** con sesión persistente (`headless=False`) sobre un servidor gráfico virtual (`Xvfb` + `x11vnc` + `noVNC`) y **Google Gemini Vision AI** (`google-genai` + `Pydantic`) para extraer precios, descuentos, disponibilidad y detectar bloqueos CAPTCHA a partir de capturas de pantalla.

---

## ✨ Características Principales

- **Control de Acceso y Canales Privados**:
  - Los usuarios solicitan acceso mediante `/solicitar_acceso` o escribiendo al bot.
  - El canal `#admin` recibe una tarjeta interactiva con botones `[✅ Aprobar]` y `[✖️ Rechazar]`.
  - Al aprobar, el bot crea automáticamente un canal privado `#precios-<usuario>` visible solo para ese usuario y el bot.
- **Ingesta Inteligente de Enlaces**:
  - Al pegar cualquier URL en su canal privado (o usar `/agregar <url>`), el bot registra el producto y presenta botones interactivos (`[🎯 Definir Precio Objetivo]` con Modal o `[📉 Dejar abierto a cualquier bajada]`).
- **Intervalos por Tienda y Sesión Persistente**:
  - **Mercado Libre**: cada 3 horas.
  - **Amazon**: cada 12 horas.
  - **Otras tiendas**: cada 6 horas.
  - Perfil persistente en `./chrome_profile` protegido por `asyncio.Lock` para mantener cookies y sesiones iniciadas sin conflictos de concurrencia.
- **Sistema Anti-Captcha con noVNC**:
  - Si Gemini detecta un CAPTCHA o pantalla de login obligatorio (`es_captcha == True`), la tienda se marca en pausa (`pausada_captcha = True`), la pestaña se mantiene abierta en Chromium y se envía una alerta a `#admin` con el enlace directo `http://<SERVER_IP>:6080` para resolverlo desde el navegador.
  - Mientras esté en pausa, prueba **1 solo producto por hora** de esa tienda hasta que `es_captcha == False`, reactivando el monitoreo automáticamente.
- **4 Condiciones de Alerta con Captura Adjunta**:
  1. Aparece un descuento nuevo o el porcentaje de descuento supera al anterior.
  2. El precio cae significativamente (`>= 10%` respecto al precio anterior).
  3. El precio actual alcanza o baja del `precio_objetivo` definido por el usuario.
  4. Cambia el estado de `en_stock` (se agotó o volvió a estar disponible).

---

## 🚀 Despliegue Rápido en Homelab (Docker Compose)

1. **Configurar variables de entorno**:
   ```bash
   cp .env.example .env
   ```
   Edita `.env` con tu `DISCORD_TOKEN`, `ADMIN_CHANNEL_ID`, `GEMINI_API_KEY` y la IP local de tu servidor (`SERVER_IP`).

2. **Permisos del Bot en Discord Developer Portal**:
   - Activa **Server Members Intent** y **Message Content Intent** en la sección *Bot*.
   - Invita el bot a tu servidor con permisos de: `Manage Channels`, `View Channels`, `Send Messages`, `Embed Links`, `Attach Files`, `Read Message History` y `Use Slash Commands`.

3. **Construir y levantar el contenedor**:
   ```bash
   docker compose up -d --build
   ```

4. **Acceder al navegador vía noVNC** (para iniciar sesión en tiendas o resolver CAPTCHAs):
   - Abre `http://<SERVER_IP>:6080` en tu navegador web.
