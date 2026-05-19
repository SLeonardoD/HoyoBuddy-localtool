"""
HoyoWrapper/starrail_game.py
----------------------------
Módulo de Honkai: Star Rail. Solo constantes; toda la lógica está en BaseGame.

Particularidades respecto a Genshin:
  - Mimo: rutas estándar /<endpoint>, GET para claim_mimo_task_reward.
  - Redeem: POST con webExchangeCdkeyRisk.
  - x-rpc-signgame: "hkrpg"
"""

from __future__ import annotations

from HoyoWrapper.game_model import BaseGame

__all__ = ["StarRailGame"]


class StarRailGame(BaseGame):
    """Honkai: Star Rail."""

    GAME_BIZ:  str = "hkrpg_global"
    GAME_NAME: str = "Honkai: Star Rail"
    GAME_ID:   int = 6
