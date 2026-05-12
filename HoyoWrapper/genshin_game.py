"""
genshin_game.py
---------------
Módulo de Genshin Impact.

Hereda toda la lógica de BaseGame. Este archivo solo declara las constantes
de identificación del juego.

Particularidades de Genshin respecto al resto:
  - Mimo viajero: usa rutas /qiuqiu/<endpoint> y POST para claim_mimo_task_reward.
    Esto está manejado en BaseGame mediante la comprobación GAME_ID == 2.
  - Canje de códigos: usa GET (no POST como StarRail/ZZZ).
    Esto está manejado en BaseGame mediante _CODE_POST_GAMES.
  - x-rpc-signgame: "hk4e"

game_biz soportados:
    hk4e_global     Genshin Impact (región global, HoYoLab)
    hk4e_cn         Genshin Impact (región CN, Miyoushe)
"""

from __future__ import annotations

from game_model import BaseGame

__all__ = ["GenshinGame"]


class GenshinGame(BaseGame):
    """Genshin Impact."""

    GAME_BIZ:  str = "hk4e_global"
    GAME_NAME: str = "Genshin Impact"
    GAME_ID:   int = 2  # ID mimo viajero
