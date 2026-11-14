"""
bot.py - Cliente de Discord, control de acceso por canal privado, comandos Slash,
vistas interactivas y sistema de notificaciones de precios + anti-captcha.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from discord import app_commands
from dotenv import load_dotenv

from database import Database
from scraper_worker import ResultadoChequeo, ScraperWorker
from vision_ai import VisionAIClient

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("price_tracker.bot")

URL_REGEX = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def format_price(value: Optional[float]) -> str:
    """Formatea un valor numérico como moneda legible."""
    if value is None:
        return "Pendiente / No disponible"
    if value.is_integer():
        return f"${int(value):,}".replace(",", ".")
    return f"${value:,.2f}"


def sanitize_channel_suffix(username: str) -> str:
    """Normaliza un nombre de usuario de Discord para crear el canal `#precios-<usuario>`."""
    cleaned = re.sub(r"[^a-z0-9_-]", "", username.lower().replace(" ", "-"))
    return cleaned[:20] or "usuario"


# =============================================================================
# Vistas Interactivas y Modals (discord.ui)
# =============================================================================


class SolicitudAccesoView(discord.ui.View):
    """
    Vista enviada al canal #admin con botones [Aprobar] y [Rechazar]
    para gestionar el alta de nuevos usuarios y crear su canal privado.
    """

    def __init__(
        self,
        bot: "PriceTrackerBot",
        solicitante_id: int,
        solicitante_nombre: str,
    ) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.solicitante_id = solicitante_id
        self.solicitante_nombre = solicitante_nombre

    @discord.ui.button(
        label="Aprobar",
        style=discord.ButtonStyle.success,
        emoji="✅",
    )
    async def aprobar_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(thinking=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "❌ Esta acción debe ejecutarse dentro de un servidor.", ephemeral=True
            )
            return

        # Buscar al miembro en el servidor
        member = guild.get_member(self.solicitante_id)
        if member is None:
            try:
                member = await guild.fetch_member(self.solicitante_id)
            except discord.DiscordException:
                member = None

        # Configurar permisos exclusivos para el canal privado #precios-<usuario>
        overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                manage_channels=True,
            ),
        }
        if member is not None:
            overwrites[member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            )

        channel_name = f"precios-{sanitize_channel_suffix(self.solicitante_nombre)}"

        # Verificar si el usuario ya tenía un canal creado previamente
        existing_user = await self.bot.db.get_user(self.solicitante_id)
        private_channel: Optional[discord.TextChannel] = None
        if existing_user and existing_user.get("channel_id"):
            ch = guild.get_channel(int(existing_user["channel_id"]))
            if isinstance(ch, discord.TextChannel):
                private_channel = ch

        if private_channel is None:
            category = (
                interaction.channel.category
                if isinstance(interaction.channel, discord.TextChannel)
                else None
            )
            private_channel = await guild.create_text_channel(
                name=channel_name,
                overwrites=overwrites,
                category=category,
                topic=f"Canal privado de monitoreo de precios 24/7 para {self.solicitante_nombre} (ID: {self.solicitante_id})",
                reason=f"Aprobación de acceso al bot de precios por {interaction.user}",
            )

        await self.bot.db.authorize_user(
            discord_id=self.solicitante_id,
            username=self.solicitante_nombre,
            channel_id=private_channel.id,
        )
        self.bot.pending_requests.discard(self.solicitante_id)

        # Deshabilitar botones tras aprobar
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

        embed_admin = discord.Embed(
            title="✅ Usuario Autorizado",
            description=(
                f"El usuario <@{self.solicitante_id}> (`{self.solicitante_nombre}`) ha sido **aprobado** "
                f"por {interaction.user.mention}.\n"
                f"Canal privado asignado: {private_channel.mention}"
            ),
            color=discord.Color.green(),
        )
        if interaction.message:
            await interaction.message.edit(embed=embed_admin, view=self)

        # Enviar bienvenida al canal privado recién creado
        embed_bienvenida = discord.Embed(
            title="👋 ¡Bienvenido a tu Monitor de Precios 24/7!",
            description=(
                f"Hola <@{self.solicitante_id}>, tu acceso fue aprobado y este es tu **canal privado exclusivo**.\n\n"
                "### ¿Cómo funciona?\n"
                "• **Pegar un enlace**: Simplemente pega cualquier URL de producto aquí (Mercado Libre, Buscalibre u otra tienda) o usa `/agregar <url>`.\n"
                "• **`/lista`**: Consulta todos tus productos monitoreados, precios actuales, descuentos y stock.\n"
                "• **`/objetivo <id> <precio>`**: Define o modifica el precio objetivo de un producto.\n"
                "• **`/revisar <id>`**: Fuerza una revisión inmediata (usa `0` para revisar todos tus productos).\n"
                "• **`/quitar <id>`**: Elimina un producto de tu seguimiento."
            ),
            color=discord.Color.blurple(),
        )
        await private_channel.send(
            content=f"<@{self.solicitante_id}>", embed=embed_bienvenida
        )
        await interaction.followup.send(
            f"Canal {private_channel.mention} creado y usuario autorizado.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Rechazar",
        style=discord.ButtonStyle.danger,
        emoji="✖️",
    )
    async def rechazar_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(thinking=True)

        await self.bot.db.revoke_user(
            discord_id=self.solicitante_id,
            username=self.solicitante_nombre,
        )
        self.bot.pending_requests.discard(self.solicitante_id)

        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

        embed_admin = discord.Embed(
            title="❌ Solicitud Rechazada",
            description=(
                f"La solicitud de <@{self.solicitante_id}> (`{self.solicitante_nombre}`) "
                f"fue **rechazada** por {interaction.user.mention}."
            ),
            color=discord.Color.red(),
        )
        if interaction.message:
            await interaction.message.edit(embed=embed_admin, view=self)

        await interaction.followup.send(
            f"Solicitud de `{self.solicitante_nombre}` rechazada.",
            ephemeral=True,
        )


class PrecioObjetivoModal(discord.ui.Modal, title="Definir Precio Objetivo"):
    """Modal interactivo para que el usuario ingrese el precio objetivo de un producto."""

    precio_input = discord.ui.TextInput(
        label="Precio Objetivo (solo números)",
        placeholder="Ej: 29990",
        required=True,
        max_length=20,
    )

    def __init__(
        self,
        bot: "PriceTrackerBot",
        product_id: int,
        user_id: int,
        parent_view: "ConfigurarProductoView",
    ) -> None:
        super().__init__()
        self.bot = bot
        self.product_id = product_id
        self.user_id = user_id
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_val = (
            self.precio_input.value.strip()
            .replace("$", "")
            .replace(" ", "")
            .replace(".", "")
            .replace(",", ".")
        )
        try:
            precio_obj = float(raw_val)
            if precio_obj <= 0:
                raise ValueError("El precio debe ser mayor a 0.")
        except ValueError:
            await interaction.response.send_message(
                "❌ Por favor ingresa un monto numérico válido mayor a 0 (ejemplo: `29990`).",
                ephemeral=True,
            )
            return

        await self.bot.db.update_target_price(
            product_id=self.product_id,
            user_id=self.user_id,
            precio_objetivo=precio_obj,
        )

        self.parent_view.disable_all()
        if interaction.message:
            try:
                await interaction.message.edit(view=self.parent_view)
            except discord.DiscordException:
                pass

        await interaction.response.send_message(
            f"🎯 **Precio objetivo guardado** para el producto **#{self.product_id}**: "
            f"**{format_price(precio_obj)}**.\n"
            "🔍 *Realizando primera captura e inspección con IA, te avisaré aquí en unos segundos...*"
        )

        # Ejecutar el primer chequeo en segundo plano
        asyncio.create_task(
            self.bot.ejecutar_chequeo_inicial(self.product_id, interaction.channel_id)
        )


class ConfigurarProductoView(discord.ui.View):
    """
    Botones interactivos mostrados tras agregar un link para elegir si definir
    un precio objetivo o dejarlo abierto a cualquier bajada/descuento.
    """

    def __init__(
        self, bot: "PriceTrackerBot", product_id: int, user_id: int
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.product_id = product_id
        self.user_id = user_id

    def disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ Solo el dueño de este producto puede configurarlo.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="Definir Precio Objetivo",
        style=discord.ButtonStyle.primary,
        emoji="🎯",
    )
    async def definir_objetivo_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        modal = PrecioObjetivoModal(
            bot=self.bot,
            product_id=self.product_id,
            user_id=self.user_id,
            parent_view=self,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Dejar abierto a cualquier bajada",
        style=discord.ButtonStyle.secondary,
        emoji="📉",
    )
    async def cualquier_bajada_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.bot.db.update_target_price(
            product_id=self.product_id,
            user_id=self.user_id,
            precio_objetivo=None,
        )
        self.disable_all()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"📉 Producto **#{self.product_id}** configurado en **modo abierto** (se te alertará ante cualquier descuento nuevo, bajada >= 10% o cambio de stock).\n"
            "🔍 *Realizando primera captura e inspección con IA, te avisaré aquí en unos segundos...*"
        )
        asyncio.create_task(
            self.bot.ejecutar_chequeo_inicial(self.product_id, interaction.channel_id)
        )


# =============================================================================
# Cliente Principal del Bot
# =============================================================================


class PriceTrackerBot(discord.Client):
    """Cliente principal de Discord para el sistema de monitoreo de precios 24/7."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True

        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

        self.admin_channel_id = int(os.getenv("ADMIN_CHANNEL_ID", "0"))
        self.db = Database()
        self.vision = VisionAIClient()
        self.worker = ScraperWorker(
            db=self.db,
            vision_client=self.vision,
            on_price_alert=self.enviar_alerta_precio,
            on_captcha_detected=self.enviar_alerta_captcha_admin,
            on_store_restored=self.enviar_notificacion_tienda_reactivada,
            on_error=self.enviar_error_chequeo,
        )
        self.scheduler = AsyncIOScheduler()
        self.pending_requests: set[int] = set()

    async def setup_hook(self) -> None:
        """Se ejecuta al iniciar el bot antes de conectar al gateway de Discord."""
        await self.db.init_db()
        await self.worker.start()

        # Programar revisión periódica cada 5 minutos (evalúa qué tiendas/productos
        # han cumplido su ventana de 3h / 6h / 12h o 1h en modo anti-captcha)
        self.scheduler.add_job(
            self.worker.run_scheduled_checks,
            trigger="interval",
            minutes=5,
            id="scheduled_price_checks",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()

        await self.tree.sync()
        logger.info("Comandos Slash sincronizados y scheduler iniciado.")

    async def close(self) -> None:
        """Cierra ordenadamente el scheduler, el navegador Playwright y la sesión de Discord."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        await self.worker.stop()
        await super().close()

    async def on_ready(self) -> None:
        assert self.user is not None
        logger.info(
            "Bot conectado como %s (ID: %s). Canal Admin ID: %s",
            self.user,
            self.user.id,
            self.admin_channel_id,
        )

    # =========================================================================
    # Control de Acceso y Manejo de Solicitudes
    # =========================================================================

    async def enviar_solicitud_admin(
        self, user: discord.abc.User, origen: str
    ) -> bool:
        """
        Envía una solicitud de acceso al canal #admin con botones [Aprobar] y [Rechazar].
        Retorna True si fue enviada, False si ya había una pendiente o falta configurar #admin.
        """
        if user.id in self.pending_requests:
            return False

        await self.db.upsert_user(
            discord_id=user.id,
            username=user.name,
            autorizado=False,
        )

        if not self.admin_channel_id:
            logger.error("ADMIN_CHANNEL_ID no está configurado en el archivo .env.")
            return False

        admin_channel = self.get_channel(self.admin_channel_id)
        if admin_channel is None:
            try:
                admin_channel = await self.fetch_channel(self.admin_channel_id)
            except discord.DiscordException as exc:
                logger.error("No se pudo obtener el canal #admin (%s): %s", self.admin_channel_id, exc)
                return False

        if not isinstance(admin_channel, discord.abc.Messageable):
            return False

        self.pending_requests.add(user.id)

        embed = discord.Embed(
            title="🔐 Nueva Solicitud de Acceso",
            description=(
                f"**Usuario:** {user.mention} (`{user.name}`)\n"
                f"**Discord ID:** `{user.id}`\n"
                f"**Origen:** {origen}\n\n"
                "¿Deseas autorizar a este usuario y crear su canal privado `#precios-"
                f"{sanitize_channel_suffix(user.name)}`?"
            ),
            color=discord.Color.gold(),
        )
        embed.set_thumbnail(url=user.display_avatar.url)

        view = SolicitudAccesoView(
            bot=self,
            solicitante_id=user.id,
            solicitante_nombre=user.name,
        )
        await admin_channel.send(embed=embed, view=view)
        return True

    async def verificar_acceso_interaccion(
        self, interaction: discord.Interaction
    ) -> Optional[dict[str, Any]]:
        """
        Verifica que el usuario esté autorizado y que esté ejecutando el comando
        dentro de su propio canal privado asignado.
        """
        user_record = await self.db.get_user(interaction.user.id)
        if not user_record or not user_record["autorizado"]:
            enviado = await self.enviar_solicitud_admin(
                interaction.user, origen=f"Intento de usar comando en <#{interaction.channel_id}>"
            )
            texto = (
                "🔒 **No tienes acceso autorizado todavía.**\n"
                "Se ha enviado una solicitud de acceso al canal `#admin`. "
                "Cuando el administrador la apruebe, se creará tu canal privado `#precios-"
                f"{sanitize_channel_suffix(interaction.user.name)}`."
                if enviado
                else "⏳ **Tu solicitud de acceso ya está pendiente de revisión** por un administrador."
            )
            await interaction.response.send_message(texto, ephemeral=True)
            return None

        expected_channel_id = user_record.get("channel_id")
        if expected_channel_id and interaction.channel_id != int(expected_channel_id):
            await interaction.response.send_message(
                f"⚠️ Por favor utiliza tus comandos exclusivamente dentro de tu canal privado: <#{expected_channel_id}>.",
                ephemeral=True,
            )
            return None

        return user_record

    # =========================================================================
    # Ingesta Automática de Links por Mensaje y Chequeo Inicial
    # =========================================================================

    async def registrar_link_usuario(
        self,
        user_id: int,
        url: str,
    ) -> tuple[dict[str, Any], discord.Embed, ConfigurarProductoView]:
        """Crea el producto en BD y construye el Embed + View de configuración."""
        producto = await self.db.add_product(user_id=user_id, url=url)
        embed = discord.Embed(
            title="🛒 Producto Registrado",
            description=(
                f"**ID:** `#{producto['id']}`\n"
                f"**Tienda detectada:** `{producto['tienda'].upper()}`\n"
                f"**URL:** {producto['url']}\n\n"
                "¿Deseas definir un **precio objetivo** específico o prefieres que te avise ante **cualquier bajada / descuento**?"
            ),
            color=discord.Color.blurple(),
        )
        view = ConfigurarProductoView(
            bot=self,
            product_id=producto["id"],
            user_id=user_id,
        )
        return producto, embed, view

    async def ejecutar_chequeo_inicial(
        self, product_id: int, channel_id: Optional[int]
    ) -> None:
        """Realiza el primer escaneo tras agregar un producto y envía la ficha inicial."""
        resultado = await self.worker.check_single_product(
            product_id=product_id,
            force_notify_user=True,
        )
        if not channel_id or resultado is None:
            return

        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return

        await self._enviar_resumen_chequeo(
            channel=channel,
            resultado=resultado,
            titulo="📸 Primera Lectura Completada",
        )

    async def on_message(self, message: discord.Message) -> None:
        """
        Escucha mensajes entrantes:
        - Si un usuario no autorizado escribe (por DM o mencionando al bot), envía solicitud a #admin.
        - Si un usuario autorizado pega un link en su canal privado, lo registra automáticamente.
        """
        if message.author.bot:
            return

        # Ignorar mensajes en el propio canal de administración a menos que sea un link de un usuario
        user_record = await self.db.get_user(message.author.id)

        if not user_record or not user_record["autorizado"]:
            # Si escribe por DM o menciona al bot o escribe en un canal donde el bot lo ve
            es_dm = isinstance(message.channel, discord.DMChannel)
            menciona_bot = self.user in message.mentions if self.user else False
            if es_dm or menciona_bot:
                enviado = await self.enviar_solicitud_admin(
                    message.author,
                    origen="Mensaje directo / mención al bot",
                )
                if enviado:
                    await message.reply(
                        "🔒 Hola, este bot requiere autorización. He enviado tu solicitud al canal `#admin`. "
                        "En cuanto te aprueben, se creará tu canal privado de seguimiento.",
                        mention_author=True,
                    )
            return

        # Usuario autorizado: verificar que esté en su canal privado exclusivo
        if message.channel.id != user_record.get("channel_id"):
            return

        # Detectar si pegó una o varias URLs en el mensaje
        urls = URL_REGEX.findall(message.content)
        if not urls:
            return

        for url in urls[:3]:  # Máximo 3 links por mensaje para evitar flood accidental
            _, embed, view = await self.registrar_link_usuario(
                user_id=message.author.id,
                url=url,
            )
            await message.reply(embed=embed, view=view, mention_author=False)

    # =========================================================================
    # Envío de Alertas de Precios y Alertas Anti-Captcha
    # =========================================================================

    async def _enviar_resumen_chequeo(
        self,
        channel: discord.abc.Messageable,
        resultado: ResultadoChequeo,
        titulo: str,
    ) -> None:
        """Construye y envía un Embed con la captura de pantalla adjunta."""
        prod = resultado.producto_despues
        ia = resultado.datos_ia

        if ia.es_captcha:
            embed = discord.Embed(
                title=f"🛡️ Bloqueo / CAPTCHA Detectado (Producto #{prod['id']})",
                description=(
                    f"La tienda **{prod['tienda'].upper()}** presentó una pantalla de verificación o login forzado.\n"
                    "Se ha notificado automáticamente al administrador para que lo resuelva vía noVNC."
                ),
                color=discord.Color.orange(),
            )
        else:
            color = (
                discord.Color.green()
                if resultado.motivos_alerta
                else discord.Color.blue()
            )
            descripcion_alertas = (
                "\n".join(resultado.motivos_alerta)
                if resultado.motivos_alerta
                else "Lectura actualizada y guardada en el historial."
            )
            embed = discord.Embed(
                title=f"{titulo}: {prod.get('nombre') or ia.nombre_producto}",
                url=prod["url"],
                description=descripcion_alertas,
                color=color,
            )
            embed.add_field(
                name="💰 Precio Actual",
                value=f"**{format_price(prod.get('precio_actual'))}**",
                inline=True,
            )
            embed.add_field(
                name="🏷️ Precio Anterior / Descuento",
                value=(
                    f"{format_price(prod.get('precio_anterior'))} "
                    f"(`{prod.get('descuento_actual', 0)}% OFF`)"
                    if prod.get("descuento_actual", 0) > 0
                    else "Sin descuento"
                ),
                inline=True,
            )
            embed.add_field(
                name="🎯 Precio Objetivo",
                value=format_price(prod.get("precio_objetivo"))
                if prod.get("precio_objetivo")
                else "Cualquier bajada",
                inline=True,
            )
            embed.add_field(
                name="📦 Disponibilidad",
                value="✅ En Stock" if prod.get("en_stock") else "❌ Sin Stock",
                inline=True,
            )
            embed.add_field(
                name="🏪 Tienda / ID",
                value=f"`{prod['tienda'].upper()}` • ID `#{prod['id']}`",
                inline=True,
            )

        screenshot_file = Path(resultado.screenshot_path)
        if screenshot_file.exists():
            file_attachment = discord.File(
                str(screenshot_file), filename=screenshot_file.name
            )
            embed.set_image(url=f"attachment://{screenshot_file.name}")
            await channel.send(embed=embed, file=file_attachment)
        else:
            await channel.send(embed=embed)

    async def enviar_alerta_precio(self, resultado: ResultadoChequeo) -> None:
        """
        Callback invocado por `ScraperWorker` cuando se cumple al menos 1 de las 4
        condiciones de alerta en el monitoreo automático.
        """
        prod = resultado.producto_despues
        channel_id = prod.get("channel_id")
        if not channel_id:
            return

        channel = self.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.fetch_channel(int(channel_id))
            except discord.DiscordException as exc:
                logger.error("No se pudo abrir el canal privado %s: %s", channel_id, exc)
                return

        if not isinstance(channel, discord.abc.Messageable):
            return

        user_id = prod["user_id"]
        await channel.send(content=f"🔔 ¡Alerta de seguimiento para <@{user_id}>!")
        await self._enviar_resumen_chequeo(
            channel=channel,
            resultado=resultado,
            titulo=f"🚨 Alerta en Producto #{prod['id']}",
        )

    async def enviar_alerta_captcha_admin(
        self,
        tienda: str,
        producto: dict[str, Any],
        screenshot_path: str,
        novnc_url: str,
    ) -> None:
        """
        Callback invocado por `ScraperWorker` cuando Vision AI detecta `es_captcha == True`.
        Envía alerta al canal #admin con el enlace a noVNC para resolución manual.
        """
        if not self.admin_channel_id:
            return

        admin_channel = self.get_channel(self.admin_channel_id)
        if admin_channel is None:
            try:
                admin_channel = await self.fetch_channel(self.admin_channel_id)
            except discord.DiscordException:
                return

        if not isinstance(admin_channel, discord.abc.Messageable):
            return

        embed = discord.Embed(
            title=f"🛑 CAPTCHA / Bloqueo Detectado en `{tienda.upper()}`",
            description=(
                f"Se detectó un desafío de seguridad o inicio de sesión obligatorio al revisar el producto "
                f"**#{producto['id']}** ({producto['url']}).\n\n"
                "### Acciones tomadas automáticamente:\n"
                f"1. La tienda **`{tienda.upper()}`** ha sido marcada como `pausada_captcha = True`.\n"
                "2. Se pausó el resto de la cola de productos de esta tienda.\n"
                "3. La pestaña quedó abierta en el navegador persistente para que puedas resolverlo ahora mismo.\n"
                "4. Mientras esté pausada, el worker probará **1 solo producto por hora** hasta confirmar que el bloqueo desapareció.\n\n"
                f"👉 **[Abrir Escritorio Remoto noVNC ({novnc_url})]({novnc_url})**"
            ),
            color=discord.Color.red(),
        )

        screenshot_file = Path(screenshot_path)
        if screenshot_file.exists():
            file_attachment = discord.File(
                str(screenshot_file), filename=screenshot_file.name
            )
            embed.set_image(url=f"attachment://{screenshot_file.name}")
            await admin_channel.send(embed=embed, file=file_attachment)
        else:
            await admin_channel.send(embed=embed)

    async def enviar_notificacion_tienda_reactivada(
        self, tienda: str, producto: dict[str, Any]
    ) -> None:
        """Notifica al canal #admin cuando una tienda pausada por CAPTCHA vuelve a estar operativa."""
        if not self.admin_channel_id:
            return

        admin_channel = self.get_channel(self.admin_channel_id)
        if admin_channel is None:
            try:
                admin_channel = await self.fetch_channel(self.admin_channel_id)
            except discord.DiscordException:
                return

        if not isinstance(admin_channel, discord.abc.Messageable):
            return

        embed = discord.Embed(
            title=f"✅ Tienda `{tienda.upper()}` Reactivada",
            description=(
                f"La prueba en el producto **#{producto['id']}** confirmó que ya no hay CAPTCHA (`es_captcha == False`).\n"
                f"El monitoreo normal para **`{tienda.upper()}`** ha sido reactivado automáticamente."
            ),
            color=discord.Color.green(),
        )
        await admin_channel.send(embed=embed)

    async def enviar_error_chequeo(
        self,
        producto: dict[str, Any],
        error: Exception,
        screenshot_path: Optional[str] = None,
    ) -> None:
        """
        Informa en el canal privado del usuario (o en #admin si no existe) cualquier
        excepción ocurrida durante la captura con Playwright o el análisis con Gemini Vision AI.
        """
        target_channel_id = producto.get("channel_id") or self.admin_channel_id
        if not target_channel_id:
            return

        channel = self.get_channel(int(target_channel_id))
        if channel is None:
            try:
                channel = await self.fetch_channel(int(target_channel_id))
            except discord.DiscordException as exc:
                logger.error(
                    "No se pudo obtener el canal %s para reportar error: %s",
                    target_channel_id,
                    exc,
                )
                return

        if not isinstance(channel, discord.abc.Messageable):
            return

        detalle_error = f"{type(error).__name__}: {error}"
        if len(detalle_error) > 900:
            detalle_error = detalle_error[:900] + "..."

        embed = discord.Embed(
            title=f"⚠️ Error al Analizar el Producto #{producto.get('id')}",
            description=(
                f"Ocurrió un problema durante la captura o el análisis con IA del producto:\n"
                f"🔗 **URL:** {producto.get('url')}\n"
                f"🏪 **Tienda:** `{str(producto.get('tienda', 'otra')).upper()}`\n\n"
                f"```text\n{detalle_error}\n```"
            ),
            color=discord.Color.red(),
        )

        if screenshot_path:
            screenshot_file = Path(screenshot_path)
            if screenshot_file.exists():
                file_attachment = discord.File(
                    str(screenshot_file), filename=screenshot_file.name
                )
                embed.set_image(url=f"attachment://{screenshot_file.name}")
                await channel.send(embed=embed, file=file_attachment)
                return

        await channel.send(embed=embed)


bot = PriceTrackerBot()


# =============================================================================
# Comandos Slash (app_commands)
# =============================================================================


@bot.tree.command(
    name="solicitar_acceso",
    description="Solicita autorización al administrador para crear tu canal privado de monitoreo.",
)
async def cmd_solicitar_acceso(interaction: discord.Interaction) -> None:
    user_record = await bot.db.get_user(interaction.user.id)
    if user_record and user_record["autorizado"]:
        ch_id = user_record.get("channel_id")
        await interaction.response.send_message(
            f"✅ Ya estás autorizado. Tu canal privado es <#{ch_id}>.",
            ephemeral=True,
        )
        return

    enviado = await bot.enviar_solicitud_admin(
        interaction.user, origen="Comando `/solicitar_acceso`"
    )
    if enviado:
        await interaction.response.send_message(
            "📨 Tu solicitud fue enviada al canal `#admin`. Recibirás una mención en tu nuevo canal privado apenas seas aprobado.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "⏳ Ya tienes una solicitud pendiente de aprobación por el administrador.",
            ephemeral=True,
        )


@bot.tree.command(
    name="agregar",
    description="Agrega una URL de producto a tu lista de monitoreo 24/7.",
)
@app_commands.describe(url="Enlace completo del producto (http:// o https://)")
async def cmd_agregar(interaction: discord.Interaction, url: str) -> None:
    user_record = await bot.verificar_acceso_interaccion(interaction)
    if not user_record:
        return

    if not URL_REGEX.match(url.strip()):
        await interaction.response.send_message(
            "❌ La URL ingresada no es válida. Asegúrate de que comience con `http://` o `https://`.",
            ephemeral=True,
        )
        return

    _, embed, view = await bot.registrar_link_usuario(
        user_id=interaction.user.id,
        url=url.strip(),
    )
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(
    name="lista",
    description="Muestra todos tus productos monitoreados con sus precios, descuentos y stock.",
)
async def cmd_lista(interaction: discord.Interaction) -> None:
    user_record = await bot.verificar_acceso_interaccion(interaction)
    if not user_record:
        return

    productos = await bot.db.get_user_products(interaction.user.id)
    if not productos:
        await interaction.response.send_message(
            "📭 Aún no tienes productos en seguimiento. Pega un enlace en este canal o usa `/agregar <url>`.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title=f"📋 Productos Monitoreados de {interaction.user.display_name}",
        description=f"Total en seguimiento: **{len(productos)}** producto(s)",
        color=discord.Color.blurple(),
    )

    for prod in productos[:25]:
        nombre = prod.get("nombre") or "⏳ Pendiente de primera lectura"
        precio_act = format_price(prod.get("precio_actual"))
        descuento = prod.get("descuento_actual") or 0
        stock_str = "✅ En stock" if prod.get("en_stock") else "❌ Agotado"
        objetivo_str = (
            format_price(prod.get("precio_objetivo"))
            if prod.get("precio_objetivo")
            else "Abierto (cualquier bajada)"
        )
        ult_rev = prod.get("ultima_revision") or "Nunca"
        if ult_rev != "Nunca":
            ult_rev = ult_rev[:19].replace("T", " ") + " UTC"

        valor_campo = (
            f"🏪 **Tienda:** `{prod['tienda'].upper()}` | {stock_str}\n"
            f"💰 **Precio actual:** **{precio_act}** "
            f"{f'(`-{descuento}% OFF`)' if descuento > 0 else ''}\n"
            f"🎯 **Objetivo:** {objetivo_str}\n"
            f"🕒 **Últ. revisión:** `{ult_rev}`\n"
            f"🔗 [Ver producto]({prod['url']})"
        )
        embed.add_field(
            name=f"#{prod['id']} — {nombre[:70]}",
            value=valor_campo,
            inline=False,
        )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="objetivo",
    description="Configura o modifica el precio objetivo de un producto (usa 0 para dejarlo abierto).",
)
@app_commands.describe(
    id="ID numérico del producto (visible en /lista)",
    precio="Precio objetivo deseado (o 0 para alertar con cualquier bajada)",
)
async def cmd_objetivo(
    interaction: discord.Interaction, id: int, precio: float
) -> None:
    user_record = await bot.verificar_acceso_interaccion(interaction)
    if not user_record:
        return

    nuevo_objetivo: Optional[float] = precio if precio > 0 else None
    actualizado = await bot.db.update_target_price(
        product_id=id,
        user_id=interaction.user.id,
        precio_objetivo=nuevo_objetivo,
    )

    if not actualizado:
        await interaction.response.send_message(
            f"❌ No se encontró ningún producto con ID `#{id}` en tu lista.",
            ephemeral=True,
        )
        return

    if nuevo_objetivo is not None:
        await interaction.response.send_message(
            f"🎯 El precio objetivo del producto **#{id}** se actualizó a **{format_price(nuevo_objetivo)}**."
        )
    else:
        await interaction.response.send_message(
            f"📉 Se eliminó el precio fijo del producto **#{id}**. Ahora recibirás alertas ante cualquier descuento o bajada significativa."
        )


