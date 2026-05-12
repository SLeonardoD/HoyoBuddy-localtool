"""
session_manager.py
------------------
Punto de entrada principal de la librería simplificada de HoYoLab.

Orquesta el login, la detección de juegos y la creación de los objetos
de juego correspondientes. Es lo único que el usuario final necesita importar.

Uso típico:
    from session_manager import HoyoManager

    manager = HoyoManager()
    session = await manager.login("email@ejemplo.com", "contraseña")

    # Ver qué juegos tiene el usuario
    for game in session.games:
        print(game)

    # Hacer check-in diario en todos los juegos detectados
    results = await session.checkin_all()

    # Tareas y tienda del mimo viajero
    for game in session.games:
        tasks = await game.get_mimo_tasks()
        ...
"""

from __future__ import annotations

import logging
import typing

from auth_manager import AuthManager, HoyoSession, LoginError, HoyoAPIError
from game_model import BaseGame, create_game

__all__ = ["HoyoManager", "ActiveSession"]

logger = logging.getLogger(__name__)


class ActiveSession:
    """
    Sesión activa con los juegos del usuario ya detectados y listos.

    Atributos:
        session     HoyoSession autenticada (cookies, region).
        games       Lista de objetos BaseGame (uno por juego detectado).
        raw_accounts Lista de dicts con los datos brutos de las cuentas.
    """

    def __init__(self, session: HoyoSession, accounts: list[dict]) -> None:
        self.session = session
        self.raw_accounts = accounts
        self.games: list[BaseGame] = [
            create_game(session, acc) for acc in accounts
        ]

    @property
    def region(self) -> str:
        """'os' o 'cn'."""
        return self.session.region

    @property
    def account_id(self) -> typing.Optional[int]:
        """ID numérico de la cuenta HoYoLab."""
        return self.session.account_id

    def get_game(self, game_biz: str) -> typing.Optional[BaseGame]:
        """Devuelve el objeto de juego por game_biz, o None si no existe."""
        return next((g for g in self.games if g.game_biz == game_biz), None)

    async def checkin_all(self) -> list[dict]:
        """
        Realiza el check-in diario en todos los juegos detectados.

        Devuelve lista de dicts con el resultado por juego:
            game_name   str
            uid         str
            success     bool
            message     str
            reward      dict | None   (el reward del día si se reclamó)
        """
        results = []
        for game in self.games:
            try:
                result = await game.claim_daily_reward()
                results.append({
                    "game_name": game.GAME_NAME,
                    "uid": game.uid,
                    "success": True,
                    "message": result.get("message", "Check-in realizado."),
                    "reward": result.get("reward"),
                })
            except HoyoAPIError as e:
                results.append({
                    "game_name": game.GAME_NAME,
                    "uid": game.uid,
                    "success": False,
                    "message": str(e),
                    "reward": None,
                })
            except NotImplementedError:
                pass  # Juego no soportado aún
        return results

    def __repr__(self) -> str:
        game_names = [g.GAME_NAME for g in self.games]
        return (
            f"<ActiveSession region={self.region!r} "
            f"account_id={self.account_id} "
            f"games={game_names}>"
        )


class HoyoManager:
    """
    Punto de entrada principal.

    Parámetros:
        db_path         Ruta al archivo SQLite. Por defecto './hoyo_auth.db'.
        captcha_port    Puerto local para el servidor de captcha. Por defecto 5000.
        lang            Idioma del widget Geetest ('en', 'es', 'zh-cn', ...).

    Uso:
        manager = HoyoManager()
        session = await manager.login("email", "password")
        print(session.games)
    """

    def __init__(
        self,
        db_path: str = "./hoyo_auth.db",
        captcha_port: int = 5000,
        lang: str = "en",
    ) -> None:
        self._auth = AuthManager(
            db_path=db_path,
            captcha_port=captcha_port,
            lang=lang,
        )

    async def login(
        self,
        email: str,
        password: str,
        *,
        region: typing.Optional[typing.Literal["os", "cn"]] = None,
    ) -> ActiveSession:
        """
        Autentica y devuelve una ActiveSession con los juegos del usuario listos.

        Si ya hay cookies guardadas en la DB y siguen siendo válidas,
        no se necesita contraseña ni captcha.

        Parámetros:
            email       Email o usuario de HoYoLab / Miyoushe.
            password    Contraseña.
            region      'os' o 'cn'. Si no se indica, se detecta automáticamente.

        Lanza:
            LoginError  Si las credenciales son incorrectas.
        """
        # 1. Autenticar y obtener sesión con cookies
        hoyo_session = await self._auth.login(email, password, region=region)

        # 2. Detectar qué juegos tiene el usuario
        try:
            accounts = await hoyo_session.get_game_accounts()
        except HoyoAPIError as e:
            raise LoginError(f"Login exitoso pero error al obtener cuentas: {e}") from e

        if not accounts:
            logger.warning("El usuario no tiene cuentas de juego vinculadas.")

        # 3. Filtrar solo los game_biz que tienen soporte implementado
        _SUPPORTED = {
            "hk4e_global", "hk4e_cn",
            "hkrpg_global", "hkrpg_cn",
            "nap_global", "nap_cn",
            "bh3_global", "bh3_cn",
        }
        supported_accounts = [
            acc for acc in accounts
            if acc.get("game_biz", "") in _SUPPORTED
        ]

        logger.info(
            "Detectados %d juego(s) para account_id=%s",
            len(supported_accounts),
            hoyo_session.account_id,
        )

        return ActiveSession(hoyo_session, supported_accounts)

    async def logout(self, email: str) -> None:
        """Elimina las cookies guardadas del usuario de la DB."""
        await self._auth.logout(email)
