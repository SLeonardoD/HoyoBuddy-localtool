"""
app.py
------
Nucleo de la aplicacion HoYoWrapper.

Contiene toda la logica de negocio extraida de main.py para que sea
testeable de forma independiente. main.py solo orquesta el menu y llama
a las funciones de este modulo.

Responsabilidades:
  - Registro y eliminacion de cuentas.
  - Restaurar sesiones guardadas por account_hash (sin necesitar el email).
  - Ejecutar las tareas diarias (check-in, mimo, compra) por cuenta y juego.
  - Producir resultados estructurados que main.py imprime.

Dependencias inyectadas (no se crean aqui):
  - CookieStore   almacen de cookies
  - AccountStore  almacen de aliases
  - HoyoManager   wrapper de autenticacion y juegos
"""

from __future__ import annotations

import asyncio
import typing

from loguru import logger

from db.cookie_store import CookieStore
from db.account_store import AccountStore
from HoyoWrapper.session import HoyoSession, HoyoAPIError, LoginError
from HoyoWrapper.session_manager import HoyoManager, ActiveSession
from HoyoWrapper.game_model import random_delay

__all__ = [
    "AppContext",
    "add_account",
    "delete_account",
    "list_accounts",
    "restore_session",
    "run_daily_tasks",
    "TaskResult",
    "AccountResult",
    "DailyRunResult",
]

# URLs de canje de codigos por juego (para mostrar al usuario tras compra mimo)
REDEEM_URLS: dict[str, str] = {
    "hk4e_global":  "https://genshin.hoyoverse.com/en/gift",
    "hkrpg_global": "https://hsr.hoyoverse.com/gift",
    "nap_global":   "https://zenless.hoyoverse.com/redemption",
}


# ---------------------------------------------------------------------------
# Contexto de la aplicacion
# ---------------------------------------------------------------------------

class AppContext(typing.NamedTuple):
    """
    Dependencias de la aplicacion agrupadas para inyeccion limpia.
    Se crea una vez en main.py y se pasa a todas las funciones.
    """
    cookie_store:  CookieStore
    account_store: AccountStore
    manager:       HoyoManager


# ---------------------------------------------------------------------------
# Tipos de resultado
# ---------------------------------------------------------------------------

class TaskResult(typing.TypedDict):
    """Resultado de una tarea individual (check-in, tarea mimo, compra)."""
    type:        str    # "checkin" | "mimo_task" | "mimo_buy"
    game_name:   str
    success:     bool
    message:     str
    detail:      str    # informacion adicional (nombre de tarea, codigo, etc.)


class AccountResult(typing.TypedDict):
    """Resultado completo de procesar una cuenta."""
    alias:   str
    region:  str
    success: bool        # False si la sesion no pudo restaurarse
    message: str         # mensaje de error si success=False
    tasks:   list[TaskResult]


class DailyRunResult(typing.TypedDict):
    """Resultado de ejecutar las tareas diarias en todas las cuentas."""
    accounts: list[AccountResult]


# ---------------------------------------------------------------------------
# Gestion de cuentas
# ---------------------------------------------------------------------------