@bot.tree.command(
    name="quitar",
    description="Elimina un producto de tu lista de monitoreo.",
)
@app_commands.describe(id="ID numérico del producto a eliminar")
async def cmd_quitar(interaction: discord.Interaction, id: int) -> None:
    user_record = await bot.verificar_acceso_interaccion(interaction)
    if not user_record:
        return

    eliminado = await bot.db.delete_product(
        product_id=id,
        user_id=interaction.user.id,
    )
    if not eliminado:
        await interaction.response.send_message(
            f"❌ No se encontró ningún producto con ID `#{id}` en tu lista.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"🗑️ El producto **#{id}** ha sido eliminado de tu lista de monitoreo."
    )


@bot.tree.command(
    name="revisar",
    description="Fuerza la revisión inmediata de un producto (o pasa 0 para revisar todos tus productos).",
)
@app_commands.describe(
    id="ID del producto a revisar inmediatamente, o 0 para revisar todos tus productos"
)
async def cmd_revisar(interaction: discord.Interaction, id: int) -> None:
    user_record = await bot.verificar_acceso_interaccion(interaction)
    if not user_record:
        return

    await interaction.response.defer(thinking=True)

    if id == 0:
        productos = await bot.db.get_user_products(interaction.user.id)
        if not productos:
            await interaction.followup.send(
                "📭 No tienes productos registrados para revisar."
            )
            return

        await interaction.followup.send(
            f"🔄 Iniciando revisión forzada de **{len(productos)}** producto(s). "
            "Los resultados irán apareciendo a continuación..."
        )
        for prod in productos:
            resultado = await bot.worker.check_single_product(
                product_id=prod["id"],
                force_notify_user=True,
            )
            if resultado and isinstance(interaction.channel, discord.abc.Messageable):
                await bot._enviar_resumen_chequeo(
                    channel=interaction.channel,
                    resultado=resultado,
                    titulo=f"🔎 Revisión Manual (#{prod['id']})",
                )
        return

    # Revisión de un producto específico verificando que pertenezca al usuario
    prod = await bot.db.get_product_by_id(id)
    if not prod or int(prod["user_id"]) != interaction.user.id:
        await interaction.followup.send(
            f"❌ No existe ningún producto con ID `#{id}` asociado a tu cuenta."
        )
        return

    resultado = await bot.worker.check_single_product(
        product_id=id,
        force_notify_user=True,
    )
    if resultado is None:
        await interaction.followup.send(
            f"⚠️ Ocurrió un error al intentar capturar o analizar el producto **#{id}**."
        )
        return

    if isinstance(interaction.channel, discord.abc.Messageable):
        await interaction.followup.send(f"✅ Revisión completada para el producto **#{id}**:")
        await bot._enviar_resumen_chequeo(
            channel=interaction.channel,
            resultado=resultado,
            titulo=f"🔎 Revisión Manual (#{id})",
        )


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "La variable de entorno DISCORD_TOKEN es obligatoria. Configúrala en tu archivo .env."
        )
    bot.run(token)


if __name__ == "__main__":
    main()
