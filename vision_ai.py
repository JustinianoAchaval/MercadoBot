"""
vision_ai.py - Extracción estructurada de precios y detección de CAPTCHAs desde capturas PNG
usando el SDK oficial `google-genai` (API v1beta) y validación estricta con Pydantic.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

logger = logging.getLogger("price_tracker.vision_ai")

DEFAULT_GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

PROMPT_EXTRACCION = """Eres un sistema experto en extracción de datos de comercio electrónico (Mercado Libre, Buscalibre, Amazon, tiendas retail, etc.) a partir de capturas de pantalla.

Analiza cuidadosamente la imagen adjunta y devuelve ÚNICAMENTE un objeto JSON válido que cumpla estrictamente con el esquema solicitado, siguiendo estas reglas:

1. `es_captcha` (bool):
   - Devuelve `true` si la captura muestra CUALQUIERA de estos bloqueos:
     * Un desafío CAPTCHA ("No soy un robot", reCAPTCHA, hCaptcha, Cloudflare Turnstile, rompecabezas deslizable).
     * Una pantalla de bloqueo de seguridad ("Verifica que eres humano", "Tráfico inusual", "Access Denied", "Error 403").
     * Una pantalla de inicio de sesión obligatorio que impide ver el producto ("¡Hola! Para continuar, ingresa a tu cuenta", "Inicia sesión para ver el precio").
   - Devuelve `false` si la página del producto es visible normalmente (aunque haya un banner pequeño de cookies).

2. `nombre_producto` (str):
   - El título principal del producto tal como aparece en la publicación.
   - Si `es_captcha` es `true` y no se ve el producto, devuelve `"Bloqueo / Captcha detectado"`.

3. `precio_actual` (float | null):
   - El precio de venta final vigente al público (el precio principal con descuento aplicado, si lo hubiera).
   - Ignora el valor de las cuotas mensuales (ej. NO confundas "en 6 cuotas de $5.000" con el precio total del producto).
   - Convierte el número a `float` limpio:
     * En tiendas hispanohablantes (Chile, Argentina, Colombia, España), el punto `.` suele ser separador de miles (ej. "$ 45.990" -> `45990.0`).
     * Si hay centavos explícitos separados por coma o superíndice, inclúyelos como decimales.
   - Si el producto no muestra precio (por estar agotado o bloqueado por CAPTCHA), devuelve `null`.

4. `precio_anterior` (float | null):
   - El precio original tachado (antes del descuento) si aparece explícitamente en la página.
   - Si no tiene precio tachado ni descuento, devuelve `null`.

5. `porcentaje_descuento` (int):
   - El porcentaje entero de descuento anunciado (ej. "25% OFF" -> `25`, "-15%" -> `15`).
   - Si no aparece escrito pero existen tanto `precio_anterior` como `precio_actual` (con `precio_anterior > precio_actual`), calcúlalo como `round((1 - precio_actual / precio_anterior) * 100)`.
   - Si no tiene descuento, devuelve `0`.

6. `en_stock` (bool):
   - `true` si el producto se puede comprar ("Comprar ahora", "Agregar al carrito", "Stock disponible", "Disponible").
   - `false` si dice "Publicación pausada", "Agotado", "Sin stock", "No disponible por el momento" o si `es_captcha` es `true`.
"""


class DatosProducto(BaseModel):
    """Esquema Pydantic estricto para la respuesta estructurada de Gemini."""

    nombre_producto: str = Field(
        description="Nombre o título principal del producto en la tienda."
    )
    precio_actual: Optional[float] = Field(
        default=None,
        description="Precio final de compra actual sin símbolos de moneda, o None si no está visible.",
    )
    precio_anterior: Optional[float] = Field(
        default=None,
        description="Precio anterior tachado antes del descuento, o None si no tiene descuento.",
    )
    porcentaje_descuento: int = Field(
        default=0,
        ge=0,
        le=100,
        description="Porcentaje entero de descuento (0 si no tiene descuento).",
    )
    en_stock: bool = Field(
        description="True si hay unidades disponibles para compra, False si está agotado o pausado."
    )
    es_captcha: bool = Field(
        description="True si aparece verificación 'No soy un robot', bloqueo de seguridad o pantalla de login forzado."
    )


class VisionAIClient:
    """Cliente asíncrono para analizar capturas de pantalla de productos con Gemini."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError(
                "No se encontró GEMINI_API_KEY en las variables de entorno."
            )
        self.model = model or os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
        # Inicializamos con api_version='v1beta' para habilitar soporte completo de
        # response_schema / JSON estructurado en todos los modelos de Gemini.
        self.client = genai.Client(
            api_key=self.api_key,
            http_options={"api_version": "v1beta"},
        )

    async def analizar_captura(self, image_path: str | Path) -> DatosProducto:
        """
        Recibe la ruta de una captura PNG, la envía a Gemini exigiendo el esquema
        Pydantic `DatosProducto` y devuelve la instancia validada.
        """
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"No existe la captura de pantalla: {path}")

        image_bytes = path.read_bytes()

        # Pasamos las instrucciones directamente dentro de `contents` junto con la imagen
        # para evitar errores de `Developer instruction is not enabled` entre versiones de API.
        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=[
                PROMPT_EXTRACCION,
                types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=DatosProducto,
                temperature=0.1,
            ),
        )

        if getattr(response, "parsed", None) is not None and isinstance(
            response.parsed, DatosProducto
        ):
            resultado = response.parsed
        elif response.text:
            resultado = DatosProducto.model_validate_json(response.text)
        else:
            raise RuntimeError("Gemini devolvió una respuesta vacía.")

        # Normalización defensiva del porcentaje de descuento
        if (
            resultado.porcentaje_descuento == 0
            and resultado.precio_anterior
            and resultado.precio_actual
            and resultado.precio_anterior > resultado.precio_actual > 0
        ):
            calculado = round(
                (1.0 - (resultado.precio_actual / resultado.precio_anterior)) * 100
            )
            resultado.porcentaje_descuento = max(0, min(99, calculado))

        logger.info(
            "Resultado Vision AI (%s): nombre='%s', precio=%s, desc=%d%%, stock=%s, captcha=%s",
            path.name,
            resultado.nombre_producto,
            resultado.precio_actual,
            resultado.porcentaje_descuento,
            resultado.en_stock,
            resultado.es_captcha,
        )
        return resultado


# Función de conveniencia de nivel de módulo para importar directamente
_default_client: Optional[VisionAIClient] = None


async def analizar_captura(image_path: str | Path) -> DatosProducto:
    """
    Función asíncrona de alto nivel que analiza una captura PNG y devuelve `DatosProducto`.
    """
    global _default_client
    if _default_client is None:
        _default_client = VisionAIClient()
    return await _default_client.analizar_captura(image_path)