async def add_account(
    ctx: AppContext,
    alias: str,
    email: str,
    password: str,
    region: typing.Optional[typing.Literal["os", "cn"]] = None,
) -> dict:
    """
    Registra una cuenta nueva.

    Flujo:
      1. Verifica que el alias no exista ya.
      2. Hace login con el wrapper (obtiene cookies + account_hash).
      3. Guarda cookies en CookieStore y alias en AccountStore.

    Devuelve dict con:
        success      bool
        message      str
        alias        str
        region       str    (detectada automaticamente si no se indico)
        games        list   juegos detectados para esta cuenta
    """
    # Verificar alias antes de hacer login (evita trafico innecesario)
    if ctx.account_store.exists_alias(alias):
        return {
            "success": False,
            "message": f"Ya existe una cuenta con el alias '{alias}'. Elige otro nombre.",
            "alias": alias,
            "region": region or "",
            "games": [],
        }

    try:
        session = await ctx.manager.login(email, password, region=region)
        accounts = await session.session.get_game_accounts()
    except LoginError as e:
        logger.exception("Error de login al registrar cuenta '{}'.", alias)
        return {
            "success": False,
            "message": f"Error de login: {e}",
            "alias": alias,
            "region": region or "",
            "games": [],
        }
    except Exception as e:
        logger.exception("Error inesperado al registrar cuenta '{}'.", alias)
        return {
            "success": False,
            "message": f"Error inesperado: {e}",
            "alias": alias,
            "region": region or "",
            "games": [],
        }

    # Calcular account_hash igual que lo hace CookieStore
    from db.cookie_store import _hash_email
    account_hash = _hash_email(email)

    # Guardar alias en AccountStore
    try:
        ctx.account_store.add(alias, account_hash, session.region)
    except ValueError as e:
        # La cuenta puede ya estar registrada con otro alias
        return {
            "success": False,
            "message": str(e),
            "alias": alias,
            "region": session.region,
            "games": [],
        }

    game_names = [g.GAME_NAME for g in session.games]
    logger.info(
        "Cuenta '{}' registrada. Region: {}. Juegos: {}",
        alias, session.region, ", ".join(game_names) or "ninguno"
    )

    return {
        "success": True,
        "message": f"Cuenta '{alias}' registrada correctamente.",
        "alias":   alias,
        "region":  session.region,
        "games":   game_names,
    }


def delete_account(ctx: AppContext, alias: str) -> dict:
    """
    Elimina una cuenta por alias.

    Elimina el alias de AccountStore pero NO las cookies de CookieStore
    — si el usuario vuelve a registrar la misma cuenta, las cookies siguen
    validas y se reutilizaran sin necesidad de captcha.

    Devuelve dict con success y message.
    """
    deleted = ctx.account_store.delete_by_alias(alias)
    if deleted:
        logger.info("Cuenta '{}' eliminada.", alias)
        return {"success": True, "message": f"Cuenta '{alias}' eliminada."}
    return {
        "success": False,
        "message": f"No se encontro ninguna cuenta con el alias '{alias}'.",
    }


def list_accounts(ctx: AppContext) -> list[dict]:
    """
    Devuelve la lista de cuentas registradas.
    Cada elemento: {alias, region, created_at}
    """
    return ctx.account_store.list_all()


# ---------------------------------------------------------------------------
# Restauracion de sesion
# ---------------------------------------------------------------------------

async def restore_session(
    ctx: AppContext,
    account: dict,
) -> typing.Optional[ActiveSession]:
    """
    Restaura una sesion guardada a partir de un registro de AccountStore.

    No necesita email — usa account_hash para recuperar las cookies de
    CookieStore directamente. Si las cookies expiraron devuelve None.

    Parametros:
        account     Dict de AccountStore.list_all(): {alias, account_hash, region}
    """
    account_hash = account["account_hash"]
    region       = account["region"]

    cookies, device_id = ctx.cookie_store.get_by_hash(account_hash, region)
    if not cookies:
        logger.warning(
            "No se encontraron cookies para la cuenta '{}'. "
            "Vuelve a registrarla con 'Agregar cuenta'.",
            account["alias"]
        )
        return None

    session = HoyoSession(cookies, region, device_id)

    # Validar que las cookies siguen siendo validas
    try:
        accounts = await session.get_game_accounts()
    except (HoyoAPIError, Exception):
        logger.warning(
            "Las cookies de '{}' han expirado. "
            "Vuelve a registrarla con 'Agregar cuenta'.",
            account["alias"]
        )
        return None

    # Construir ActiveSession con los juegos detectados
    from HoyoWrapper.game_model import create_game
    _SUPPORTED = {
        "hk4e_global", "hk4e_cn",
        "hkrpg_global", "hkrpg_cn",
        "nap_global", "nap_cn",
        "bh3_global", "bh3_cn",
    }
    supported = [a for a in accounts if a.get("game_biz", "") in _SUPPORTED]
    return ActiveSession(session, supported)


