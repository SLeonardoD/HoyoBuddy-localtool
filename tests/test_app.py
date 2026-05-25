"""
tests/test_app.py
-----------------
Tests de app.py — logica de negocio de la aplicacion.

Todas las dependencias externas (HoyoManager, HoyoSession, juegos)
se mockean. No se hace ningun request real a HoYoverse.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app import (
    AppContext,
    add_account,
    delete_account,
    list_accounts,
    restore_session,
    run_daily_tasks,
    _process_mimo_tasks,
    _process_mimo_buys,
)
from db.cookie_store import CookieStore
from db.account_store import AccountStore
from HoyoWrapper.session import HoyoSession, HoyoAPIError, LoginError
from HoyoWrapper.session_manager import ActiveSession
from tests.conftest import TEST_DEVICE_ID, TEST_COOKIES_OS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

HASH_1 = "a" * 64
ALIAS_1 = "Cuenta Principal"
REGION_1 = "os"


@pytest.fixture
def cookie_store() -> CookieStore:
    return CookieStore(":memory:")


@pytest.fixture
def account_store() -> AccountStore:
    return AccountStore(":memory:")


@pytest.fixture
def mock_manager() -> MagicMock:
    """HoyoManager mockeado con login() que devuelve ActiveSession valida."""
    manager = MagicMock()
    session = HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)

    mock_game = MagicMock()
    mock_game.GAME_NAME = "Genshin Impact"
    mock_game.game_biz  = "hk4e_global"
    mock_game.uid       = "700000001"

    mock_active = MagicMock(spec=ActiveSession)
    mock_active.session = session
    mock_active.region  = "os"
    mock_active.games   = [mock_game]

    manager.login = AsyncMock(return_value=mock_active)
    return manager


@pytest.fixture
def ctx(cookie_store, account_store, mock_manager) -> AppContext:
    return AppContext(
        cookie_store=cookie_store,
        account_store=account_store,
        manager=mock_manager,
    )


@pytest.fixture
def ctx_with_account(ctx) -> AppContext:
    """AppContext con una cuenta ya registrada."""
    ctx.account_store.add(ALIAS_1, HASH_1, REGION_1)
    ctx.cookie_store.set(
        "test@example.com", REGION_1, TEST_COOKIES_OS, TEST_DEVICE_ID
    )
    return ctx


# ---------------------------------------------------------------------------
# Tests de add_account
# ---------------------------------------------------------------------------

class TestAddAccount:

    @pytest.mark.asyncio
    async def test_success(self, ctx: AppContext) -> None:
        """add_account() exitoso registra alias y devuelve success=True."""
        ctx.manager.login.return_value.session.get_game_accounts = AsyncMock(
            return_value=[{
                "game_biz": "hk4e_global", "game_uid": "700000001",
                "nickname": "Traveler", "level": 60,
                "region": "os_asia", "region_name": "Asia",
            }]
        )

        with patch("db.cookie_store._hash_email", return_value=HASH_1):
            result = await add_account(ctx, ALIAS_1, "test@example.com", "pass123")

        assert result["success"] is True, (
            f"add_account() deberia ser exitoso.\n  Resultado: {result}"
        )
        assert result["alias"] == ALIAS_1
        assert ctx.account_store.exists_alias(ALIAS_1), (
            "El alias deberia haberse guardado en AccountStore."
        )

    @pytest.mark.asyncio
    async def test_duplicate_alias_fails(self, ctx: AppContext) -> None:
        """add_account() falla si el alias ya existe, sin hacer login."""
        ctx.account_store.add(ALIAS_1, HASH_1, "os")

        result = await add_account(ctx, ALIAS_1, "other@example.com", "pass")

        assert result["success"] is False, (
            "add_account() deberia fallar con alias duplicado."
        )
        assert "alias" in result["message"].lower(), (
            f"El mensaje deberia mencionar el alias.\n  Mensaje: {result['message']!r}"
        )
        # No deberia haber llamado a login
        ctx.manager.login.assert_not_called()

    @pytest.mark.asyncio
    async def test_login_error_returns_failure(self, ctx: AppContext) -> None:
        """add_account() devuelve success=False si el login falla."""
        ctx.manager.login.side_effect = LoginError("Credenciales incorrectas.")

        result = await add_account(ctx, ALIAS_1, "bad@example.com", "wrongpass")

        assert result["success"] is False
        assert "login" in result["message"].lower() or "credenciales" in result["message"].lower(), (
            f"Mensaje inesperado: {result['message']!r}"
        )
        assert not ctx.account_store.exists_alias(ALIAS_1), (
            "No deberia haberse guardado el alias tras login fallido."
        )


# ---------------------------------------------------------------------------
# Tests de delete_account
# ---------------------------------------------------------------------------

class TestDeleteAccount:

    def test_delete_existing(self, ctx_with_account: AppContext) -> None:
        """delete_account() elimina cuenta existente y devuelve success=True."""
        result = delete_account(ctx_with_account, ALIAS_1)

        assert result["success"] is True
        assert not ctx_with_account.account_store.exists_alias(ALIAS_1), (
            "La cuenta deberia haber sido eliminada."
        )

    def test_delete_nonexistent(self, ctx: AppContext) -> None:
        """delete_account() devuelve success=False para alias inexistente."""
        result = delete_account(ctx, "No Existe")

        assert result["success"] is False
        assert "encontro" in result["message"].lower() or "found" in result["message"].lower(), (
            f"Mensaje inesperado: {result['message']!r}"
        )


# ---------------------------------------------------------------------------
# Tests de list_accounts
# ---------------------------------------------------------------------------

class TestListAccounts:

    def test_empty(self, ctx: AppContext) -> None:
        """list_accounts() devuelve lista vacia si no hay cuentas."""
        assert list_accounts(ctx) == []

    def test_returns_registered_accounts(self, ctx: AppContext) -> None:
        """list_accounts() devuelve las cuentas registradas."""
        ctx.account_store.add("A", HASH_1, "os")
        ctx.account_store.add("B", "b" * 64, "cn")

        result = list_accounts(ctx)

        assert len(result) == 2
        aliases = [r["alias"] for r in result]
        assert "A" in aliases
        assert "B" in aliases


# ---------------------------------------------------------------------------
# Tests de restore_session
# ---------------------------------------------------------------------------

class TestRestoreSession:

    @pytest.mark.asyncio
    async def test_returns_none_if_no_cookies(self, ctx: AppContext) -> None:
        """restore_session() devuelve None si no hay cookies para el hash."""
        account = {"alias": ALIAS_1, "account_hash": HASH_1, "region": "os"}
        result = await restore_session(ctx, account)

        assert result is None, (
            "restore_session() deberia devolver None cuando no hay cookies."
        )

    @pytest.mark.asyncio
    async def test_returns_none_if_cookies_expired(self, ctx: AppContext) -> None:
        """restore_session() devuelve None si las cookies no son validas."""
        ctx.cookie_store.set("x@x.com", "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        # Calcular el hash real
        from db.cookie_store import _hash_email
        real_hash = _hash_email("x@x.com")
        ctx.cookie_store.set("x@x.com", "os", TEST_COOKIES_OS, TEST_DEVICE_ID)

        account = {"alias": ALIAS_1, "account_hash": real_hash, "region": "os"}

        with patch(
            "HoyoWrapper.session.HoyoSession.get_game_accounts",
            new=AsyncMock(side_effect=HoyoAPIError(-100, "Auth failed", {}))
        ):
            result = await restore_session(ctx, account)

        assert result is None, (
            "restore_session() deberia devolver None con cookies expiradas."
        )

    @pytest.mark.asyncio
    async def test_returns_active_session_if_valid(self, ctx: AppContext) -> None:
        """restore_session() devuelve ActiveSession si las cookies son validas."""
        from db.cookie_store import _hash_email
        real_hash = _hash_email("valid@example.com")

        ctx.cookie_store.set("valid@example.com", "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        account = {"alias": ALIAS_1, "account_hash": real_hash, "region": "os"}

        mock_accounts = [{
            "game_biz": "hk4e_global", "game_uid": "700000001",
            "nickname": "Traveler", "level": 60,
            "region": "os_asia", "region_name": "Asia",
        }]

        with patch(
            "HoyoWrapper.session.HoyoSession.get_game_accounts",
            new=AsyncMock(return_value=mock_accounts)
        ):
            result = await restore_session(ctx, account)

        assert result is not None, (
            "restore_session() deberia devolver ActiveSession con cookies validas."
        )
        assert isinstance(result, ActiveSession), (
            f"Se esperaba ActiveSession, obtenido: {type(result)}"
        )


# ---------------------------------------------------------------------------
# Tests de run_daily_tasks
# ---------------------------------------------------------------------------

class TestRunDailyTasks:

    @pytest.mark.asyncio
    async def test_empty_accounts_returns_empty(self, ctx: AppContext) -> None:
        """run_daily_tasks() devuelve lista vacia si no hay cuentas."""
        result = await run_daily_tasks(ctx)
        assert result["accounts"] == [], (
            f"Sin cuentas deberia devolver lista vacia, obtenido: {result['accounts']}"
        )

    @pytest.mark.asyncio
    async def test_expired_session_reported_as_error(self, ctx: AppContext) -> None:
        """
        Si restore_session() devuelve None, la cuenta aparece en el resultado
        con success=False y se continua con las demas.
        """
        ctx.account_store.add(ALIAS_1, HASH_1, "os")

        with patch("app.restore_session", new=AsyncMock(return_value=None)):
            with patch("app.random_delay", new=AsyncMock()):
                result = await run_daily_tasks(ctx)

        assert len(result["accounts"]) == 1
        assert result["accounts"][0]["success"] is False, (
            "Cuenta con sesion expirada deberia tener success=False."
        )
        assert result["accounts"][0]["alias"] == ALIAS_1

    @pytest.mark.asyncio
    async def test_successful_account_has_tasks(self, ctx: AppContext) -> None:
        """
        Cuenta con sesion valida produce resultados de tareas.
        """
        ctx.account_store.add(ALIAS_1, HASH_1, "os")

        # Mock de juego con check-in exitoso y sin mimo
        mock_game = MagicMock()
        mock_game.GAME_NAME = "Genshin Impact"
        mock_game.game_biz  = "hk4e_global"
        mock_game.uid       = "700000001"
        mock_game.claim_daily_reward = AsyncMock(return_value={
            "message": "Check-in realizado.",
            "already_done": False,
            "total_sign_day": 5,
        })
        mock_game.get_mimo_tasks = AsyncMock(side_effect=NotImplementedError)
        mock_game.buy_mimo_currency = AsyncMock(side_effect=NotImplementedError)

        mock_active = MagicMock(spec=ActiveSession)
        mock_active.games = [mock_game]

        with patch("app.restore_session", new=AsyncMock(return_value=mock_active)):
            with patch("app.random_delay", new=AsyncMock()):
                result = await run_daily_tasks(ctx)

        assert result["accounts"][0]["success"] is True
        tasks = result["accounts"][0]["tasks"]
        checkin_tasks = [t for t in tasks if t["type"] == "checkin"]
        assert len(checkin_tasks) == 1, (
            f"Deberia haber 1 tarea de check-in, encontradas: {len(checkin_tasks)}"
        )
        assert checkin_tasks[0]["success"] is True

    @pytest.mark.asyncio
    async def test_delay_between_accounts(self, ctx: AppContext) -> None:
        """
        ANTI-DETECCION: run_daily_tasks() llama random_delay entre cuentas.
        Con 2 cuentas debe haber exactamente 1 delay.
        """
        ctx.account_store.add("Cuenta A", HASH_1, "os")
        ctx.account_store.add("Cuenta B", "b" * 64, "os")

        delay_count = 0

        async def mock_delay(*a, **kw):
            nonlocal delay_count
            delay_count += 1

        mock_active = MagicMock(spec=ActiveSession)
        mock_active.games = []

        with patch("app.restore_session", new=AsyncMock(return_value=mock_active)):
            with patch("app.random_delay", side_effect=mock_delay):
                await run_daily_tasks(ctx)

        assert delay_count == 1, (
            f"Con 2 cuentas deberia haber 1 delay entre ellas, "
            f"se produjeron {delay_count}."
        )


# ---------------------------------------------------------------------------
# Tests de _process_mimo_tasks
# ---------------------------------------------------------------------------

class TestProcessMimoTasks:

    @pytest.mark.asyncio
    async def test_skips_if_not_implemented(self) -> None:
        """Sin soporte de mimo devuelve lista vacia."""
        game = MagicMock()
        game.get_mimo_tasks = AsyncMock(side_effect=NotImplementedError)
        result = await _process_mimo_tasks(game)
        assert result == []

    @pytest.mark.asyncio
    async def test_claims_completed_tasks(self) -> None:
        """Reclama tareas con status=1 y las reporta como exitosas."""
        game = MagicMock()
        game.GAME_NAME = "Genshin Impact"
        game.get_mimo_tasks = AsyncMock(return_value=[
            {"task_id": 1, "name": "Iniciar sesion", "status": 1},
            {"task_id": 2, "name": "Explorar mapa",  "status": 0},
        ])
        game.claim_mimo_task_reward = AsyncMock(return_value=None)

        result = await _process_mimo_tasks(game)

        claimed  = [r for r in result if r["success"] is True]
        pending  = [r for r in result if not r["success"] and "pendiente" in r["message"].lower()]

        assert len(claimed) == 1, (
            f"Deberia haberse reclamado 1 tarea, se reclamaron: {len(claimed)}"
        )
        assert len(pending) == 1, (
            f"Deberia haber 1 tarea pendiente, encontradas: {len(pending)}"
        )
        assert claimed[0]["detail"] == "Iniciar sesion", (
            f"Nombre de tarea reclamada incorrecto: {claimed[0]['detail']!r}"
        )

    @pytest.mark.asyncio
    async def test_handles_claim_error(self) -> None:
        """Si claim_mimo_task_reward falla, el error se reporta sin propagarse."""
        game = MagicMock()
        game.GAME_NAME = "Honkai: Star Rail"
        game.get_mimo_tasks = AsyncMock(return_value=[
            {"task_id": 5, "name": "Tarea fallida", "status": 1},
        ])
        game.claim_mimo_task_reward = AsyncMock(
            side_effect=HoyoAPIError(-100, "Error al reclamar", {})
        )

        result = await _process_mimo_tasks(game)

        assert len(result) == 1
        assert result[0]["success"] is False
        assert result[0]["detail"] == "Tarea fallida"


# ---------------------------------------------------------------------------
# Tests de _process_mimo_buys
# ---------------------------------------------------------------------------

class TestProcessMimoBuys:

    @pytest.mark.asyncio
    async def test_skips_if_not_implemented(self) -> None:
        """Sin soporte de mimo devuelve lista vacia."""
        game = MagicMock()
        game.buy_mimo_currency = AsyncMock(side_effect=NotImplementedError)
        result = await _process_mimo_buys(game)
        assert result == []

    @pytest.mark.asyncio
    async def test_no_purchases_returns_empty(self) -> None:
        """Si no hay monedas que comprar devuelve lista vacia."""
        game = MagicMock()
        game.GAME_NAME = "Genshin Impact"
        game.game_biz  = "hk4e_global"
        game.buy_mimo_currency = AsyncMock(return_value=[])
        result = await _process_mimo_buys(game)
        assert result == []

    @pytest.mark.asyncio
    async def test_successful_purchase_reported(self) -> None:
        """Compra exitosa se reporta con el codigo y la URL de canje."""
        game = MagicMock()
        game.GAME_NAME = "Genshin Impact"
        game.game_biz  = "hk4e_global"
        game.buy_mimo_currency = AsyncMock(return_value=[
            {"item_name": "Genesis Crystals", "code": "GENSHIN2024"},
        ])

        result = await _process_mimo_buys(game)

        assert len(result) == 1
        assert result[0]["success"] is True
        assert "GENSHIN2024" in result[0]["detail"], (
            f"El codigo deberia aparecer en detail: {result[0]['detail']!r}"
        )
        assert "genshin.hoyoverse.com" in result[0]["detail"], (
            f"La URL de canje deberia aparecer en detail: {result[0]['detail']!r}"
        )

    @pytest.mark.asyncio
    async def test_api_error_reported(self) -> None:
        """Error en la compra mimo se reporta sin propagarse."""
        game = MagicMock()
        game.GAME_NAME = "Zenless Zone Zero"
        game.game_biz  = "nap_global"
        game.buy_mimo_currency = AsyncMock(
            side_effect=HoyoAPIError(-500, "Insufficient points", {})
        )

        result = await _process_mimo_buys(game)

        assert len(result) == 1
        assert result[0]["success"] is False
        assert "Insufficient points" in result[0]["message"] or "-500" in result[0]["message"]
