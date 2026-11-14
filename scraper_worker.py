"""
scraper_worker.py - Worker asíncrono con Playwright (contexto persistente no-headless).

Responsabilidades:
- Administrar una única instancia de Chromium con `launch_persistent_context` en `./chrome_profile`
  protegida por un `asyncio.Lock` para evitar colisiones entre el planificador y comandos manuales.
- Respetar los intervalos de revisión por tienda:
  * Mercado Libre: cada 3 horas
  * Buscalibre: cada 12 horas
  * Otras tiendas: cada 6 horas
- Ejecutar la máquina de estados Anti-Captcha:
  * Al detectar `es_captcha == True`, pausa la tienda en `estado_tiendas`, deja la pestaña abierta
    en el navegador X11/noVNC para resolución manual y envía alerta al canal `#admin`.
  * Mientras una tienda está pausada, prueba únicamente 1 producto por hora hasta que `es_captcha == False`,
    momento en el que reactiva la tienda automáticamente.
- Evaluar las 4 reglas de alerta de precios/stock y disparar notificaciones con captura adjunta.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from database import Database, utc_now_iso
from vision_ai import DatosProducto, VisionAIClient

logger = logging.getLogger("price_tracker.scraper")

# Intervalos de monitoreo por tienda
STORE_INTERVALS: dict[str, timedelta] = {
    "mercadolibre": timedelta(hours=3),
    "buscalibre": timedelta(hours=12),
    "otra": timedelta(hours=6),
}

CAPTCHA_RETRY_INTERVAL = timedelta(hours=1)


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    """Convierte una cadena ISO 8601 a objeto datetime con zona horaria UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


@dataclass
class ResultadoChequeo:
    """Resultado completo del escaneo de un producto."""

    producto_antes: dict[str, Any]
    producto_despues: dict[str, Any]
    datos_ia: DatosProducto
    screenshot_path: str
    es_primera_lectura: bool = False
    motivos_alerta: list[str] = field(default_factory=list)


def evaluar_condiciones_alerta(
    producto_previo: dict[str, Any],
    datos_nuevos: DatosProducto,
    umbral_bajada_pct: float = 10.0,
) -> list[str]:
    """
    Evalúa las 4 condiciones de alerta comparando el estado previo en base de datos
    con los datos recién extraídos por Vision AI.

    Retorna una lista de textos descriptivos por cada condición cumplida.
    """
    motivos: list[str] = []

    es_primera_vez = producto_previo.get("ultima_revision") is None
    precio_previo: Optional[float] = producto_previo.get("precio_actual")
    descuento_previo: int = int(producto_previo.get("descuento_actual") or 0)
    stock_previo: bool = bool(producto_previo.get("en_stock", True))
    precio_objetivo: Optional[float] = producto_previo.get("precio_objetivo")

    precio_nuevo = datos_nuevos.precio_actual
    descuento_nuevo = int(datos_nuevos.porcentaje_descuento or 0)
    stock_nuevo = bool(datos_nuevos.en_stock)

    # Condición A: Aparece un descuento nuevo o el porcentaje de descuento es mayor al anterior
    if not es_primera_vez and descuento_nuevo > 0 and descuento_nuevo > descuento_previo:
        if descuento_previo == 0:
            motivos.append(
                f"🏷️ **¡Nuevo descuento detectado!** Ahora tiene **{descuento_nuevo}% OFF**."
            )
        else:
            motivos.append(
                f"🔥 **¡Aumentó el descuento!** Subió de {descuento_previo}% a **{descuento_nuevo}% OFF**."
            )

    # Condición B: El precio baja de forma significativa (>= 10% respecto al precio_actual anterior)
    if (
        not es_primera_vez
        and precio_previo is not None
        and precio_previo > 0
        and precio_nuevo is not None
        and precio_nuevo < precio_previo
    ):
        pct_bajada = ((precio_previo - precio_nuevo) / precio_previo) * 100.0
        if pct_bajada >= umbral_bajada_pct:
            motivos.append(
                f"📉 **¡Bajada significativa de precio (-{pct_bajada:.1f}%)!** "
                f"Bajó de `${precio_previo:,.2f}` a **`${precio_nuevo:,.2f}`**."
            )

    # Condición C: precio_actual <= precio_objetivo (cuando el usuario definió objetivo)
    if (
        precio_objetivo is not None
        and precio_objetivo > 0
        and precio_nuevo is not None
        and precio_nuevo <= precio_objetivo
    ):
        motivos.append(
            f"🎯 **¡Precio objetivo alcanzado!** El precio actual (**`${precio_nuevo:,.2f}`**) "
            f"es igual o menor a tu objetivo (`${precio_objetivo:,.2f}`)."
        )

    # Condición D: Cambia el estado de en_stock (se agotó o volvió a haber stock)
    if not es_primera_vez and stock_nuevo != stock_previo:
        if stock_nuevo:
            motivos.append("✅ **¡Volvió a estar en STOCK!** El producto ya está disponible.")
        else:
            motivos.append("⚠️ **¡Producto AGOTADO!** El producto se quedó sin stock.")

    return motivos


