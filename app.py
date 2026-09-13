"""
app.py
------
Nucleo de la aplicacion HoYoWrapper.

Contiene toda la logica de negocio extraida de main.py para que sea
testeable de forma independiente. main.py solo orquesta el menu y llama
a las funciones de este modulo.

Responsabilidades:
  - Registro, relogin y eliminacion de cuentas.
  - Restaurar sesiones guardadas.
  - Ejecutar las tareas diarias (check-in, mimo, compra) por cuenta y juego.
  - Sistema de codigos de regalo: agregar codigos a la DB y canjear
    automaticamente los pendientes en todas las cuentas que apliquen,
    ademas del canje manual puntual.
  - Producir resultados estructurados que main.py imprime.

Dependencias inyectadas (no se crean aqui):
  - AccountStore   cuentas (cookies + alias unificados)
  - GiftCodeStore  codigos de regalo + tracking de canjes
  - HoyoManager    wrapper de autenticacion y juegos
"""

from __future__ import annotations

import asyncio
import random
import typing
from datetime import datetime, timezone

from loguru import logger

import config
from db.store import AccountStore, GiftCodeStore
from HoyoWrapper.session import HoyoSession, HoyoAPIError, LoginError
from HoyoWrapper.session_manager import HoyoManager, ActiveSession
from HoyoWrapper.game_model import random_delay

__all__ = [
    "AppContext",
    "add_account",
    "relogin_account",
    "delete_account",
    "list_accounts",
    "restore_session",
    "run_daily_tasks",
    "redeem_code_for_all",
    "add_gift_code",
    "redeem_pending_codes",
    "REDEEMABLE_GAMES",
    "TaskResult",
    "AccountResult",
    "DailyRunResult",
    "RedeemResult",
]

# URLs de canje de codigos por juego (para mostrar al usuario tras compra
# mimo). Definidas una sola vez en config.py.
REDEEM_URLS: dict[str, str] = config.REDEEM_WEBPAGE_URLS


# ---------------------------------------------------------------------------
# Contexto de la aplicacion
# ---------------------------------------------------------------------------

class AppContext(typing.NamedTuple):
    """
    Dependencias de la aplicacion agrupadas para inyeccion limpia.
    Se crea una vez en main.py y se pasa a todas las funciones.
    """
    account_store: AccountStore
    code_store:    GiftCodeStore
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
      2. Hace login con el wrapper (AuthManager persiste las cookies
         en AccountStore por su cuenta, indexado por email+region, sin
         necesidad de conocer el alias).
      3. Asigna el alias a esa fila con set_alias().

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

    # Asignar el alias a la fila que AuthManager acaba de crear/actualizar
    try:
        ctx.account_store.set_alias(email, session.region, alias)
    except ValueError as e:
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


async def relogin_account(ctx: AppContext, alias: str, password: str) -> dict:
    """
    Reingresa la contrasena de una cuenta ya registrada cuyas cookies
    expiraron, sin pedir el email de nuevo (ya lo tenemos guardado en
    texto plano — para eso se dejo de hashear).

    Flujo:
      1. Busca la cuenta por alias para obtener su email/region.
      2. Llama a manager.login(email, password, region=region) — esto
         hace un login por credenciales normal (con captcha si hace
         falta) y AuthManager persiste las cookies nuevas en la MISMA
         fila (el alias no se toca, set() nunca lo modifica).

    Devuelve el mismo shape que add_account().
    """
    account = ctx.account_store.get_by_alias(alias)
    if account is None:
        return {
            "success": False,
            "message": f"No existe ninguna cuenta con el alias '{alias}'.",
            "alias": alias, "region": "", "games": [],
        }

    email  = account["email"]
    region = account["region"]

    try:
        session = await ctx.manager.login(email, password, region=region)
    except LoginError as e:
        logger.exception("Error de relogin para '{}'.", alias)
        return {
            "success": False,
            "message": f"Error de login: {e}",
            "alias": alias, "region": region, "games": [],
        }
    except Exception as e:
        logger.exception("Error inesperado en relogin para '{}'.", alias)
        return {
            "success": False,
            "message": f"Error inesperado: {e}",
            "alias": alias, "region": region, "games": [],
        }

    game_names = [g.GAME_NAME for g in session.games]
    logger.info("Cuenta '{}' reautenticada correctamente.", alias)
    return {
        "success": True,
        "message": f"Cuenta '{alias}' reautenticada correctamente.",
        "alias": alias, "region": session.region, "games": game_names,
    }


