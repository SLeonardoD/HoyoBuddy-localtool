"""
HoyoWrapper/
------------
Wrapper minimalista para automatizar tareas en HoYoLab / Miyoushe.

Exporta la interfaz pública del paquete. El usuario final solo necesita
importar desde aquí y desde db/.

Uso típico:
    from db import CookieStore
    from HoyoWrapper import HoyoManager, ActiveSession

    store   = CookieStore("./hoyo_auth.db")
    manager = HoyoManager(store)
    session = await manager.login("email@ejemplo.com", "contraseña")

    results = await session.checkin_all()
"""

from HoyoWrapper.session import HoyoAPIError, HoyoSession, LoginError
from HoyoWrapper.session_manager import ActiveSession, HoyoManager
from HoyoWrapper.game_model import (
    BaseGame,
    GAME_BIZ_MAP,
    GAME_ID_MAP,
    CURRENCY_ITEM_KEYWORDS,
    create_game,
    random_delay,
)

__all__ = [
    # Punto de entrada
    "HoyoManager",
    "ActiveSession",
    # Sesión y errores
    "HoyoSession",
    "HoyoAPIError",
    "LoginError",
    # Juego base y utilidades
    "BaseGame",
    "GAME_BIZ_MAP",
    "GAME_ID_MAP",
    "CURRENCY_ITEM_KEYWORDS",
    "create_game",
    "random_delay",
]