# ---------------------------------------------------------------------------
# Tareas diarias
# ---------------------------------------------------------------------------

async def run_daily_tasks(ctx: AppContext) -> DailyRunResult:
    """
    Ejecuta las tareas diarias en todas las cuentas registradas.

    Por cada cuenta:
      1. Restaurar sesion (si falla, se reporta y se continua con la siguiente).
      2. Para cada juego:
         a. Check-in diario.
         b. Mimo: reclamar recompensas de tareas completadas (status=1).
         c. Mimo: intentar compra de moneda premium.
      3. Delay aleatorio entre cuentas (anti-deteccion).

    Devuelve DailyRunResult con los resultados de todas las cuentas.
    """
    accounts = ctx.account_store.list_all()
    if not accounts:
        logger.info("No hay cuentas registradas. Usa 'Agregar cuenta' primero.")
        return {"accounts": []}

    results: list[AccountResult] = []

    for i, account in enumerate(accounts):
        # Delay entre cuentas (no antes de la primera)
        if i > 0:
            await random_delay(2.0, 5.0)

        alias = account["alias"]
        logger.info("=" * 60)
        logger.info("Procesando cuenta: {}", alias)
        logger.info("=" * 60)

        # Restaurar sesion
        active = await restore_session(ctx, account)
        if active is None:
            results.append({
                "alias":   alias,
                "region":  account["region"],
                "success": False,
                "message": (
                    "Sesion expirada o no encontrada. "
                    "Vuelve a registrar la cuenta con 'Agregar cuenta'."
                ),
                "tasks": [],
            })
            continue

        # Procesar juegos
        if not active.games:
            logger.warning(
                "  La cuenta '{}' no tiene juegos soportados vinculados.", alias
            )
            results.append({
                "alias":   alias,
                "region":  account["region"],
                "success": True,
                "message": "Sin juegos soportados vinculados a esta cuenta.",
                "tasks":   [],
            })
            continue

        task_results = await _process_account_games(active, alias)

        results.append({
            "alias":   alias,
            "region":  account["region"],
            "success": True,
            "message": "OK",
            "tasks":   task_results,
        })

    return {"accounts": results}


async def _process_account_games(
    active: ActiveSession,
    alias: str,
) -> list[TaskResult]:
    """
    Ejecuta check-in, mimo tasks y compra de moneda para todos los juegos
    de una sesion activa.
    """
    tasks: list[TaskResult] = []

    for j, game in enumerate(active.games):
        if j > 0:
            await random_delay(1.0, 3.0)

        logger.info("  Juego: {}", game.GAME_NAME)

        # ── Check-in diario ──────────────────────────────────────────
        try:
            result = await game.claim_daily_reward()
            if result["already_done"]:
                msg = f"Ya realizado hoy (dia {result['total_sign_day']} del mes)"
            else:
                msg = f"Realizado (dia {result['total_sign_day']} del mes)"

            logger.success("    [Check-in] {}", msg)
            tasks.append({
                "type":      "checkin",
                "game_name": game.GAME_NAME,
                "success":   True,
                "message":   msg,
                "detail":    "",
            })
        except HoyoAPIError as e:
            logger.error("    [Check-in] Error: {}", e)
            tasks.append({
                "type":      "checkin",
                "game_name": game.GAME_NAME,
                "success":   False,
                "message":   str(e),
                "detail":    "",
            })

        # ── Mimo: tareas ─────────────────────────────────────────────
        mimo_tasks = await _process_mimo_tasks(game)
        tasks.extend(mimo_tasks)

        # ── Mimo: compra de moneda ───────────────────────────────────
        mimo_buys = await _process_mimo_buys(game)
        tasks.extend(mimo_buys)

    return tasks