def delete_account(ctx: AppContext, alias: str) -> dict:
    """
    Elimina una cuenta por alias (soft-delete).

    Le quita el alias a la fila en AccountStore pero preserva las
    cookies — si el usuario vuelve a registrar la misma cuenta, se
    reutilizan sin necesidad de login+captcha de nuevo. Ver
    AccountStore.delete_by_alias() para el detalle.

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

    Ya no hace falta una segunda consulta a la DB: account_store.list_all()
    (o get_by_alias()) ya trae cookies+device_id en el mismo dict, porque
    ahora es una sola tabla. Si las cookies expiraron devuelve None.

    Parametros:
        account     Dict de AccountStore.list_all():
                    {alias, email, region, cookies, device_id, ...}
    """
    region  = account["region"]
    cookies = account["cookies"]

    if not cookies:
        logger.warning(
            "No hay cookies guardadas para la cuenta '{}'. "
            "Vuelve a registrarla con 'Agregar cuenta'.",
            account["alias"]
        )
        return None

    session = HoyoSession(cookies, region, account["device_id"])

    # Validar que las cookies siguen siendo validas
    try:
        accounts = await session.get_game_accounts()
    except (HoyoAPIError, Exception):
        logger.warning(
            "Las cookies de '{}' han expirado. "
            "Usa 'Reingresar contrasena' para renovarlas sin perder el alias.",
            account["alias"]
        )
        return None

    # Construir ActiveSession con los juegos detectados
    supported = [
        a for a in accounts
        if a.get("game_biz", "") in config.SUPPORTED_GAME_BIZ
    ]
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
            await random_delay(*config.DELAY_BETWEEN_ACCOUNTS)

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
                    "Usa 'Reingresar contrasena' para renovarla."
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

        # ANTI-BANEO: pausa "recien cargo la pagina" antes de la primera
        # accion sobre esta cuenta. A diferencia del delay entre cuentas
        # (que se salta para la primera), este se aplica siempre.
        await random_delay(*config.DELAY_AFTER_SESSION_RESTORE)

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
            await random_delay(*config.DELAY_BETWEEN_GAMES)

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
        # ANTI-BANEO: antes esto arrancaba apenas terminaba el check-in,
        # sin ninguna espera — como si el navegador saltara de pagina
        # instantaneamente. Simula el tiempo de navegar a la seccion mimo.
        await random_delay(*config.DELAY_CHECKIN_TO_MIMO)
        mimo_tasks = await _process_mimo_tasks(game)
        tasks.extend(mimo_tasks)

        # ── Mimo: compra de moneda ───────────────────────────────────
        # ANTI-BANEO: idem, transicion de la pestaña de tareas a la de
        # tienda dentro del mismo mimo — antes tambien sin espera.
        await random_delay(*config.DELAY_MIMO_TASKS_TO_SHOP)
        mimo_buys = await _process_mimo_buys(game)
        tasks.extend(mimo_buys)

    return tasks


