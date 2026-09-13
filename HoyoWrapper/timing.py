"""
HoyoWrapper/timing.py
---------------------
Helper de anti-deteccion compartido: espera aleatoria entre operaciones.

Separado en su propio modulo (igual que ds_utils.py) para que tanto
auth_manager.py (capa de autenticacion) como game_model.py (logica de
juego) puedan usarlo sin que ninguno dependa del otro — auth_manager
necesita delays humanos tanto como game_model (login -> intercambio de
cookie_token_v2, login -> primera consulta de cuentas), y no tendria
sentido que la capa de autenticacion dependa de la de logica de juego
solo por esto.
"""

from __future__ import annotations

import asyncio
import random

from loguru import logger

__all__ = ["random_delay"]


async def random_delay(min_s: float, max_s: float) -> None:
    """
    Espera aleatoria con decimales entre operaciones para evitar patrones
    de timing robotico. Valores tipicos: 1.3s, 2.7s, etc.

    Siempre requiere un rango explicito (config.DELAY_*) — no tiene
    default propio para forzar a cada llamador a declarar A PROPOSITO
    que transicion esta simulando, en vez de heredar silenciosamente un
    rango pensado para otro contexto.
    """
    delay = round(random.uniform(min_s, max_s), 2)
    logger.debug("[ANTI-DETECCION] Delay de {}s.", delay)
    await asyncio.sleep(delay)