async def _process_mimo_tasks(game) -> list[TaskResult]:
    """Reclama recompensas de tareas mimo completadas (status=1)."""
    results: list[TaskResult] = []

    try:
        tasks = await game.get_mimo_tasks()
    except NotImplementedError:
        return []  # Honkai o CN: sin mimo
    except HoyoAPIError as e:
        logger.warning("    [Mimo tasks] Error al obtener tareas: {}", e)
        return [{
            "type":      "mimo_task",
            "game_name": game.GAME_NAME,
            "success":   False,
            "message":   f"Error al obtener tareas: {e}",
            "detail":    "",
        }]

    # status: 0=pendiente, 1=completada(sin reclamar), 2=reclamada
    completed   = [t for t in tasks if t.get("status") == 1]
    pending     = [t for t in tasks if t.get("status") == 0]
    already     = [t for t in tasks if t.get("status") == 2]

    logger.info(
        "    [Mimo tasks] Total: {} | Reclamables: {} | Pendientes: {} | Ya reclamadas: {}",
        len(tasks), len(completed), len(pending), len(already)
    )

    # Reclamar las completadas
    for task in completed:
        task_id   = task.get("task_id") or task.get("id")
        task_name = task.get("name") or task.get("title") or f"Tarea #{task_id}"
        await asyncio.sleep(0.5)  # pequeno delay entre claims

        try:
            await game.claim_mimo_task_reward(task_id)
            logger.success("    [Mimo] Reclamada: {}", task_name)
            results.append({
                "type":      "mimo_task",
                "game_name": game.GAME_NAME,
                "success":   True,
                "message":   "Recompensa reclamada",
                "detail":    task_name,
            })
        except HoyoAPIError as e:
            logger.warning("    [Mimo] Error al reclamar '{}': {}", task_name, e)
            results.append({
                "type":      "mimo_task",
                "game_name": game.GAME_NAME,
                "success":   False,
                "message":   str(e),
                "detail":    task_name,
            })

    # Registrar las pendientes (informacion para el usuario)
    for task in pending:
        task_name = task.get("name") or task.get("title") or f"Tarea #{task.get('task_id')}"
        logger.info("    [Mimo] Pendiente (completar en juego): {}", task_name)
        results.append({
            "type":      "mimo_task",
            "game_name": game.GAME_NAME,
            "success":   False,
            "message":   "Pendiente — completa esta tarea en el juego",
            "detail":    task_name,
        })

    return results


async def _process_mimo_buys(game) -> list[TaskResult]:
    """Intenta comprar moneda premium con puntos mimo."""
    try:
        purchases = await game.buy_mimo_currency()
    except NotImplementedError:
        return []  # Honkai o CN: sin mimo
    except HoyoAPIError as e:
        logger.warning("    [Mimo compra] Error: {}", e)
        return [{
            "type":      "mimo_buy",
            "game_name": game.GAME_NAME,
            "success":   False,
            "message":   str(e),
            "detail":    "",
        }]

    results: list[TaskResult] = []

    if not purchases:
        logger.info("    [Mimo compra] Sin monedas disponibles para comprar.")
        return []

    redeem_url = REDEEM_URLS.get(game.game_biz, "https://hoyolab.com")
    for p in purchases:
        code      = p.get("code", "")
        item_name = p.get("item_name", "Moneda")
        logger.success(
            "    [Mimo compra] {} obtenida — Codigo: {} | Canjear en: {}",
            item_name, code, redeem_url
        )
        results.append({
            "type":      "mimo_buy",
            "game_name": game.GAME_NAME,
            "success":   True,
            "message":   f"Compra exitosa: {item_name}",
            "detail":    f"Codigo: {code} | Canjear en: {redeem_url}",
        })

    return results