async def _process_mimo_tasks(game) -> list[TaskResult]:
    """
    Procesa tareas mimo completo:
      1. Tareas completables via API (VISIT, VIEW_TOPIC, TRAILER, etc.)
         -> finish_mimo_task() primero, luego claim.
      2. Tareas ya completadas (status=1/FINISHED)
         -> claim_mimo_task_reward() directamente.
      3. Tareas pendientes que requieren accion en juego
         -> reportar al usuario.
      4. Tareas ya reclamadas (status=3/CLAIMED)
         -> ignorar silenciosamente.

    Status segun genshin.py/models/hoyolab/mimo.py:
        1 = FINISHED (completada, sin reclamar)
        2 = ONGOING  (en progreso / pendiente)
        3 = CLAIMED  (ya reclamada)
    """
    from HoyoWrapper.game_model import MIMO_FINISHABLE_TASK_TYPES

    results: list[TaskResult] = []

    try:
        tasks = await game.get_mimo_tasks()
    except NotImplementedError:
        return []
    except HoyoAPIError as e:
        logger.warning(
            "[APP] Error al obtener tareas mimo | game={} error={}", game.GAME_NAME, e
        )
        return [{
            "type":      "mimo_task",
            "game_name": game.GAME_NAME,
            "success":   False,
            "message":   f"Error al obtener tareas: {e}",
            "detail":    "",
        }]

    # Clasificar por status
    finished = [t for t in tasks if t.get("status") == 1]
    ongoing  = [t for t in tasks if t.get("status") == 2]
    claimed  = [t for t in tasks if t.get("status") == 3]

    # Clasificar ongoing por tipo
    finishable_via_api = [
        t for t in ongoing
        if (t.get("task_type") or t.get("type")) in MIMO_FINISHABLE_TASK_TYPES
    ]
    requires_game = [
        t for t in ongoing
        if (t.get("task_type") or t.get("type")) not in MIMO_FINISHABLE_TASK_TYPES
    ]

    logger.info(
        "[APP] Mimo tareas | game={} total={} completadas={} "
        "finishable_api={} requieren_juego={} ya_reclamadas={}",
        game.GAME_NAME, len(tasks), len(finished),
        len(finishable_via_api), len(requires_game), len(claimed)
    )

    # Paso 1: completar via API las que se pueden
    for task in finishable_via_api:
        task_id   = task.get("task_id") or task.get("id")
        task_name = task.get("task_name") or task.get("name") or f"Tarea #{task_id}"
        task_type = task.get("task_type") or task.get("type")

        await random_delay(*config.DELAY_BETWEEN_MIMO_TASKS)

        try:
            await game.finish_mimo_task(task_id)
            logger.info(
                "[APP] Tarea completada via API | game={} task={!r} type={}",
                game.GAME_NAME, task_name, task_type
            )
            # Añadir a finished para reclamar en el paso 2
            finished.append(task)
        except HoyoAPIError as e:
            logger.warning(
                "[APP] No se pudo completar tarea | game={} task={!r} "
                "retcode={} message={!r}",
                game.GAME_NAME, task_name, e.retcode, e.message
            )
            results.append({
                "type":      "mimo_task",
                "game_name": game.GAME_NAME,
                "success":   False,
                "message":   f"No se pudo completar: {e.message}",
                "detail":    task_name,
            })

    # Paso 2: reclamar todas las completadas
    for task in finished:
        task_id   = task.get("task_id") or task.get("id")
        task_name = task.get("task_name") or task.get("name") or f"Tarea #{task_id}"

        await random_delay(*config.DELAY_BETWEEN_MIMO_TASKS)

        try:
            await game.claim_mimo_task_reward(task_id)
            logger.info(
                "[APP] Recompensa reclamada | game={} task={!r}",
                game.GAME_NAME, task_name
            )
            results.append({
                "type":      "mimo_task",
                "game_name": game.GAME_NAME,
                "success":   True,
                "message":   "Recompensa reclamada",
                "detail":    task_name,
            })
        except HoyoAPIError as e:
            logger.warning(
                "[APP] Error al reclamar | game={} task={!r} "
                "retcode={} message={!r}",
                game.GAME_NAME, task_name, e.retcode, e.message
            )
            results.append({
                "type":      "mimo_task",
                "game_name": game.GAME_NAME,
                "success":   False,
                "message":   str(e),
                "detail":    task_name,
            })

    # Paso 3: reportar pendientes que requieren accion en juego
    for task in requires_game:
        task_name = task.get("task_name") or task.get("name") or f"Tarea #{task.get('task_id')}"
        task_type = task.get("task_type") or task.get("type")
        logger.info(
            "[APP] Tarea pendiente (requiere juego) | game={} task={!r} type={}",
            game.GAME_NAME, task_name, task_type
        )
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


