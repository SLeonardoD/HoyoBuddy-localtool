"""
HoyoWrapper/session.py
----------------------
Objetos de sesión y errores del wrapper.

Separado de auth_manager para que game_model y session_manager puedan
importar HoyoSession y los errores sin depender de la lógica de login.

Exporta:
    HoyoSession     Sesión autenticada con cookies, region y device_id.
    HoyoAPIError    Error de la API de HoYoverse (retcode != 0).
    LoginError      Error durante el proceso de login.
"""

from __future__ import annotations

import logging
import typing
import uuid

import aiohttp

__all__ = ["HoyoSession", "HoyoAPIError", "LoginError"]

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# URLs para obtener cuentas de juego vinculadas
_GAME_ACCOUNTS_OS_URL = (
    "https://api-os-takumi.mihoyo.com/binding/api/getUserGameRolesByCookie"
)
_GAME_ACCOUNTS_CN_URL = (
    "https://api-takumi.mihoyo.com/binding/api/getUserGameRolesByCookie"
)


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------

class HoyoAPIError(Exception):
    """Error devuelto por la API de HoYoverse (retcode != 0)."""

    def __init__(self, retcode: int, message: str, raw: dict) -> None:
        self.retcode = retcode
        self.message = message
        self.raw = raw
        super().__init__(f"[{retcode}] {message}")


class LoginError(Exception):
    """Error durante el proceso de login."""


# ---------------------------------------------------------------------------
# HoyoSession
# ---------------------------------------------------------------------------

class HoyoSession:
    """
    Sesión autenticada con HoYoLab o Miyoushe.

    Atributos:
        cookies     Dict de cookies activas de la sesión.
        region      'os' (HoYoLab global) o 'cn' (Miyoushe).
        device_id   UUID del dispositivo generado durante el login y
                    persistido en DB. Se reutiliza en todos los requests
                    para simular siempre el mismo dispositivo físico.

    Uso:
        # Normalmente obtenida via HoyoManager.login()
        data = await session.get_game_accounts()
    """

    def __init__(
        self,
        cookies: dict[str, str],
        region: str,
        device_id: typing.Optional[str] = None,
    ) -> None:
        self.cookies = cookies
        self.region = region
        # Si no hay device_id (sesión en memoria sin DB), generamos uno nuevo.
        # En uso normal siempre llega desde la DB o del login.
        self.device_id: str = device_id or str(uuid.uuid4())

    @property
    def account_id(self) -> typing.Optional[int]:
        """ID numérico de la cuenta extraído de las cookies."""
        for key in ("ltuid_v2", "account_id_v2", "ltuid", "account_id"):
            if val := self.cookies.get(key):
                try:
                    return int(val)
                except ValueError:
                    pass
        return None

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: typing.Optional[dict] = None,
        # 'body' en lugar de 'json' para no shadowear el módulo json.
        body: typing.Optional[dict] = None,
        headers: typing.Optional[dict] = None,
    ) -> typing.Any:
        """
        Petición HTTP autenticada con las cookies de la sesión.

        Devuelve data{} de la respuesta JSON de HoYoverse,
        o lanza HoyoAPIError si retcode != 0.

        Parámetros:
            method      'GET' o 'POST'.
            url         URL completa del endpoint.
            params      Query params opcionales.
            body        Cuerpo JSON del request (para POST).
            headers     Headers adicionales a los por defecto.
        """
        merged_headers = {"User-Agent": _USER_AGENT}
        if headers:
            merged_headers.update(headers)

        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar()
        ) as http_session:
            async with http_session.request(
                method,
                url,
                params=params,
                json=body,              # aiohttp espera 'json', no 'body'
                headers=merged_headers,
                cookies=self.cookies,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)

        retcode = data.get("retcode", 0)
        if retcode != 0:
            raise HoyoAPIError(
                retcode,
                data.get("message", "Unknown error"),
                data,
            )

        return data.get("data", data)

    async def get_game_accounts(self) -> list[dict]:
        """
        Lista de cuentas de juego vinculadas al usuario.

        Cada elemento contiene:
            game_biz    'hk4e_global', 'hkrpg_global', ...
            game_uid    UID del jugador en ese juego
            nickname    Nombre del personaje
            level       Nivel del jugador
            region      Servidor (e.g. 'os_asia')
            region_name Nombre legible del servidor
        """
        url = (
            _GAME_ACCOUNTS_OS_URL
            if self.region == "os"
            else _GAME_ACCOUNTS_CN_URL
        )
        data = await self.request("GET", url)
        return data.get("list", [])

    def __repr__(self) -> str:
        return (
            f"<HoyoSession region={self.region!r} "
            f"account_id={self.account_id} "
            f"device_id={self.device_id!r}>"
        )
