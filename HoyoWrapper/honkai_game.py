"""
honkai_game.py
--------------
Módulo de Honkai Impact 3rd.

Hereda toda la lógica de BaseGame. Este archivo solo declara las constantes
de identificación del juego.

Limitaciones conocidas de Honkai Impact 3rd respecto al resto de juegos:
  - Sin mimo viajero: GAME_ID = 0, los métodos de mimo lanzan NotImplementedError.
  - Sin canje de códigos vía API pública: redeem_code() lanza NotImplementedError.
  - Check-in sí está soportado, tanto OS como CN.
  - x-rpc-signgame: "bh3"

game_biz soportados:
    bh3_global    Honkai Impact 3rd (región global, HoYoLab)
    bh3_cn        Honkai Impact 3rd (región CN, Miyoushe)
"""

from __future__ import annotations

from game_model import BaseGame

__all__ = ["HonkaiGame"]


class HonkaiGame(BaseGame):
    """Honkai Impact 3rd."""

    GAME_BIZ:  str = "bh3_global"
    GAME_NAME: str = "Honkai Impact 3rd"
    GAME_ID:   int = 0  # Sin soporte de mimo viajero