# ---------------------------------------------------------------------------
# Juegos que soportan canje de codigos (para el menu de seleccion)
# Solo OS — CN no soportado via API.
# Definido en config.py, derivado automaticamente de CODE_URLS +
# GAME_BIZ_MAP para que nunca quede desincronizado con los juegos que
# realmente tienen endpoint de redeem configurado.
# ---------------------------------------------------------------------------

REDEEMABLE_GAMES: list[dict] = config.REDEEMABLE_GAMES


class RedeemResult(typing.TypedDict):
    """Resultado del canje de un codigo en una cuenta."""
    alias:     str
    game_name: str
    uid:       str
    success:   bool
    message:   str


def _classify_redeem_error(e: HoyoAPIError) -> tuple[str, str]:
    """
    Clasifica un HoyoAPIError de redeem_code() en (scope, label).

    scope:
        'global'    El codigo murio para TODAS las cuentas.
        'account'   Resultado definitivo pero solo para esta cuenta.
        'transient' Temporal — no guardar nada, reintentar despues.

    label (solo relevante si scope == 'account'):
        'claimed'    Ya lo habia reclamado esta cuenta.
        'ineligible' No aplica a esta cuenta (region/nivel).

    Ver config.REDEEM_*_RETCODES para las listas y su fuente
    (genshin.py errors.py).
    """
    if e.retcode in config.REDEEM_GLOBAL_DEAD_RETCODES:
        return "global", ""
    if e.retcode in config.REDEEM_ACCOUNT_CLAIMED_RETCODES:
        return "account", "claimed"
    if e.retcode in config.REDEEM_ACCOUNT_INELIGIBLE_RETCODES:
        return "account", "ineligible"
    # Cooldown, "aun no activado", o cualquier retcode que no reconocemos
    # todavia: mejor reintentar de mas que marcar como muerto algo que
    # en realidad seguia valido.
    return "transient", ""


