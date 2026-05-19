"""
starrail_game.py
----------------
Módulo de Honkai: Star Rail.

Hereda toda la lógica de BaseGame. Este archivo solo declara las constantes
de identificación del juego.

Particularidades respecto a Genshin:
  - Mimo viajero: usa rutas estándar /<endpoint> (no /qiuqiu/).
  - claim_mimo_task_reward: usa GET (no POST como Genshin).
  - Canje de códigos: usa POST con webExchangeCdkeyRisk.
  - x-rpc-signgame: "hkrpg"

game_biz soportados:
    hkrpg_global    Honkai: Star Rail (región global, HoYoLab)
    hkrpg_cn        Honkai: Star Rail (región CN, Miyoushe)
"""

from __future__ import annotations

from game_model import BaseGame

__all__ = ["StarRailGame"]


class StarRailGame(BaseGame):
    """Honkai: Star Rail."""

    GAME_BIZ:  str = "hkrpg_global"
    GAME_NAME: str = "Honkai: Star Rail"
    GAME_ID:   int = 6  # ID mimo viajero
