"""
database.py - Gestión de base de datos SQLite asíncrona con aiosqlite.

Administra las tablas:
- usuarios: Control de acceso y canal privado asignado por usuario.
- productos: Productos monitoreados por cada usuario, precios y estado actual.
- historial_precios: Registro histórico de cada lectura válida realizada.
- estado_tiendas: Control de pausas por detección de CAPTCHA por tienda.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import aiosqlite

logger = logging.getLogger("price_tracker.database")

DEFAULT_DB_PATH = os.getenv("DB_PATH", "./data/tracker.db")


def utc_now_iso() -> str:
    """Devuelve la fecha/hora actual en UTC en formato ISO 8601."""
    return datetime.now(timezone.utc).isoformat()


def detectar_tienda(url: str) -> str:
    """
    Identifica la tienda a partir del dominio de la URL.
    Retorna 'mercadolibre', 'buscalibre' u 'otra'.
    """
    try:
        parsed = urlparse(url.strip())
        host = (parsed.netloc or "").lower()
    except Exception:
        return "otra"

    if any(
        domain in host
        for domain in (
            "mercadolibre.",
            "mercadolivre.",
            "meli.la",
            "articulo.mercadolibre",
            "producto.mercadolibre",
        )
    ):
        return "mercadolibre"
    if "buscalibre." in host:
        return "buscalibre"
    return "otra"


class Database:
    """Gestor asíncrono de SQLite para el bot de monitoreo de precios."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> aiosqlite.Connection:
        """Crea una conexión aiosqlite hacia el archivo de base de datos."""
        return aiosqlite.connect(self.db_path)

    async def init_db(self) -> None:
        """
        Inicializa el esquema de la base de datos y configura el modo WAL
        para permitir lecturas y escrituras concurrentes sin bloqueos.
        """
        async with self._connect() as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA synchronous=NORMAL;")
            await db.execute("PRAGMA foreign_keys=ON;")

            # 1. Tabla de usuarios
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS usuarios (
                    discord_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL,
                    channel_id INTEGER,
                    autorizado BOOLEAN NOT NULL DEFAULT 0
                );
                """
            )

            # 2. Tabla de productos monitoreados
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS productos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    tienda TEXT NOT NULL,
                    nombre TEXT,
                    precio_actual REAL,
                    precio_anterior REAL,
                    precio_objetivo REAL,
                    descuento_actual INTEGER NOT NULL DEFAULT 0,
                    en_stock BOOLEAN NOT NULL DEFAULT 1,
                    ultima_revision DATETIME,
                    FOREIGN KEY (user_id) REFERENCES usuarios (discord_id) ON DELETE CASCADE
                );
                """
            )

            # 3. Tabla de historial de precios
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS historial_precios (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    producto_id INTEGER NOT NULL,
                    precio REAL,
                    en_stock BOOLEAN NOT NULL,
                    fecha DATETIME NOT NULL,
                    FOREIGN KEY (producto_id) REFERENCES productos (id) ON DELETE CASCADE
                );
                """
            )

            # 4. Tabla de estado de tiendas (control anti-captcha)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS estado_tiendas (
                    tienda TEXT PRIMARY KEY,
                    pausada_captcha BOOLEAN NOT NULL DEFAULT 0,
                    ultima_prueba DATETIME
                );
                """
            )

            # Índices para optimizar consultas periódicas
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_productos_user_id ON productos (user_id);"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_productos_tienda ON productos (tienda);"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_historial_producto_id ON historial_precios (producto_id);"
            )

            # Pre-inicializar registros de tiendas conocidas
            for tienda_base in ("mercadolibre", "buscalibre", "otra"):
                await db.execute(
                    """
                    INSERT OR IGNORE INTO estado_tiendas (tienda, pausada_captcha, ultima_prueba)
                    VALUES (?, 0, NULL);
                    """,
                    (tienda_base,),
                )

            await db.commit()
            logger.info("Base de datos inicializada correctamente en %s", self.db_path)

    # =========================================================================
    # Gestión de Usuarios
    # =========================================================================

    async def get_user(self, discord_id: int) -> Optional[dict[str, Any]]:
        """Obtiene los datos de un usuario por su ID de Discord."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM usuarios WHERE discord_id = ?;", (discord_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                data = dict(row)
                data["autorizado"] = bool(data["autorizado"])
                return data

    async def upsert_user(
        self,
        discord_id: int,
        username: str,
        channel_id: Optional[int] = None,
        autorizado: Optional[bool] = None,
    ) -> dict[str, Any]:
        """
        Crea o actualiza un usuario en la tabla `usuarios`.
        Si `channel_id` o `autorizado` son None, conserva los valores existentes.
        """
        existing = await self.get_user(discord_id)
        async with self._connect() as db:
            if existing is None:
                await db.execute(
                    """
                    INSERT INTO usuarios (discord_id, username, channel_id, autorizado)
                    VALUES (?, ?, ?, ?);
                    """,
                    (
                        discord_id,
                        username,
                        channel_id,
                        1 if autorizado else 0,
                    ),
                )
            else:
                new_channel_id = (
                    channel_id if channel_id is not None else existing["channel_id"]
                )
                new_autorizado = (
                    (1 if autorizado else 0)
                    if autorizado is not None
                    else (1 if existing["autorizado"] else 0)
                )
                await db.execute(
                    """
                    UPDATE usuarios
                    SET username = ?, channel_id = ?, autorizado = ?
                    WHERE discord_id = ?;
                    """,
                    (username, new_channel_id, new_autorizado, discord_id),
                )
            await db.commit()

        user = await self.get_user(discord_id)
        assert user is not None
        return user

    async def authorize_user(
        self, discord_id: int, username: str, channel_id: int
    ) -> dict[str, Any]:
        """Autoriza a un usuario y le asocia su canal privado de monitoreo."""
        return await self.upsert_user(
            discord_id=discord_id,
            username=username,
            channel_id=channel_id,
            autorizado=True,
        )

    async def revoke_user(self, discord_id: int, username: str) -> dict[str, Any]:
        """Marca a un usuario como no autorizado."""
        return await self.upsert_user(
            discord_id=discord_id,
            username=username,
            autorizado=False,
        )

    # =========================================================================
    # Gestión de Productos
    # =========================================================================

    async def add_product(
        self,
        user_id: int,
        url: str,
        precio_objetivo: Optional[float] = None,
        nombre: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Inserta un nuevo producto para monitorear y devuelve el registro creado.
        """
        url_clean = url.strip()
        tienda = detectar_tienda(url_clean)

        async with self._connect() as db:
            # Asegurar que la tienda exista en estado_tiendas
            await db.execute(
                """
                INSERT OR IGNORE INTO estado_tiendas (tienda, pausada_captcha, ultima_prueba)
                VALUES (?, 0, NULL);
                """,
                (tienda,),
            )

            cursor = await db.execute(
                """
                INSERT INTO productos (
                    user_id, url, tienda, nombre,
                    precio_actual, precio_anterior, precio_objetivo,
                    descuento_actual, en_stock, ultima_revision
                )
                VALUES (?, ?, ?, ?, NULL, NULL, ?, 0, 1, NULL);
                """,
                (user_id, url_clean, tienda, nombre, precio_objetivo),
            )
            product_id = cursor.lastrowid
            await db.commit()

        assert product_id is not None
        product = await self.get_product_by_id(product_id)
        assert product is not None
        return product

    async def get_product_by_id(self, product_id: int) -> Optional[dict[str, Any]]:
        """Obtiene un producto por su ID junto con el channel_id de su dueño."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT p.*, u.channel_id, u.username, u.autorizado
                FROM productos p
                JOIN usuarios u ON u.discord_id = p.user_id
                WHERE p.id = ?;
                """,
                (product_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                data = dict(row)
                data["en_stock"] = bool(data["en_stock"])
                data["autorizado"] = bool(data["autorizado"])
                return data

    async def get_user_products(self, user_id: int) -> list[dict[str, Any]]:
        """Devuelve todos los productos registrados por un usuario específico."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT p.*, u.channel_id, u.username, u.autorizado
                FROM productos p
                JOIN usuarios u ON u.discord_id = p.user_id
                WHERE p.user_id = ?
                ORDER BY p.id ASC;
                """,
                (user_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                result: list[dict[str, Any]] = []
                for row in rows:
                    item = dict(row)
                    item["en_stock"] = bool(item["en_stock"])
                    item["autorizado"] = bool(item["autorizado"])
                    result.append(item)
                return result

    async def get_all_active_products(self) -> list[dict[str, Any]]:
        """
        Devuelve todos los productos pertenecientes a usuarios autorizados,
        incluyendo el channel_id del usuario para el envío de alertas.
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT p.*, u.channel_id, u.username, u.autorizado
                FROM productos p
                JOIN usuarios u ON u.discord_id = p.user_id
                WHERE u.autorizado = 1
                ORDER BY p.id ASC;
                """
            ) as cursor:
                rows = await cursor.fetchall()
                result: list[dict[str, Any]] = []
                for row in rows:
                    item = dict(row)
                    item["en_stock"] = bool(item["en_stock"])
                    item["autorizado"] = bool(item["autorizado"])
                    result.append(item)
                return result

    async def update_target_price(
        self, product_id: int, user_id: int, precio_objetivo: Optional[float]
    ) -> bool:
        """
        Actualiza el precio objetivo de un producto verificando que pertenezca al usuario.
        Retorna True si se modificó un registro, False si no existe o no pertenece al usuario.
        """
        async with self._connect() as db:
            cursor = await db.execute(
                """
                UPDATE productos
                SET precio_objetivo = ?
                WHERE id = ? AND user_id = ?;
                """,
                (precio_objetivo, product_id, user_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def delete_product(self, product_id: int, user_id: int) -> bool:
        """
        Elimina un producto de la lista del usuario (y su historial en cascada).
        Retorna True si fue eliminado, False si no existía o no pertenecía al usuario.
        """
        async with self._connect() as db:
            await db.execute("PRAGMA foreign_keys=ON;")
            cursor = await db.execute(
                "DELETE FROM productos WHERE id = ? AND user_id = ?;",
                (product_id, user_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def update_product_scrape(
        self,
        product_id: int,
        nombre: str,
        precio_actual: Optional[float],
        precio_anterior: Optional[float],
        descuento_actual: int,
        en_stock: bool,
        fecha_iso: Optional[str] = None,
    ) -> None:
        """
        Actualiza los datos del producto tras una lectura válida (sin CAPTCHA)
        y registra la entrada correspondiente en `historial_precios`.
        """
        timestamp = fecha_iso or utc_now_iso()
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE productos
                SET nombre = COALESCE(NULLIF(?, ''), nombre),
                    precio_actual = ?,
                    precio_anterior = ?,
                    descuento_actual = ?,
                    en_stock = ?,
                    ultima_revision = ?
                WHERE id = ?;
                """,
                (
                    nombre.strip() if nombre else "",
                    precio_actual,
                    precio_anterior,
                    max(0, int(descuento_actual)),
                    1 if en_stock else 0,
                    timestamp,
                    product_id,
                ),
            )

            await db.execute(
                """
                INSERT INTO historial_precios (producto_id, precio, en_stock, fecha)
                VALUES (?, ?, ?, ?);
                """,
                (product_id, precio_actual, 1 if en_stock else 0, timestamp),
            )
            await db.commit()

    async def get_price_history(
        self, product_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Obtiene las últimas lecturas del historial de precios de un producto."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM historial_precios
                WHERE producto_id = ?
                ORDER BY fecha DESC
                LIMIT ?;
                """,
                (product_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
                result: list[dict[str, Any]] = []
                for row in rows:
                    item = dict(row)
                    item["en_stock"] = bool(item["en_stock"])
                    result.append(item)
                return result

    # =========================================================================
    # Gestión de Estado de Tiendas (Anti-Captcha)
    # =========================================================================

    async def get_store_state(self, tienda: str) -> dict[str, Any]:
        """
        Obtiene el estado de bloqueo por CAPTCHA de una tienda.
        Si no existe aún en la tabla, la inserta con valores por defecto.
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM estado_tiendas WHERE tienda = ?;", (tienda,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    data = dict(row)
                    data["pausada_captcha"] = bool(data["pausada_captcha"])
                    return data

            await db.execute(
                """
                INSERT OR IGNORE INTO estado_tiendas (tienda, pausada_captcha, ultima_prueba)
                VALUES (?, 0, NULL);
                """,
                (tienda,),
            )
            await db.commit()
            return {
                "tienda": tienda,
                "pausada_captcha": False,
                "ultima_prueba": None,
            }

    async def set_store_captcha_state(
        self,
        tienda: str,
        pausada_captcha: bool,
        ultima_prueba_iso: Optional[str] = None,
    ) -> None:
        """
        Actualiza el estado `pausada_captcha` y la marca de tiempo `ultima_prueba`
        para la tienda indicada.
        """
        timestamp = ultima_prueba_iso or utc_now_iso()
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO estado_tiendas (tienda, pausada_captcha, ultima_prueba)
                VALUES (?, ?, ?)
                ON CONFLICT(tienda) DO UPDATE SET
                    pausada_captcha = excluded.pausada_captcha,
                    ultima_prueba = excluded.ultima_prueba;
                """,
                (tienda, 1 if pausada_captcha else 0, timestamp),
            )
            await db.commit()
