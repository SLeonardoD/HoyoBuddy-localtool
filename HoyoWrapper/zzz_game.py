"""
zzz_game.py
-----------
Módulo de Zenless Zone Zero.

Hereda toda la lógica de BaseGame. Este archivo solo declara las constantes
de identificación del juego.

Particularidades respecto a Genshin:
  - Mimo viajero: usa rutas estándar /<endpoint> (no /qiuqiu/).
  - claim_mimo_task_reward: usa GET (no POST como Genshin).
  - Canje de códigos: usa POST con webExchangeCdkeyRisk.
  - x-rpc-signgame: "zzz"
  - Check-in URL específica: event/luna/zzz/os (distinta de HSR y Honkai).

game_biz soportados:
    nap_global    Zenless Zone Zero (región global, HoYoLab)
    nap_cn        Zenless Zone Zero (región CN, Miyoushe)
"""

from __future__ import annotations

from game_model import BaseGame

__all__ = ["ZZZGame"]


class ZZZGame(BaseGame):
    """Zenless Zone Zero."""

    GAME_BIZ:  str = "nap_global"
    GAME_NAME: str = "Zenless Zone Zero"
    GAME_ID:   int = 8  # ID mimo viajero