# Tipos para callbacks hacia el bot de Discord
AlertCallback = Callable[[ResultadoChequeo], Awaitable[None]]
CaptchaAlertCallback = Callable[[str, dict[str, Any], str, str], Awaitable[None]]
StoreRestoredCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
ErrorCallback = Callable[[dict[str, Any], Exception, Optional[str]], Awaitable[None]]


class ScraperWorker:
    """
    Worker asíncrono que gestiona el navegador Chromium persistente y coordina
    las revisiones periódicas y manuales.
    """

    def __init__(
        self,
        db: Database,
        vision_client: Optional[VisionAIClient] = None,
        profile_dir: str = "./chrome_profile",
        screenshots_dir: str = "./screenshots",
        on_price_alert: Optional[AlertCallback] = None,
        on_captcha_detected: Optional[CaptchaAlertCallback] = None,
        on_store_restored: Optional[StoreRestoredCallback] = None,
        on_error: Optional[ErrorCallback] = None,
    ) -> None:
        self.db = db
        self.vision = vision_client or VisionAIClient()
        self.profile_dir = Path(profile_dir)
        self.screenshots_dir = Path(screenshots_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

        self.on_price_alert = on_price_alert
        self.on_captcha_detected = on_captcha_detected
        self.on_store_restored = on_store_restored
        self.on_error = on_error

        self.significant_drop_pct = float(
            os.getenv("SIGNIFICANT_DROP_PERCENT", "10.0")
        )
        self.server_ip = os.getenv("SERVER_IP", "localhost")
        self.novnc_port = os.getenv("NOVNC_PORT", "6080")

        # Lock global que serializa el uso del contexto persistente de Playwright
        # entre el scheduler periódico y los comandos manuales (/revisar, /agregar)
        self._scrape_lock = asyncio.Lock()
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._captcha_page: Optional[Page] = None

    @property
    def novnc_url(self) -> str:
        """Devuelve la URL de acceso web al escritorio noVNC."""
        return f"http://{self.server_ip}:{self.novnc_port}/vnc.html?autoconnect=true"

    def _clean_stale_profile_locks(self) -> None:
        """
        Elimina archivos de bloqueo huérfanos de Chromium si el contenedor
        fue reiniciado abruptamente.
        """
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_path = self.profile_dir / lock_name
            if lock_path.exists() or lock_path.is_symlink():
                try:
                    lock_path.unlink(missing_ok=True)
                    logger.info("Lock huérfano eliminado: %s", lock_path)
                except OSError as exc:
                    logger.warning("No se pudo eliminar %s: %s", lock_path, exc)

    async def start(self) -> None:
        """Inicia Playwright y lanza el contexto persistente de Chromium en modo visible (headless=False)."""
        async with self._scrape_lock:
            if self._context is not None:
                return

            self._clean_stale_profile_locks()
            self._playwright = await async_playwright().start()

            headless_env = os.getenv("PLAYWRIGHT_HEADLESS", "false").lower() == "true"
            logger.info(
                "Iniciando navegador Chromium persistente en %s (headless=%s)...",
                self.profile_dir,
                headless_env,
            )

            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir.resolve()),
                headless=headless_env,
                viewport={"width": 1366, "height": 768},
                locale="es-CL",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                ],
            )

            # Script anti-detección básica de navigator.webdriver
            await self._context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            logger.info("Contexto persistente de Chromium iniciado correctamente.")

    async def stop(self) -> None:
        """Cierra de forma limpia el contexto persistente y la instancia de Playwright."""
        async with self._scrape_lock:
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception as exc:
                    logger.warning("Error cerrando BrowserContext: %s", exc)
                self._context = None

            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception as exc:
                    logger.warning("Error deteniendo Playwright: %s", exc)
                self._playwright = None

    async def _ensure_browser(self) -> BrowserContext:
        """Garantiza que el navegador esté activo antes de abrir una página."""
        if self._context is None:
            self._clean_stale_profile_locks()
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            headless_env = os.getenv("PLAYWRIGHT_HEADLESS", "false").lower() == "true"
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir.resolve()),
                headless=headless_env,
                viewport={"width": 1366, "height": 768},
                locale="es-CL",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                ],
            )
            await self._context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
        return self._context

    async def _capturar_pagina(self, producto: dict[str, Any]) -> tuple[str, Page]:
        """
        Navega a la URL del producto, espera a que cargue la interfaz visual y
        guarda una captura de pantalla PNG. Devuelve `(ruta_png, page)`.
        """
        context = await self._ensure_browser()
        page = await context.new_page()

        url = producto["url"]
        product_id = producto["id"]
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        screenshot_path = self.screenshots_dir / f"prod_{product_id}_{timestamp_str}.png"

        try:
            logger.info("Navegando a producto ID=%s (%s): %s", product_id, producto["tienda"], url)
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # Espera breve para que hidraten precios dinámicos e imágenes principales
            await page.wait_for_timeout(3500)

            # Scroll suave hacia abajo y arriba para activar lazy-loading de precios/descuentos
            await page.evaluate("window.scrollBy(0, 350);")
            await page.wait_for_timeout(800)
            await page.evaluate("window.scrollTo(0, 0);")
            await page.wait_for_timeout(700)

            await page.screenshot(path=str(screenshot_path), full_page=False)
            return str(screenshot_path), page
        except Exception:
            await page.close()
            raise

    async def check_single_product(
        self,
        product_id: int,
        force_notify_user: bool = False,
        is_captcha_probe: bool = False,
    ) -> Optional[ResultadoChequeo]:
        """
        Revisa un único producto de manera segura usando `self._scrape_lock`.
        - Si detecta CAPTCHA, activa la pausa de la tienda, deja la pestaña abierta en noVNC
          y alerta al canal `#admin`.
        - Si la lectura es válida, guarda el historial, actualiza el producto y evalúa
          las 4 condiciones de alerta.
        """
        async with self._scrape_lock:
            producto_antes = await self.db.get_product_by_id(product_id)
            if not producto_antes:
                logger.warning("Producto ID=%s no encontrado en base de datos.", product_id)
                return None

            tienda = producto_antes["tienda"]
            estado_tienda = await self.db.get_store_state(tienda)
            estaba_pausada = bool(estado_tienda["pausada_captcha"])

            page: Optional[Page] = None
            screenshot_path: Optional[str] = None
            try:
                screenshot_path, page = await self._capturar_pagina(producto_antes)
                datos_ia = await self.vision.analizar_captura(screenshot_path)

                now_iso = utc_now_iso()

                # =============================================================
                # Caso 1: Vision AI detecta CAPTCHA o pantalla de bloqueo/login
                # =============================================================
                if datos_ia.es_captcha:
                    logger.warning(
                        "CAPTCHA detectado en tienda '%s' al revisar producto ID=%s.",
                        tienda,
                        product_id,
                    )
                    await self.db.set_store_captcha_state(
                        tienda=tienda,
                        pausada_captcha=True,
                        ultima_prueba_iso=now_iso,
                    )

                    # Cerrar pestaña de captcha previa si había una para no acumularlas,
                    # y dejar abierta la pestaña actual para que el admin la resuelva en noVNC
                    if self._captcha_page and not self._captcha_page.is_closed():
                        try:
                            await self._captcha_page.close()
                        except Exception:
                            pass
                    self._captcha_page = page
                    page = None  # No cerrarla en el bloque finally

                    # Notificar al admin si la tienda recién entra en pausa o si fue una revisión manual
                    if (not estaba_pausada or not is_captcha_probe) and self.on_captcha_detected:
                        await self.on_captcha_detected(
                            tienda,
                            producto_antes,
                            screenshot_path,
                            self.novnc_url,
                        )

                    producto_despues = await self.db.get_product_by_id(product_id) or producto_antes
                    return ResultadoChequeo(
                        producto_antes=producto_antes,
                        producto_despues=producto_despues,
                        datos_ia=datos_ia,
                        screenshot_path=screenshot_path,
                        es_primera_lectura=(producto_antes.get("ultima_revision") is None),
                        motivos_alerta=[],
                    )

                # =============================================================
                # Caso 2: Lectura válida (es_captcha == False)
                # =============================================================
                if estaba_pausada:
                    logger.info(
                        "Tienda '%s' superó el bloqueo de CAPTCHA. Reactivando monitoreo normal.",
                        tienda,
                    )
                    await self.db.set_store_captcha_state(
                        tienda=tienda,
                        pausada_captcha=False,
                        ultima_prueba_iso=now_iso,
                    )
                    if self._captcha_page and not self._captcha_page.is_closed():
                        try:
                            await self._captcha_page.close()
                        except Exception:
                            pass
                        self._captcha_page = None

                    if self.on_store_restored:
                        await self.on_store_restored(tienda, producto_antes)

                es_primera_lectura = producto_antes.get("ultima_revision") is None
                motivos_alerta = evaluar_condiciones_alerta(
                    producto_previo=producto_antes,
                    datos_nuevos=datos_ia,
                    umbral_bajada_pct=self.significant_drop_pct,
                )

                # Actualizar producto e insertar registro en historial_precios
                await self.db.update_product_scrape(
                    product_id=product_id,
                    nombre=datos_ia.nombre_producto,
                    precio_actual=datos_ia.precio_actual,
                    precio_anterior=datos_ia.precio_anterior,
                    descuento_actual=datos_ia.porcentaje_descuento,
                    en_stock=datos_ia.en_stock,
                    fecha_iso=now_iso,
                )

                producto_despues = await self.db.get_product_by_id(product_id)
                assert producto_despues is not None

                resultado = ResultadoChequeo(
                    producto_antes=producto_antes,
                    producto_despues=producto_despues,
                    datos_ia=datos_ia,
                    screenshot_path=screenshot_path,
                    es_primera_lectura=es_primera_lectura,
                    motivos_alerta=motivos_alerta,
                )

                # Si se cumple alguna de las 4 condiciones de alerta (y no se notificará
                # manualmente por respuesta directa del comando), disparamos el callback
                if motivos_alerta and not force_notify_user and self.on_price_alert:
                    await self.on_price_alert(resultado)

                return resultado

            except Exception as exc:
                logger.exception(
                    "Error al revisar el producto ID=%s (%s): %s",
                    product_id,
                    producto_antes.get("url"),
                    exc,
                )
                if self.on_error is not None:
                    try:
                        await self.on_error(producto_antes, exc, screenshot_path)
                    except Exception as notify_exc:
                        logger.error(
                            "No se pudo notificar el error en Discord para producto ID=%s: %s",
                            product_id,
                            notify_exc,
                        )
                return None
            finally:
                if page is not None and not page.is_closed():
                    try:
                        await page.close()
                    except Exception:
                        pass

    async def run_scheduled_checks(self) -> None:
        """
        Recorre los productos activos agrupados por tienda y ejecuta las revisiones
        correspondientes según:
        - Intervalo por tienda (Mercado Libre 3h, Buscalibre 12h, otra 6h).
        - Estado Anti-Captcha (si una tienda está pausada, prueba máximo 1 producto cada 1 hora).
        """
        productos = await self.db.get_all_active_products()
        if not productos:
            return

        por_tienda: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for prod in productos:
            por_tienda[prod["tienda"]].append(prod)

        now = datetime.now(timezone.utc)

        for tienda, lista_productos in por_tienda.items():
            estado_tienda = await self.db.get_store_state(tienda)
            pausada = bool(estado_tienda["pausada_captcha"])
            ultima_prueba = parse_iso_datetime(estado_tienda.get("ultima_prueba"))

            # -----------------------------------------------------------------
            # Modo Anti-Captcha: probar solo 1 producto por hora
            # -----------------------------------------------------------------
            if pausada:
                if ultima_prueba and (now - ultima_prueba) < CAPTCHA_RETRY_INTERVAL:
                    restante = CAPTCHA_RETRY_INTERVAL - (now - ultima_prueba)
                    minutos = int(restante.total_seconds() // 60)
                    logger.info(
                        "Tienda '%s' pausada por CAPTCHA. Próxima prueba de desbloqueo en %d min.",
                        tienda,
                        minutos,
                    )
                    continue

                producto_prueba = lista_productos[0]
                logger.info(
                    "Ejecutando prueba horaria anti-captcha para tienda '%s' con producto ID=%s...",
                    tienda,
                    producto_prueba["id"],
                )
                resultado_prueba = await self.check_single_product(
                    product_id=producto_prueba["id"],
                    force_notify_user=False,
                    is_captcha_probe=True,
                )

                # Si sigue con CAPTCHA o falló, no revisamos más productos de esta tienda
                if resultado_prueba is None or resultado_prueba.datos_ia.es_captcha:
                    continue

                # Si se desbloqueó (es_captcha == False), continuamos con el resto de productos vencidos
                lista_productos = lista_productos[1:]

            # -----------------------------------------------------------------
            # Modo Normal: revisar productos cuyo intervalo de tienda venció
            # -----------------------------------------------------------------
            intervalo = STORE_INTERVALS.get(tienda, STORE_INTERVALS["otra"])

            for prod in lista_productos:
                ultima_rev = parse_iso_datetime(prod.get("ultima_revision"))
                vencido = (ultima_rev is None) or ((now - ultima_rev) >= intervalo)
                if not vencido:
                    continue

                resultado = await self.check_single_product(
                    product_id=prod["id"],
                    force_notify_user=False,
                    is_captcha_probe=False,
                )

                # Si durante el ciclo normal aparece un CAPTCHA, detenemos de inmediato
                # el resto de productos de esta tienda
                if resultado is not None and resultado.datos_ia.es_captcha:
                    logger.warning(
                        "Pausando cola restante de la tienda '%s' debido a CAPTCHA.",
                        tienda,
                    )
                    break

                # Pausa breve entre productos para simular navegación humana
                await asyncio.sleep(3)
