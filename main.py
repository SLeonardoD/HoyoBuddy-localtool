"""
main.py
-------
Punto de entrada de la aplicacion HoYoWrapper.

Menu de consola simple para gestionar cuentas y ejecutar tareas diarias.
Toda la logica de negocio esta en app.py — este archivo solo maneja
la entrada/salida del usuario y el bucle del menu.

Uso:
    python main.py

Requisitos:
    pip install aiohttp sqlalchemy tenacity pycryptodome loguru
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime

from loguru import logger

from db import CookieStore, AccountStore
from HoyoWrapper import HoyoManager
from app import (
    AppContext,
    add_account,
    delete_account,
    list_accounts,
    restore_session,
    run_daily_tasks,
)

# ---------------------------------------------------------------------------
# Configuracion de loguru para la consola y archivo de log
# ---------------------------------------------------------------------------

logger.remove()

# Handler de consola — solo INFO y superior, formato limpio
logger.add(
    sys.stdout,
    colorize=True,
    format="<dim>{time:HH:mm:ss}</dim> <level>{level: <8}</level> {message}",
    level="INFO",
)

# Handler de archivo — un archivo por ejecucion con fecha y hora en el nombre.
# Formato: error_log_20260524_173045.txt
# Separado de test_output.txt para distinguir errores en vivo de errores en tests.
_log_filename = datetime.now().strftime("error_log_%Y%m%d_%H%M%S.txt")
logger.add(
    _log_filename,
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} | {message}",
    encoding="utf-8",
    backtrace=True,     # Traceback completo en excepciones
    diagnose=True,      # Variables locales en el traceback
)

DB_PATH = "./hoyo_auth.db"

# ---------------------------------------------------------------------------
# Helpers de consola
# ---------------------------------------------------------------------------

SEP     = "-" * 60
SEP_FAT = "=" * 60


def _print_header() -> None:
    print(f"\n{SEP_FAT}")
    print("  HoYoWrapper — Automatizacion de tareas HoYoLab")
    print(SEP_FAT)


def _print_menu() -> None:
    print(f"\n{SEP}")
    print("  MENU PRINCIPAL")
    print(SEP)
    print("  1. Agregar cuenta")
    print("  2. Listar cuentas")
    print("  3. Eliminar cuenta")
    print("  4. Completar tareas diarias")
    print("  0. Salir")
    print(SEP)


def _ask(prompt: str) -> str:
    """Input con prompt. Ctrl+C cancela la operacion actual."""
    try:
        return input(f"  {prompt}").strip()
    except KeyboardInterrupt:
        print("\n  Operacion cancelada.")
        return ""


def _ask_password(prompt: str = "Contrasena: ") -> str:
    """Input de contrasena. Usa getpass para ocultar los caracteres."""
    try:
        import getpass
        return getpass.getpass(f"  {prompt}")
    except (KeyboardInterrupt, Exception):
        print("\n  Operacion cancelada.")
        return ""


def _confirm(prompt: str) -> bool:
    """Pide confirmacion s/n."""
    resp = _ask(f"{prompt} [s/N]: ").lower()
    return resp in ("s", "si", "y", "yes")


# ---------------------------------------------------------------------------
# Acciones del menu
# ---------------------------------------------------------------------------

async def _action_add_account(ctx: AppContext) -> None:
    """Flujo de registro de una nueva cuenta."""
    print(f"\n{SEP}")
    print("  AGREGAR CUENTA")
    print(SEP)
    print("  El alias es un nombre legible que tu eliges (ej: 'Cuenta EU').")
    print("  El email y la contrasena se usan solo para hacer login.")
    print("  Nunca se guardan en disco — solo las cookies de sesion.\n")

    alias = _ask("Alias para esta cuenta: ")
    if not alias:
        return

    email = _ask("Email de HoYoLab / Miyoushe: ")
    if not email:
        return

    password = _ask_password("Contrasena: ")
    if not password:
        return

    print("\n  Region (deja en blanco para deteccion automatica):")
    print("    os = HoYoLab global")
    print("    cn = Miyoushe (China)")
    region_input = _ask("Region [os/cn/enter]: ").lower()
    region = region_input if region_input in ("os", "cn") else None

    print()
    logger.info("Iniciando login para '{}'...", alias)

    result = await add_account(ctx, alias, email, password, region=region)

    print()
    if result["success"]:
        logger.success("Cuenta '{}' registrada. Region: {}", alias, result["region"])
        if result["games"]:
            logger.info("Juegos detectados:")
            for g in result["games"]:
                logger.info("  - {}", g)
        else:
            logger.warning("No se detectaron juegos para esta cuenta.")
    else:
        logger.error("Error al registrar cuenta: {}", result["message"])


def _action_list_accounts(ctx: AppContext) -> None:
    """Muestra las cuentas registradas."""
    print(f"\n{SEP}")
    print("  CUENTAS REGISTRADAS")
    print(SEP)

    accounts = list_accounts(ctx)
    if not accounts:
        print("  No hay cuentas registradas.")
        print(f"  Usa la opcion 1 para agregar una cuenta.")
        return

    for i, acc in enumerate(accounts, 1):
        created = acc["created_at"].strftime("%d/%m/%Y") if acc.get("created_at") else "?"
        print(f"  {i}. {acc['alias']:<30} [{acc['region'].upper()}]  (registrada: {created})")


async def _action_delete_account(ctx: AppContext) -> None:
    """Flujo de eliminacion de una cuenta."""
    print(f"\n{SEP}")
    print("  ELIMINAR CUENTA")
    print(SEP)

    accounts = list_accounts(ctx)
    if not accounts:
        print("  No hay cuentas registradas.")
        return

    print("  Cuentas disponibles:\n")
    for i, acc in enumerate(accounts, 1):
        print(f"    {i}. {acc['alias']} [{acc['region'].upper()}]")

    print()
    alias = _ask("Alias de la cuenta a eliminar (o Enter para cancelar): ")
    if not alias:
        return

    if not any(a["alias"] == alias for a in accounts):
        logger.warning("No se encontro ninguna cuenta con el alias '{}'.", alias)
        return

    if not _confirm(f"Eliminar '{alias}'? Esta accion no se puede deshacer"):
        print("  Cancelado.")
        return

    result = delete_account(ctx, alias)
    print()
    if result["success"]:
        logger.success("Cuenta '{}' eliminada.", alias)
    else:
        logger.error("Error: {}", result["message"])


async def _action_run_daily(ctx: AppContext) -> None:
    """Ejecuta las tareas diarias en todas las cuentas."""
    print(f"\n{SEP_FAT}")
    print("  COMPLETAR TAREAS DIARIAS")
    print(SEP_FAT)

    accounts = list_accounts(ctx)
    if not accounts:
        print("\n  No hay cuentas registradas.")
        print("  Usa la opcion 1 para agregar una cuenta primero.")
        return

    print(f"\n  Cuentas a procesar: {len(accounts)}")
    for acc in accounts:
        print(f"    - {acc['alias']} [{acc['region'].upper()}]")

    print()
    if not _confirm("Iniciar tareas diarias"):
        print("  Cancelado.")
        return

    print()
    result = await run_daily_tasks(ctx)

    # Imprimir resumen final
    _print_daily_summary(result)


def _print_daily_summary(result: dict) -> None:
    """Imprime el resumen final de las tareas diarias."""
    accounts = result.get("accounts", [])
    if not accounts:
        return

    print(f"\n{SEP_FAT}")
    print("  RESUMEN FINAL")
    print(SEP_FAT)

    for acc_result in accounts:
        alias   = acc_result["alias"]
        region  = acc_result["region"].upper()
        success = acc_result["success"]

        if not success:
            print(f"\n  [ERROR] {alias} [{region}]")
            print(f"    {acc_result['message']}")
            continue

        tasks = acc_result.get("tasks", [])
        print(f"\n  {alias} [{region}]")

        if not tasks:
            print(f"    Sin juegos soportados vinculados a esta cuenta.")
            continue

        # Agrupar por juego
        games: dict[str, list] = {}
        for t in tasks:
            games.setdefault(t["game_name"], []).append(t)

        for game_name, game_tasks in games.items():
            print(f"\n    {game_name}")

            for t in game_tasks:
                icon = "OK" if t["success"] else "--"

                if t["type"] == "checkin":
                    print(f"      [{icon}] Check-in: {t['message']}")

                elif t["type"] == "mimo_task":
                    status = "Reclamada" if t["success"] else t["message"]
                    name   = t["detail"] or "Tarea"
                    print(f"      [{icon}] Mimo | {name}: {status}")

                elif t["type"] == "mimo_buy":
                    if t["success"]:
                        print(f"      [{icon}] Mimo compra: {t['message']}")
                        print(f"             {t['detail']}")
                    else:
                        print(f"      [--] Mimo compra: {t['message']}")

    # Totales globales
    total_ok  = sum(1 for a in accounts if a["success"])
    total_err = len(accounts) - total_ok
    print(f"\n{SEP}")
    print(f"  Cuentas procesadas: {len(accounts)} | OK: {total_ok} | Error: {total_err}")
    print(SEP)


# ---------------------------------------------------------------------------
# Bucle principal
# ---------------------------------------------------------------------------

async def run() -> None:
    """Bucle principal de la aplicacion."""
    ctx = AppContext(
        cookie_store  = CookieStore(DB_PATH),
        account_store = AccountStore(DB_PATH),
        manager       = HoyoManager(CookieStore(DB_PATH)),
    )

    _print_header()

    while True:
        _print_menu()
        choice = _ask("Opcion: ")

        if choice == "1":
            await _action_add_account(ctx)

        elif choice == "2":
            _action_list_accounts(ctx)

        elif choice == "3":
            await _action_delete_account(ctx)

        elif choice == "4":
            await _action_run_daily(ctx)

        elif choice == "0":
            print("\n  Hasta luego.\n")
            break

        else:
            print("  Opcion no valida. Elige entre 0 y 4.")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n\n  Interrumpido por el usuario.\n")
        sys.exit(0)
    except Exception:
        logger.exception(
            "Error no capturado. Consulta hoyo_wrapper.log para el traceback completo."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
