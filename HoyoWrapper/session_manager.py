"""
HoyoWrapper/session_manager.py
------------------------------
Punto de entrada principal del wrapper.

Orquesta login, detección de juegos y creación de objetos de juego.
Recibe CookieStore por inyección de dependencia (no importa db/).

Uso típico:
    from db import CookieStore
    from HoyoWrapper import HoyoManager

    store   = CookieStore("./hoyo_auth.db")
    manager = HoyoManager(store)
    session = await manager.login("email@ejemplo.com", "contraseña")

    for game in session.games:
        print(game)

    results = await session.checkin_all()
"""

from __future__ import annotations

import logging
import typing

import config
from HoyoWrapper.auth_manager import AuthManager
from HoyoWrapper.game_model import BaseGame, create_game, random_delay
from HoyoWrapper.session import HoyoAPIError, HoyoSession, LoginError

__all__ = ["HoyoManager", "ActiveSession"]

logger = logging.getLogger(__name__)

# Juegos soportados por el wrapper (definido una sola vez en config.py;
# app.py y este archivo antes tenian cada uno su propia copia de este set).
_SUPPORTED_GAME_BIZ = config.SUPPORTED_GAME_BIZ


class ActiveSession:
    """
    Sesión activa con los juegos del usuario detectados y listos.

    Atributos:
        session      HoyoSession autenticada (cookies, region, device_id).
        games        Lista de BaseGame (uno por juego detectado).
        raw_accounts Dicts brutos de las cuentas de juego.
    """

    def __init__(self, session: HoyoSession, accounts: list[dict]) -> None:
        self.session = session
        self.raw_accounts = accounts
        self.games: list[BaseGame] = [create_game(session, acc) for acc in accounts]

    @property
    def region(self) -> str:
        return self.session.region

    @property
    def account_id(self) -> typing.Optional[int]:
        return self.session.account_id

    def get_game(self, game_biz: str) -> typing.Optional[BaseGame]:
        """Devuelve el objeto de juego por game_biz, o None si no existe."""
        return next((g for g in self.games if g.game_biz == game_biz), None)

    async def checkin_all(self) -> list[dict]:
        """
        Check-in diario en todos los juegos detectados.

        Incluye delay aleatorio entre juegos (1–3s) para evitar patrones
        de timing robótico que activen sistemas anti-bot de HoYoverse.

        Devuelve lista de dicts por juego:
            game_name       str
            uid             str
            success         bool
            message         str
            already_done    bool
            total_sign_day  int
        """
        results = []
        for i, game in enumerate(self.games):
            if i > 0:
                await random_delay(*config.DELAY_BETWEEN_GAMES)

            try:
                result = await game.claim_daily_reward()
                results.append({
                    "game_name":      game.GAME_NAME,
                    "uid":            game.uid,
                    "success":        True,
                    "message":        result.get("message", "Check-in realizado."),
                    "already_done":   result.get("already_done", False),
                    "total_sign_day": result.get("total_sign_day", 0),
                })
            except HoyoAPIError as e:
                results.append({
                    "game_name":      game.GAME_NAME,
                    "uid":            game.uid,
                    "success":        False,
                    "message":        str(e),
                    "already_done":   False,
                    "total_sign_day": 0,
                })
            except NotImplementedError:
                pass

        return results

    def __repr__(self) -> str:
        return (
            f"<ActiveSession region={self.region!r} "
            f"account_id={self.account_id} "
            f"games={[g.GAME_NAME for g in self.games]}>"
        )


class HoyoManager:
    """
    Punto de entrada principal del wrapper.

    Parámetros:
        store           CookieStore (inyectado — este objeto no crea la DB).
        captcha_port    Puerto local del servidor de captcha. Por defecto 5000.
        lang            Idioma del widget Geetest. Por defecto 'en'.

    Uso:
        from db import CookieStore
        from HoyoWrapper import HoyoManager

        manager = HoyoManager(CookieStore())
        session = await manager.login("email", "password")
    """

    def __init__(
        self,
        store: typing.Any,          # CookieStore o compatible
        captcha_port: int = config.CAPTCHA_PORT_DEFAULT,
        lang: str = config.CAPTCHA_LANG_OS,
    ) -> None:
        self._auth = AuthManager(store, captcha_port=captcha_port, lang=lang)

    async def login(
        self,
        email: str,
        password: str,
        *,
        region: typing.Optional[typing.Literal["os", "cn"]] = None,
    ) -> ActiveSession:
        """
        Autentica y devuelve ActiveSession con los juegos del usuario listos.

        Si hay cookies válidas en el store no se necesita contraseña ni captcha.

        Lanza:
            LoginError  Credenciales incorrectas.
        """
        hoyo_session = await self._auth.login(email, password, region=region)

        try:
            accounts = await hoyo_session.get_game_accounts()
        except HoyoAPIError as e:
            raise LoginError(f"Login exitoso pero error al obtener cuentas: {e}") from e

        if not accounts:
            logger.warning("El usuario no tiene cuentas de juego vinculadas.")

        supported = [acc for acc in accounts if acc.get("game_biz", "") in _SUPPORTED_GAME_BIZ]
        logger.info("Detectados %d juego(s) para account_id=%s.",
                    len(supported), hoyo_session.account_id)

        return ActiveSession(hoyo_session, supported)

    async def logout(self, email: str) -> None:
        """Elimina las cookies del usuario del store."""
        await self._auth.logout(email)
