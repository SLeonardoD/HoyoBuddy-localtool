"""
HoyoWrapper/honkai_game.py
--------------------------
Módulo de Honkai Impact 3rd. Solo constantes; toda la lógica está en BaseGame.

Limitaciones (GAME_ID = 0):
  - Sin mimo viajero: los métodos de mimo lanzan NotImplementedError.
  - Sin canje de códigos vía API pública: redeem_code() lanza NotImplementedError.
  - Check-in sí soportado, tanto OS como CN.
  - x-rpc-signgame: "bh3"
"""

from __future__ import annotations

from HoyoWrapper.game_model import BaseGame

__all__ = ["HonkaiGame"]


class HonkaiGame(BaseGame):
    """Honkai Impact 3rd."""

    GAME_BIZ:  str = "bh3_global"
    GAME_NAME: str = "Honkai Impact 3rd"
    GAME_ID:   int = 0
