"""
HoyoWrapper/zzz_game.py
-----------------------
Módulo de Zenless Zone Zero. Solo constantes; toda la lógica está en BaseGame.

Particularidades respecto a Genshin:
  - Mimo: rutas estándar /<endpoint>, GET para claim_mimo_task_reward.
  - Redeem: POST con webExchangeCdkeyRisk.
  - x-rpc-signgame: "zzz"
  - Check-in URL propia: event/luna/zzz/os (distinta de HSR y Honkai).
"""

from __future__ import annotations

from HoyoWrapper.game_model import BaseGame

__all__ = ["ZZZGame"]


class ZZZGame(BaseGame):
    """Zenless Zone Zero."""

    GAME_BIZ:  str = "nap_global"
    GAME_NAME: str = "Zenless Zone Zero"
    GAME_ID:   int = 8