async def redeem_code_for_all(
    ctx: AppContext,
    game_biz: str,
    code: str,
    *,
    lang: str = "en",
) -> list[RedeemResult]:
    """
    Canje MANUAL de un codigo puntual en todas las cuentas que tengan
    el juego indicado (no requiere haberlo agregado antes a la DB).

    Ademas de canjearlo, esta funcion:
      - Registra el codigo en GiftCodeStore (si no existia) para que
        CUALQUIER otra cuenta lo aproveche automaticamente la proxima
        vez que se corra "Canjear codigos pendientes" — asi el canje
        manual tambien alimenta el sistema automatico.
      - Clasifica cada resultado con _classify_redeem_error() y lo
        persiste igual que lo haria el canje automatico, para que no
        se reintente de mas ni de menos la proxima vez.

    Flujo por cuenta:
      1. Restaurar sesion desde cookies guardadas.
      2. Buscar el juego objetivo entre los juegos activos de la cuenta.
      3. Si lo tiene y esta habilitado, intentar redeem_code().
      4. Delay aleatorio entre cuentas (anti-deteccion).
         HoYoverse impone un cooldown entre redeems — usamos 6-10s.

    Solo opera en cuentas de region OS. CN no soporta redeem via API.

    Devuelve lista de RedeemResult, uno por cuenta que tenga el juego.
    """
    accounts = ctx.account_store.list_all()
    if not accounts:
        logger.info("[REDEEM] No hay cuentas registradas.")
        return []

    os_accounts = [a for a in accounts if a["region"] == "os"]
    if not os_accounts:
        logger.warning(
            "[REDEEM] Ninguna cuenta OS registrada. "
            "El canje de codigos solo esta disponible en region OS."
        )
        return []

    code_id = ctx.code_store.get_or_create_code_id(game_biz, code)

    results: list[RedeemResult] = []
    found_any = False

    for i, account in enumerate(os_accounts):
        if i > 0:
            # Cooldown entre redeems — HoYoverse lo requiere
            await random_delay(*config.DELAY_BETWEEN_REDEEMS)

        alias = account["alias"]
        logger.info("[REDEEM] Revisando cuenta: {}", alias)

        active = await restore_session(ctx, account)
        if active is None:
            logger.warning(
                "[REDEEM] Sesion expirada para '{}'. "
                "Usa 'Reingresar contrasena' para renovarla.", alias
            )
            continue

        target_game = active.get_game(game_biz)
        if target_game is None:
            logger.debug(
                "[REDEEM] '{}' no tiene {} — omitiendo.", alias, game_biz
            )
            continue

        # ANTI-BANEO: pausa entre restaurar la sesion (una request) y el
        # intento de canje (otra request) — antes salian pegadas.
        await random_delay(*config.DELAY_AFTER_SESSION_RESTORE)

        found_any = True
        logger.info(
            "[REDEEM] '{}' tiene {} (uid={}). Intentando canje del codigo '{}'...",
            alias, target_game.GAME_NAME, target_game.uid, code
        )

        try:
            await target_game.redeem_code(code, lang=lang)
            logger.success(
                "[REDEEM] Canjeado en '{}' ({} uid={}).",
                alias, target_game.GAME_NAME, target_game.uid
            )
            ctx.code_store.record_redemption(
                code_id, alias, "success", "Canjeado correctamente."
            )
            results.append({
                "alias":     alias,
                "game_name": target_game.GAME_NAME,
                "uid":       target_game.uid,
                "success":   True,
                "message":   "Codigo canjeado correctamente.",
            })
        except NotImplementedError as e:
            logger.warning(
                "[REDEEM] {} no soporta redeem via API: {}", target_game.GAME_NAME, e
            )
            results.append({
                "alias":     alias,
                "game_name": target_game.GAME_NAME,
                "uid":       target_game.uid,
                "success":   False,
                "message":   str(e),
            })
        except HoyoAPIError as e:
            logger.error(
                "[REDEEM] Error en '{}' ({} uid={}) — retcode={} message={!r}",
                alias, target_game.GAME_NAME, target_game.uid,
                e.retcode, e.message
            )
            scope, label = _classify_redeem_error(e)
            msg = f"[{e.retcode}] {e.message}"
            if scope == "global":
                ctx.code_store.mark_dead(code_id, msg)
                logger.warning(
                    "[REDEEM] Codigo '{}' marcado como muerto globalmente: {}",
                    code, msg
                )
            elif scope == "account":
                ctx.code_store.record_redemption(code_id, alias, label, msg)
            # scope == "transient": no se guarda nada, se reintenta despues
            results.append({
                "alias":     alias,
                "game_name": target_game.GAME_NAME,
                "uid":       target_game.uid,
                "success":   False,
                "message":   msg,
            })

    if not found_any:
        logger.info(
            "[REDEEM] Ninguna cuenta registrada tiene el juego {}.", game_biz
        )

    return results


# ---------------------------------------------------------------------------
# Codigos de regalo — sistema automatico
# ---------------------------------------------------------------------------

