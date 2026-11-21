"""
tests/test_main_selection.py
----------------------------
Flujo interactivo de la opcion "Completar tareas diarias" de main.py:
eleccion de cuentas y de juegos por cuenta.

main.py solo hace entrada/salida, pero este flujo tiene reglas propias que
no deben romperse sin aviso:

  - Enter = comportamiento de siempre (todas las cuentas, todos los juegos).
  - Ctrl+C / fin de entrada JAMAS equivale a "todas": cancela u omite.
  - Una seleccion invalida se vuelve a preguntar, no se interpreta a medias.

Importar `main` tiene efectos secundarios (quita los handlers de loguru y crea
un archivo error_log_*.txt en el directorio actual), asi que los escenarios
corren en UN subproceso con un directorio temporal como cwd, con input() y
run_daily_tasks reemplazados. Nada toca red ni la base de datos real.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_SCRIPT = r'''
import asyncio, builtins, json
import main

ACCOUNTS = [
    {"alias": "A", "region": "os"},
    {"alias": "B", "region": "os"},
    {"alias": "C", "region": "cn"},
]
main.list_accounts = lambda ctx: ACCOUNTS


def make_input(answers):
    it = iter(answers)
    def fake_input(prompt=""):
        answer = next(it)           # si el flujo pide mas de lo previsto: StopIteration
        if answer == "<CTRL+C>":
            raise KeyboardInterrupt
        return answer
    return fake_input


def run_menu(answers):
    builtins.input = make_input(answers)
    calls = []
    async def fake_run(ctx, *, aliases=None, select_games=None):
        calls.append({"aliases": aliases, "has_chooser": select_games is not None})
        return {"accounts": []}
    main.run_daily_tasks = fake_run
    asyncio.run(main._action_run_daily(None))
    return calls


class Game:
    def __init__(self, name, uid):
        self.GAME_NAME, self.uid = name, uid
        self.nickname, self.server_name, self.server = "Nick" + uid, "Srv", "srv"

GAMES = [Game("Genshin Impact", "1"), Game("Honkai: Star Rail", "2"), Game("Zenless Zone Zero", "3")]


def run_chooser(answers):
    builtins.input = make_input(answers)
    chosen = asyncio.run(main._choose_games_prompt("A", GAMES))
    return [g.uid for g in chosen]


MENU = {
    "enter_all_then_yes":            ["", "s"],
    "enter_all_then_no":             ["", "n"],
    "enter_all_then_enter":          ["", ""],
    "invalid_mode_then_all":         ["x", "", "s"],
    "choose_two_accounts":           ["e", "2,3", "s"],
    "choose_uppercase_e":            ["E", "1", "s"],
    "invalid_then_valid_accounts":   ["e", "9", "x", "1-2", "s"],
    "choose_then_zero":              ["e", "0"],
    "choose_then_ctrl_c":            ["e", "<CTRL+C>"],
    "ctrl_c_at_mode":                ["<CTRL+C>"],
    "choose_then_decline":           ["e", "1", "n"],
}
CHOOSER = {
    "enter_means_all":               [""],
    "subset":                        ["1,3"],
    "zero_skips_account":            ["0"],
    "ctrl_c_skips_account":          ["<CTRL+C>"],
    "invalid_then_valid":            ["5", "2"],
}

out = {
    "menu":    {name: run_menu(a) for name, a in MENU.items()},
    "chooser": {name: run_chooser(a) for name, a in CHOOSER.items()},
}
print("@@RESULT@@" + json.dumps(out))
'''


@pytest.fixture(scope="module")
def flows(tmp_path_factory) -> dict:
    cwd = tmp_path_factory.mktemp("main_cwd")
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=cwd, env=env, capture_output=True, timeout=120,
    )
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    marker = [ln for ln in stdout.splitlines() if ln.startswith("@@RESULT@@")]
    assert proc.returncode == 0 and marker, (
        f"El subproceso de escenarios fallo (rc={proc.returncode}).\n"
        f"--- stdout ---\n{stdout[-2000:]}\n--- stderr ---\n{stderr[-2000:]}"
    )
    return json.loads(marker[0][len("@@RESULT@@"):])


class TestAccountChoiceFlow:

    def test_enter_keeps_the_old_behaviour_everything(self, flows: dict) -> None:
        calls = flows["menu"]["enter_all_then_yes"]
        assert calls == [{"aliases": None, "has_chooser": False}], (
            f"Enter + confirmar debe correr TODO (sin filtros ni selector). Obtenido: {calls}"
        )

    @pytest.mark.parametrize("scenario", [
        "enter_all_then_no", "enter_all_then_enter", "choose_then_decline",
    ])
    def test_declining_the_confirmation_runs_nothing(self, flows: dict, scenario: str) -> None:
        assert flows["menu"][scenario] == [], f"{scenario}: no se confirmo, no debe correr nada."

    def test_invalid_mode_asks_again(self, flows: dict) -> None:
        assert flows["menu"]["invalid_mode_then_all"] == [{"aliases": None, "has_chooser": False}]

    def test_choosing_accounts_passes_their_aliases_and_a_game_selector(self, flows: dict) -> None:
        assert flows["menu"]["choose_two_accounts"] == [{"aliases": ["B", "C"], "has_chooser": True}]

    def test_mode_letter_is_case_insensitive(self, flows: dict) -> None:
        assert flows["menu"]["choose_uppercase_e"] == [{"aliases": ["A"], "has_chooser": True}]

    def test_invalid_account_selection_asks_again_and_accepts_ranges(self, flows: dict) -> None:
        assert flows["menu"]["invalid_then_valid_accounts"] == [
            {"aliases": ["A", "B"], "has_chooser": True}
        ]

    @pytest.mark.parametrize("scenario", [
        "choose_then_zero", "choose_then_ctrl_c", "ctrl_c_at_mode",
    ])
    def test_cancelling_never_runs_all(self, flows: dict, scenario: str) -> None:
        assert flows["menu"][scenario] == [], (
            f"{scenario}: cancelar (0 / Ctrl+C) no debe disparar la corrida, y menos "
            f"interpretarse como 'todas'."
        )


class TestGameChoiceFlow:

    def test_enter_means_every_game_of_the_account(self, flows: dict) -> None:
        assert flows["chooser"]["enter_means_all"] == ["1", "2", "3"]

    def test_numbers_pick_a_subset(self, flows: dict) -> None:
        assert flows["chooser"]["subset"] == ["1", "3"]

    def test_zero_skips_the_account(self, flows: dict) -> None:
        assert flows["chooser"]["zero_skips_account"] == []

    def test_ctrl_c_skips_the_account_instead_of_running_all(self, flows: dict) -> None:
        assert flows["chooser"]["ctrl_c_skips_account"] == [], (
            "Ctrl+C en el selector de juegos debe omitir la cuenta; si se tratara como "
            "Enter correria todos los juegos que el usuario queria evitar."
        )

    def test_invalid_selection_asks_again(self, flows: dict) -> None:
        assert flows["chooser"]["invalid_then_valid"] == ["2"]
