"""
HoyoWrapper/genshin_game.py
---------------------------
Módulo de Genshin Impact. Solo constantes; toda la lógica está en BaseGame.

Particularidades manejadas en BaseGame (GAME_ID == 2):
  - Mimo: rutas /<config.MIMO_GENSHIN_PATH_PREFIX>/<endpoint> (el prefijo
    lo rota HoYoverse; se corrige en config.py), POST para claim_mimo_task_reward.
  - Redeem: usa GET (no POST como StarRail/ZZZ).
  - x-rpc-signgame: "hk4e"
"""

from __future__ import annotations

from HoyoWrapper.game_model import BaseGame

__all__ = ["GenshinGame"]


class GenshinGame(BaseGame):
    """Genshin Impact."""

    GAME_BIZ:  str = "hk4e_global"
    GAME_NAME: str = "Genshin Impact"
    GAME_ID:   int = 2