async def add_gift_code(
    ctx: AppContext,
    game_biz: str,
    code: str,
    expires_at: typing.Optional[datetime] = None,
) -> dict:
    """
    Agrega un codigo a la DB para que se canjee automaticamente en
    todas las cuentas OS que tengan ese juego (via redeem_pending_codes).

    Devuelve dict con success y message.
    """
    if game_biz not in config.CODE_URLS:
        return {
            "success": False,
            "message": f"'{game_biz}' no soporta canje de codigos via API.",
        }

    added = ctx.code_store.add_code(game_biz, code, expires_at=expires_at)
    if added:
        return {
            "success": True,
            "message": f"Codigo '{code.strip().upper()}' agregado para {config.GAME_BIZ_MAP.get(game_biz, game_biz)}.",
        }
    return {
        "success": True,
        "message": f"El codigo '{code.strip().upper()}' ya estaba registrado — no se duplico.",
    }


async def redeem_pending_codes(ctx: AppContext) -> list[RedeemResult]:
    """
    Canje AUTOMATICO: busca en la DB los codigos activos y no expirados
    para cada juego de cada cuenta OS, e intenta canjear los que esa
    cuenta puntual todavia no haya intentado (no tiene fila en
    code_redemptions).

    A diferencia de redeem_code_for_all(), esto no pide un codigo
    especifico — recorre TODOS los codigos pendientes de TODAS las
    cuentas en una sola pasada. Pensado para correr junto (o despues)
    de las tareas diarias.

    Devuelve lista de RedeemResult (uno por intento realizado).
    """
    accounts = ctx.account_store.list_all()
    os_accounts = [a for a in accounts if a["region"] == "os"]
    if not os_accounts:
        logger.info("[REDEEM-AUTO] Ninguna cuenta OS registrada.")
        return []

    results: list[RedeemResult] = []
    first_attempt = True

    for account in os_accounts:
        alias = account["alias"]
        active = await restore_session(ctx, account)
        if active is None:
            logger.warning(
                "[REDEEM-AUTO] Sesion expirada para '{}'. "
                "Usa 'Reingresar contrasena' para renovarla.", alias
            )
            continue

        # ANTI-BANEO: pausa entre restaurar la sesion y empezar a mirar
        # codigos pendientes para esta cuenta — antes salian pegadas.
        await random_delay(*config.DELAY_AFTER_SESSION_RESTORE)

        for game in active.games:
            pending = ctx.code_store.get_pending_for_account(alias, game.game_biz)
            if not pending:
                continue

            for code_row in pending:
                if not first_attempt:
                    await random_delay(*config.DELAY_BETWEEN_REDEEMS)
                first_attempt = False

                code = code_row["code"]
                logger.info(
                    "[REDEEM-AUTO] '{}' | {} | codigo pendiente: {}",
                    alias, game.GAME_NAME, code
                )
                try:
                    await game.redeem_code(code)
                    logger.success(
                        "[REDEEM-AUTO] Canjeado: '{}' | {} | {}",
                        alias, game.GAME_NAME, code
                    )
                    ctx.code_store.record_redemption(
                        code_row["id"], alias, "success", "Canjeado correctamente."
                    )
                    results.append({
                        "alias": alias, "game_name": game.GAME_NAME,
                        "uid": game.uid, "success": True,
                        "message": f"Canjeado: {code}",
                    })
                except NotImplementedError:
                    continue
                except HoyoAPIError as e:
                    scope, label = _classify_redeem_error(e)
                    msg = f"[{e.retcode}] {e.message}"
                    logger.warning(
                        "[REDEEM-AUTO] '{}' | {} | {} -> {} ({})",
                        alias, game.GAME_NAME, code, msg, scope
                    )
                    if scope == "global":
                        ctx.code_store.mark_dead(code_row["id"], msg)
                    elif scope == "account":
                        ctx.code_store.record_redemption(
                            code_row["id"], alias, label, msg
                        )
                    # transient: no se guarda nada, se reintenta despues
                    results.append({
                        "alias": alias, "game_name": game.GAME_NAME,
                        "uid": game.uid, "success": False,
                        "message": f"{code}: {msg}",
                    })

    return results
