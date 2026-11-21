"""
tests/test_app.py
------------------
Tests de app.py — logica de negocio de la aplicacion.

Todas las dependencias externas (HoyoManager, HoyoSession, juegos)
se mockean. No se hace ningun request real a HoYoverse. AccountStore y
GiftCodeStore usan SQLite en memoria real (no se mockean — la logica
de queries/constraints es parte de lo que se prueba).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from app import (
    AppContext,
    add_account,
    relogin_account,
    delete_account,
    list_accounts,
    restore_session,
    run_daily_tasks,
    redeem_code_for_all,
    add_gift_code,
    redeem_pending_codes,
    _process_mimo_tasks,
    _process_mimo_buys,
    _redeem_with_token_refresh,
    _transport_error_text,
    describe_game,
    get_token_report,
    parse_index_selection,
)
import config
from db.store import AccountStore, GiftCodeStore
from HoyoWrapper.session import HoyoSession, HoyoAPIError, LoginError
from HoyoWrapper.session_manager import ActiveSession
from tests.conftest import TEST_DEVICE_ID, TEST_COOKIES_OS

ALIAS_1  = "Cuenta Principal"
EMAIL_1  = "test@example.com"
REGION_1 = "os"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def account_store() -> AccountStore:
    return AccountStore(":memory:")


@pytest.fixture
def code_store() -> GiftCodeStore:
    return GiftCodeStore(":memory:")


@pytest.fixture
def mock_manager(account_store: AccountStore) -> MagicMock:
    """
    HoyoManager mockeado con login() que devuelve ActiveSession valida.

    IMPORTANTE: el login() real de AuthManager persiste la sesion en el
    store como efecto colateral (store.set(email, region, cookies,
    device_id)) ANTES de que app.py pueda asignarle un alias — set_alias()
    exige que esa fila ya exista. Por eso el mock debe replicar ese
    efecto colateral, o add_account()/relogin_account() fallarian con
    "no hay sesion guardada" aunque el login en si haya sido exitoso.
    """
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

    async def fake_login(email, password, *, region=None, force_fresh=False):
        account_store.set(email, region or "os", dict(TEST_COOKIES_OS), TEST_DEVICE_ID)
        return mock_active

    manager.login = AsyncMock(side_effect=fake_login)
    manager.renew_cookie_token = AsyncMock(return_value=True)
    manager._mock_active = mock_active  # acceso directo para configurar la sesion en tests
    return manager


@pytest.fixture
def ctx(account_store, code_store, mock_manager) -> AppContext:
    return AppContext(
        account_store=account_store,
        code_store=code_store,
        manager=mock_manager,
    )


@pytest.fixture
def ctx_with_account(ctx) -> AppContext:
    """AppContext con una cuenta ya registrada (sesion + alias)."""
    ctx.account_store.set(EMAIL_1, REGION_1, TEST_COOKIES_OS, TEST_DEVICE_ID)
    ctx.account_store.set_alias(EMAIL_1, REGION_1, ALIAS_1)
    return ctx


def _seed_account(store: AccountStore, alias: str, email: str, region: str = "os") -> None:
    """Helper para registrar una cuenta completa (sesion + alias) de una vez."""
    store.set(email, region, dict(TEST_COOKIES_OS), TEST_DEVICE_ID)
    store.set_alias(email, region, alias)


# ---------------------------------------------------------------------------
# Tests de add_account
# ---------------------------------------------------------------------------

class TestAddAccount:

    @pytest.mark.asyncio
    async def test_success(self, ctx: AppContext) -> None:
        """add_account() exitoso registra alias y devuelve success=True."""
        ctx.manager._mock_active.session.get_game_accounts = AsyncMock(
            return_value=[{
                "game_biz": "hk4e_global", "game_uid": "700000001",
                "nickname": "Traveler", "level": 60,
                "region": "os_asia", "region_name": "Asia",
            }]
        )

        result = await add_account(ctx, ALIAS_1, EMAIL_1, "pass123")

        assert result["success"] is True, (
            f"add_account() deberia ser exitoso.\n  Resultado: {result}"
        )
        assert result["alias"] == ALIAS_1
        assert ctx.account_store.exists_alias(ALIAS_1), (
            "El alias deberia haberse guardado en AccountStore."
        )

    @pytest.mark.asyncio
    async def test_duplicate_alias_fails_without_attempting_login(self, ctx: AppContext) -> None:
        """
        add_account() falla si el alias ya existe, SIN hacer login —
        evita trafico de red innecesario cuando el fallo es previsible
        de antemano solo con lo que ya esta en la DB local.
        """
        _seed_account(ctx.account_store, ALIAS_1, EMAIL_1)

        result = await add_account(ctx, ALIAS_1, "other@example.com", "pass")

        assert result["success"] is False
        assert "alias" in result["message"].lower()
        ctx.manager.login.assert_not_called()

    @pytest.mark.asyncio
    async def test_login_error_returns_failure(self, ctx: AppContext) -> None:
        """add_account() devuelve success=False si el login falla, sin dejar rastro en la DB."""
        ctx.manager.login.side_effect = LoginError("Credenciales incorrectas.")

        result = await add_account(ctx, ALIAS_1, "bad@example.com", "wrongpass")

        assert result["success"] is False
        assert not ctx.account_store.exists_alias(ALIAS_1), (
            "No deberia haberse guardado el alias tras login fallido."
        )


# ---------------------------------------------------------------------------
# Tests de relogin_account
#
# Esta funcion es la razon de que el email se guarde en texto plano: el
# usuario elige un alias, y el sistema ya sabe que email usar — solo
# pide la contrasena.
# ---------------------------------------------------------------------------

class TestReloginAccount:

    @pytest.mark.asyncio
    async def test_success_reuses_stored_email(self, ctx_with_account: AppContext) -> None:
        """
        relogin_account() encuentra el email a partir del alias (sin que
        el usuario lo re-ingrese) y llama a manager.login() con el.
        """
        ctx_with_account.manager._mock_active.session.get_game_accounts = AsyncMock(
            return_value=[{
                "game_biz": "hk4e_global", "game_uid": "700000001",
                "nickname": "Traveler", "level": 60,
                "region": "os_asia", "region_name": "Asia",
            }]
        )

        result = await relogin_account(ctx_with_account, ALIAS_1, "new_password")

        assert result["success"] is True, f"Resultado: {result}"
        called_email = ctx_with_account.manager.login.call_args.args[0]
        assert called_email == EMAIL_1, (
            f"relogin_account deberia usar el email guardado ({EMAIL_1!r}), "
            f"uso: {called_email!r}"
        )

    @pytest.mark.asyncio
    async def test_nonexistent_alias_fails_without_login_attempt(self, ctx: AppContext) -> None:
        result = await relogin_account(ctx, "No Existe", "password")

        assert result["success"] is False
        assert "no existe" in result["message"].lower()
        ctx.manager.login.assert_not_called()

    @pytest.mark.asyncio
    async def test_login_error_reported_as_failure(self, ctx_with_account: AppContext) -> None:
        ctx_with_account.manager.login.side_effect = LoginError("Contrasena incorrecta.")

        result = await relogin_account(ctx_with_account, ALIAS_1, "wrong_password")

        assert result["success"] is False
        assert "login" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_relogin_does_not_require_new_alias(self, ctx_with_account: AppContext) -> None:
        """
        REGLA CLAVE: relogin_account() NO le pide un alias nuevo al
        caller — usa el mismo que ya tenia, exactamente porque AuthManager
        (via manager.login) nunca toca la columna alias al persistir
        cookies (ver AccountStore.set()).
        """
        ctx_with_account.manager._mock_active.session.get_game_accounts = AsyncMock(
            return_value=[]
        )
        await relogin_account(ctx_with_account, ALIAS_1, "new_password")

        assert ctx_with_account.account_store.exists_alias(ALIAS_1), (
            "El alias deberia seguir existiendo despues del relogin."
        )


# ---------------------------------------------------------------------------
# Tests de delete_account (soft-delete)
# ---------------------------------------------------------------------------

class TestDeleteAccount:

    def test_delete_existing(self, ctx_with_account: AppContext) -> None:
        result = delete_account(ctx_with_account, ALIAS_1)

        assert result["success"] is True
        assert not ctx_with_account.account_store.exists_alias(ALIAS_1)

    def test_delete_nonexistent(self, ctx: AppContext) -> None:
        result = delete_account(ctx, "No Existe")

        assert result["success"] is False
        assert "encontro" in result["message"].lower() or "found" in result["message"].lower()

    def test_delete_preserves_cookies_for_future_relogin(self, ctx_with_account: AppContext) -> None:
        """
        REGLA DE NEGOCIO: eliminar una cuenta NO borra sus cookies — solo
        la etiqueta. Se prueba a nivel de app.py (no solo en db/store.py)
        porque es el comportamiento visible que un usuario esperaria:
        re-agregar la misma cuenta no deberia pedir captcha de nuevo.
        """
        delete_account(ctx_with_account, ALIAS_1)

        cookies, _ = ctx_with_account.account_store.get(EMAIL_1, REGION_1)
        assert cookies == TEST_COOKIES_OS, (
            "Las cookies deberian seguir disponibles tras eliminar la cuenta "
            "(soft-delete), para que un futuro re-registro las reutilice."
        )


# ---------------------------------------------------------------------------
# Tests de list_accounts
# ---------------------------------------------------------------------------

class TestListAccounts:

    def test_empty(self, ctx: AppContext) -> None:
        assert list_accounts(ctx) == []

    def test_returns_registered_accounts(self, ctx: AppContext) -> None:
        _seed_account(ctx.account_store, "A", "a@example.com", "os")
        _seed_account(ctx.account_store, "B", "b@example.com", "cn")

        result = list_accounts(ctx)

        assert len(result) == 2
        aliases = [r["alias"] for r in result]
        assert "A" in aliases and "B" in aliases


# ---------------------------------------------------------------------------
# Tests de restore_session
# ---------------------------------------------------------------------------

class TestRestoreSession:

    @pytest.mark.asyncio
    async def test_returns_none_if_no_cookies(self, ctx: AppContext) -> None:
        """Un dict de cuenta sin cookies (nunca deberia pasar desde list_all(), pero defensivo)."""
        account = {"alias": ALIAS_1, "email": EMAIL_1, "region": "os",
                   "cookies": {}, "device_id": None}
        result = await restore_session(ctx, account)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_if_cookies_expired(self, ctx: AppContext) -> None:
        account = {"alias": ALIAS_1, "email": EMAIL_1, "region": "os",
                   "cookies": TEST_COOKIES_OS, "device_id": TEST_DEVICE_ID}

        with patch(
            "HoyoWrapper.session.HoyoSession.get_game_accounts",
            new=AsyncMock(side_effect=HoyoAPIError(-100, "Auth failed", {}))
        ):
            result = await restore_session(ctx, account)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_active_session_if_valid(self, ctx: AppContext) -> None:
        account = {"alias": ALIAS_1, "email": EMAIL_1, "region": "os",
                   "cookies": TEST_COOKIES_OS, "device_id": TEST_DEVICE_ID}

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

        assert isinstance(result, ActiveSession), (
            f"Se esperaba ActiveSession, obtenido: {type(result)}"
        )

    @pytest.mark.asyncio
    async def test_uses_cookies_from_account_dict_directly(self, ctx: AppContext) -> None:
        """
        REGLA CLAVE (simplificacion habilitada por la fusion de tablas):
        restore_session() NO debe hacer una segunda consulta a la DB —
        toma cookies/device_id directamente del dict de cuenta recibido.
        Verificado dando un device_id DISTINTO al que tiene la cuenta
        real en el store (que aca ni se toca), para confirmar que se usa
        el valor del dict y no uno recien consultado.
        """
        custom_device_id = "custom-not-in-db-1234"
        account = {"alias": ALIAS_1, "email": EMAIL_1, "region": "os",
                   "cookies": TEST_COOKIES_OS, "device_id": custom_device_id}

        with patch(
            "HoyoWrapper.session.HoyoSession.get_game_accounts",
            new=AsyncMock(return_value=[])
        ):
            result = await restore_session(ctx, account)

        assert result.session.device_id == custom_device_id, (
            "restore_session deberia usar el device_id del dict recibido, "
            "no volver a consultarlo de la DB."
        )


# ---------------------------------------------------------------------------
# Tests de run_daily_tasks
# ---------------------------------------------------------------------------

class TestRunDailyTasks:

    @pytest.mark.asyncio
    async def test_empty_accounts_returns_empty(self, ctx: AppContext) -> None:
        result = await run_daily_tasks(ctx)
        assert result["accounts"] == []

    @pytest.mark.asyncio
    async def test_expired_session_reported_as_error(self, ctx: AppContext) -> None:
        _seed_account(ctx.account_store, ALIAS_1, EMAIL_1)

        with patch("app.restore_session", new=AsyncMock(return_value=None)):
            with patch("app.random_delay", new=AsyncMock()):
                result = await run_daily_tasks(ctx)

        assert len(result["accounts"]) == 1
        assert result["accounts"][0]["success"] is False
        assert result["accounts"][0]["alias"] == ALIAS_1

    @pytest.mark.asyncio
    async def test_successful_account_has_tasks(self, ctx: AppContext) -> None:
        _seed_account(ctx.account_store, ALIAS_1, EMAIL_1)

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
        checkin_tasks = [t for t in result["accounts"][0]["tasks"] if t["type"] == "checkin"]
        assert len(checkin_tasks) == 1
        assert checkin_tasks[0]["success"] is True

    @pytest.mark.asyncio
    async def test_delay_between_accounts_uses_config_range(self, ctx: AppContext) -> None:
        """
        ANTI-DETECCION: con N cuentas hay N-1 delays, y usan
        config.DELAY_BETWEEN_ACCOUNTS (no un valor hardcodeado aparte).
        """
        _seed_account(ctx.account_store, "Cuenta A", "a@example.com")
        _seed_account(ctx.account_store, "Cuenta B", "b@example.com")

        received_args = []

        async def mock_delay(*a, **kw):
            received_args.append(a)

        mock_active = MagicMock(spec=ActiveSession)
        mock_active.games = []

        with patch("app.restore_session", new=AsyncMock(return_value=mock_active)):
            with patch("app.random_delay", side_effect=mock_delay):
                await run_daily_tasks(ctx)

        assert len(received_args) == 1, (
            f"Con 2 cuentas deberia haber 1 delay, hubo {len(received_args)}."
        )
        assert received_args[0] == config.DELAY_BETWEEN_ACCOUNTS, (
            f"El delay deberia usar config.DELAY_BETWEEN_ACCOUNTS "
            f"{config.DELAY_BETWEEN_ACCOUNTS}, uso: {received_args[0]}"
        )


# ---------------------------------------------------------------------------
# Tests de redeem_code_for_all (canje manual)
# ---------------------------------------------------------------------------

class TestRedeemCodeForAll:

    @pytest.mark.asyncio
    async def test_empty_accounts_returns_empty(self, ctx: AppContext) -> None:
        result = await redeem_code_for_all(ctx, "hk4e_global", "TESTCODE")
        assert result == []

    @pytest.mark.asyncio
    async def test_cn_accounts_skipped(self, ctx: AppContext) -> None:
        """Cuentas CN se omiten — redeem solo disponible en OS."""
        _seed_account(ctx.account_store, "Cuenta CN", "cn@example.com", "cn")
        result = await redeem_code_for_all(ctx, "hk4e_global", "TESTCODE")
        assert result == []

    @pytest.mark.asyncio
    async def test_account_without_game_skipped(self, ctx: AppContext) -> None:
        _seed_account(ctx.account_store, "Cuenta OS", "os1@example.com")

        mock_active = MagicMock(spec=ActiveSession)
        mock_active.get_game = lambda biz: None

        with patch("app.restore_session", new=AsyncMock(return_value=mock_active)):
            result = await redeem_code_for_all(ctx, "hk4e_global", "TESTCODE")

        assert result == []

    @pytest.mark.asyncio
    async def test_successful_redeem_reported_and_recorded_in_code_store(
        self, ctx: AppContext
    ) -> None:
        """
        Un canje manual exitoso no solo se reporta — tambien queda
        registrado en GiftCodeStore como 'success', para que el sistema
        automatico (redeem_pending_codes) no lo vuelva a intentar en esta
        misma cuenta.
        """
        _seed_account(ctx.account_store, "Cuenta OS", "os1@example.com")

        mock_game = MagicMock()
        mock_game.game_biz  = "hk4e_global"
        mock_game.GAME_NAME = "Genshin Impact"
        mock_game.uid       = "700000001"
        mock_game.redeem_code = AsyncMock(return_value=None)

        mock_active = MagicMock(spec=ActiveSession)
        mock_active.get_game = lambda biz: mock_game if biz == "hk4e_global" else None

        with patch("app.restore_session", new=AsyncMock(return_value=mock_active)):
            with patch("app.random_delay", new=AsyncMock()):
                result = await redeem_code_for_all(ctx, "hk4e_global", "ABCD1234")

        assert len(result) == 1 and result[0]["success"] is True
        mock_game.redeem_code.assert_called_once_with("ABCD1234", lang="en")

        # Verificar que quedo persistido en GiftCodeStore
        code_id = ctx.code_store.get_or_create_code_id("hk4e_global", "ABCD1234")
        assert ctx.code_store.has_redeemed(code_id, "Cuenta OS") is True, (
            "El canje manual exitoso deberia registrarse en GiftCodeStore."
        )

    @pytest.mark.asyncio
    async def test_globally_dead_retcode_marks_code_dead(self, ctx: AppContext) -> None:
        """
        Un retcode GLOBAL (ej. -2001 expirado) durante el canje manual
        marca el codigo como muerto en GiftCodeStore — no solo se
        reporta como fallo puntual de esa cuenta.
        """
        _seed_account(ctx.account_store, "Cuenta OS", "os1@example.com")

        mock_game = MagicMock()
        mock_game.game_biz  = "hk4e_global"
        mock_game.GAME_NAME = "Genshin Impact"
        mock_game.uid       = "700000001"
        mock_game.redeem_code = AsyncMock(
            side_effect=HoyoAPIError(-2001, "Redemption code has expired.", {})
        )

        mock_active = MagicMock(spec=ActiveSession)
        mock_active.get_game = lambda biz: mock_game if biz == "hk4e_global" else None

        with patch("app.restore_session", new=AsyncMock(return_value=mock_active)):
            with patch("app.random_delay", new=AsyncMock()):
                await redeem_code_for_all(ctx, "hk4e_global", "EXPIREDCODE")

        codes = ctx.code_store.list_codes("hk4e_global")
        assert codes[0]["status"] == "dead", (
            f"El codigo deberia quedar marcado 'dead' tras un retcode global. Estado: {codes[0]}"
        )

    @pytest.mark.asyncio
    async def test_api_error_reported_as_failure(self, ctx: AppContext) -> None:
        _seed_account(ctx.account_store, "Cuenta OS", "os1@example.com")

        mock_game = MagicMock()
        mock_game.game_biz  = "hk4e_global"
        mock_game.GAME_NAME = "Genshin Impact"
        mock_game.uid       = "700000001"
        mock_game.redeem_code = AsyncMock(
            side_effect=HoyoAPIError(-2017, "Code already used", {})
        )

        mock_active = MagicMock(spec=ActiveSession)
        mock_active.get_game = lambda biz: mock_game if biz == "hk4e_global" else None

        with patch("app.restore_session", new=AsyncMock(return_value=mock_active)):
            with patch("app.random_delay", new=AsyncMock()):
                result = await redeem_code_for_all(ctx, "hk4e_global", "USEDCODE")

        assert len(result) == 1 and result[0]["success"] is False
        assert "-2017" in result[0]["message"]

    @pytest.mark.asyncio
    async def test_multiple_accounts_all_receive_code(self, ctx: AppContext) -> None:
        _seed_account(ctx.account_store, "Cuenta A", "a@example.com")
        _seed_account(ctx.account_store, "Cuenta B", "b@example.com")

        def make_game(uid: str) -> MagicMock:
            g = MagicMock()
            g.game_biz  = "hk4e_global"
            g.GAME_NAME = "Genshin Impact"
            g.uid       = uid
            g.redeem_code = AsyncMock(return_value=None)
            return g

        game_a, game_b = make_game("700000001"), make_game("700000002")
        sessions = [
            MagicMock(spec=ActiveSession, get_game=lambda biz, g=game_a: g if biz == "hk4e_global" else None),
            MagicMock(spec=ActiveSession, get_game=lambda biz, g=game_b: g if biz == "hk4e_global" else None),
        ]
        call_count = {"n": 0}

        async def mock_restore(ctx, account):
            s = sessions[call_count["n"]]
            call_count["n"] += 1
            return s

        with patch("app.restore_session", side_effect=mock_restore):
            with patch("app.random_delay", new=AsyncMock()):
                result = await redeem_code_for_all(ctx, "hk4e_global", "MULTICODE")

        assert len(result) == 2 and all(r["success"] for r in result)

    @pytest.mark.asyncio
    async def test_expired_session_skipped_gracefully(self, ctx: AppContext) -> None:
        _seed_account(ctx.account_store, "Expirada", "expired@example.com")
        _seed_account(ctx.account_store, "Valida", "valid@example.com")

        mock_game = MagicMock()
        mock_game.game_biz  = "hk4e_global"
        mock_game.GAME_NAME = "Genshin Impact"
        mock_game.uid       = "700000002"
        mock_game.redeem_code = AsyncMock(return_value=None)

        mock_active = MagicMock(spec=ActiveSession)
        mock_active.get_game = lambda biz: mock_game if biz == "hk4e_global" else None

        sessions = [None, mock_active]
        call_count = {"n": 0}

        async def mock_restore(ctx, account):
            s = sessions[call_count["n"]]
            call_count["n"] += 1
            return s

        with patch("app.restore_session", side_effect=mock_restore):
            with patch("app.random_delay", new=AsyncMock()):
                result = await redeem_code_for_all(ctx, "hk4e_global", "TESTCODE")

        assert len(result) == 1 and result[0]["success"] is True


# ---------------------------------------------------------------------------
# Tests de add_gift_code
# ---------------------------------------------------------------------------

class TestAddGiftCode:

    @pytest.mark.asyncio
    async def test_success(self, ctx: AppContext) -> None:
        result = await add_gift_code(ctx, "hk4e_global", "NEWCODE123")
        assert result["success"] is True
        assert len(ctx.code_store.list_codes("hk4e_global")) == 1

    @pytest.mark.asyncio
    async def test_rejects_game_without_redeem_support(self, ctx: AppContext) -> None:
        """Honkai Impact 3rd no tiene endpoint de redeem — no debe poder agregarse un codigo para el."""
        result = await add_gift_code(ctx, "bh3_global", "SOMECODE")
        assert result["success"] is False
        assert ctx.code_store.list_codes("bh3_global") == []

    @pytest.mark.asyncio
    async def test_duplicate_add_is_not_an_error(self, ctx: AppContext) -> None:
        """Agregar el mismo codigo dos veces no es un error — solo informa que ya estaba."""
        await add_gift_code(ctx, "hk4e_global", "DUPECODE")
        result = await add_gift_code(ctx, "hk4e_global", "DUPECODE")

        assert result["success"] is True, "Un duplicado no deberia tratarse como fallo del usuario."
        assert len(ctx.code_store.list_codes("hk4e_global")) == 1


# ---------------------------------------------------------------------------
# Tests de redeem_pending_codes (canje automatico)
# ---------------------------------------------------------------------------

class TestRedeemPendingCodes:

    @pytest.mark.asyncio
    async def test_no_accounts_returns_empty(self, ctx: AppContext) -> None:
        result = await redeem_pending_codes(ctx)
        assert result == []

    @pytest.mark.asyncio
    async def test_no_pending_codes_returns_empty(self, ctx: AppContext) -> None:
        _seed_account(ctx.account_store, ALIAS_1, EMAIL_1)

        mock_game = MagicMock()
        mock_game.game_biz  = "hk4e_global"
        mock_game.GAME_NAME = "Genshin Impact"
        mock_game.uid       = "700000001"

        mock_active = MagicMock(spec=ActiveSession)
        mock_active.games = [mock_game]

        with patch("app.restore_session", new=AsyncMock(return_value=mock_active)):
            with patch("app.random_delay", new=AsyncMock()):
                result = await redeem_pending_codes(ctx)

        assert result == [], "Sin codigos en la DB no deberia intentarse ningun canje."

    @pytest.mark.asyncio
    async def test_full_scenario_two_accounts_four_outcomes(self, ctx: AppContext) -> None:
        """
        Escenario de integracion end-to-end: 2 cuentas, 4 codigos con los
        4 desenlaces posibles (exito, ya reclamado, muerto globalmente,
        transitorio). Verifica el comportamiento agregado del sistema
        completo, no solo una unidad aislada.
        """
        _seed_account(ctx.account_store, "Alice", "alice@example.com")
        _seed_account(ctx.account_store, "Bob", "bob@example.com")

        def make_redeem():
            async def redeem_code(code, lang="en"):
                if code == "GOODCODE":
                    return {"message": "ok"}
                if code == "CLAIMEDCODE":
                    raise HoyoAPIError(-2017, "Redemption code has been claimed already.", {})
                if code == "DEADCODE":
                    raise HoyoAPIError(-2001, "Redemption code has expired.", {})
                if code == "COOLDOWNCODE":
                    raise HoyoAPIError(-2016, "Redemption is on cooldown.", {})
                raise HoyoAPIError(-9999, "retcode desconocido", {})
            return redeem_code

        def make_game(uid: str) -> MagicMock:
            g = MagicMock()
            g.game_biz  = "hk4e_global"
            g.GAME_NAME = "Genshin Impact"
            g.uid       = uid
            return g

        game_alice, game_bob = make_game("1"), make_game("2")
        game_alice.redeem_code = make_redeem()
        game_bob.redeem_code   = make_redeem()

        session_alice = MagicMock(spec=ActiveSession, games=[game_alice])
        session_bob   = MagicMock(spec=ActiveSession, games=[game_bob])

        async def fake_restore(ctx, account):
            return session_alice if account["alias"] == "Alice" else session_bob

        for code in ["GOODCODE", "CLAIMEDCODE", "DEADCODE", "COOLDOWNCODE"]:
            await add_gift_code(ctx, "hk4e_global", code)

        with patch("app.restore_session", side_effect=fake_restore):
            with patch("app.random_delay", new=AsyncMock()):
                await redeem_pending_codes(ctx)

        # DEADCODE debe haber muerto globalmente tras el primer intento
        # (Alice se procesa primero) — Bob ni siquiera deberia haberlo
        # intentado en esta misma corrida.
        dead_code = next(c for c in ctx.code_store.list_codes("hk4e_global") if c["code"] == "DEADCODE")
        assert dead_code["status"] == "dead"

        with patch("app.restore_session", side_effect=fake_restore):
            with patch("app.random_delay", new=AsyncMock()):
                second_run = await redeem_pending_codes(ctx)

        second_run_codes = " ".join(r["message"] for r in second_run)
        assert "GOODCODE" not in second_run_codes, "Exito no deberia reintentarse."
        assert "CLAIMEDCODE" not in second_run_codes, "Ya reclamado no deberia reintentarse."
        assert "DEADCODE" not in second_run_codes, "Muerto globalmente no deberia reintentarse."
        assert "COOLDOWNCODE" in second_run_codes, "Transitorio SI deberia reintentarse."

    @pytest.mark.asyncio
    async def test_expired_session_skipped(self, ctx: AppContext) -> None:
        _seed_account(ctx.account_store, ALIAS_1, EMAIL_1)
        await add_gift_code(ctx, "hk4e_global", "SOMECODE")

        with patch("app.restore_session", new=AsyncMock(return_value=None)):
            result = await redeem_pending_codes(ctx)

        assert result == [], "Una sesion expirada no deberia intentar canjear nada."


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
        """
        Reclama tareas con status=1 (FINISHED) y reporta como pendiente
        las de status=2 (ONGOING) que requieren accion en juego (task_type=6).

        Status segun genshin.py: 1=FINISHED, 2=ONGOING, 3=CLAIMED.
        task_type=6 (GI_GAME) requiere accion en juego — no completable via API.
        """
        game = MagicMock()
        game.GAME_NAME = "Genshin Impact"
        game.get_mimo_tasks = AsyncMock(return_value=[
            {"task_id": 1, "task_name": "Iniciar sesion", "status": 1, "task_type": 5},
            {"task_id": 2, "task_name": "Explorar mapa",  "status": 2, "task_type": 6},
        ])
        game.claim_mimo_task_reward = AsyncMock(return_value=None)
        game.finish_mimo_task = AsyncMock(return_value=None)

        with patch("app.random_delay", new=AsyncMock()):
            result = await _process_mimo_tasks(game)

        claimed  = [r for r in result if r["success"] is True]
        pending  = [r for r in result if not r["success"] and "pendiente" in r["message"].lower()]

        assert len(claimed) == 1
        assert len(pending) == 1
        assert claimed[0]["detail"] == "Iniciar sesion"

    @pytest.mark.asyncio
    async def test_finishes_and_claims_web_completable_tasks(self) -> None:
        """
        Tareas ONGOING con task_type completable via API (tipo 2=VISIT)
        se completan con finish_mimo_task() y luego se reclaman.
        """
        game = MagicMock()
        game.GAME_NAME = "Honkai: Star Rail"
        game.get_mimo_tasks = AsyncMock(return_value=[
            {"task_id": 10, "task_name": "Visitar grupo", "status": 2, "task_type": 2},
        ])
        game.finish_mimo_task = AsyncMock(return_value=None)
        game.claim_mimo_task_reward = AsyncMock(return_value=None)

        with patch("app.random_delay", new=AsyncMock()):
            result = await _process_mimo_tasks(game)

        game.finish_mimo_task.assert_called_once_with(10)
        game.claim_mimo_task_reward.assert_called_once_with(10)

        claimed = [r for r in result if r["success"] is True]
        assert len(claimed) == 1
        assert claimed[0]["detail"] == "Visitar grupo"

    @pytest.mark.asyncio
    async def test_handles_claim_error(self) -> None:
        """Si claim_mimo_task_reward falla, el error se reporta sin propagarse."""
        game = MagicMock()
        game.GAME_NAME = "Honkai: Star Rail"
        game.get_mimo_tasks = AsyncMock(return_value=[
            {"task_id": 5, "task_name": "Tarea fallida", "status": 1, "task_type": 5},
        ])
        game.finish_mimo_task = AsyncMock(return_value=None)
        game.claim_mimo_task_reward = AsyncMock(
            side_effect=HoyoAPIError(-100, "Error al reclamar", {})
        )

        with patch("app.random_delay", new=AsyncMock()):
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
        game = MagicMock()
        game.buy_mimo_currency = AsyncMock(side_effect=NotImplementedError)
        result = await _process_mimo_buys(game)
        assert result == []

    @pytest.mark.asyncio
    async def test_no_purchases_returns_empty(self) -> None:
        game = MagicMock()
        game.GAME_NAME = "Genshin Impact"
        game.game_biz  = "hk4e_global"
        game.buy_mimo_currency = AsyncMock(return_value=[])
        result = await _process_mimo_buys(game)
        assert result == []

    @pytest.mark.asyncio
    async def test_successful_purchase_reported(self) -> None:
        game = MagicMock()
        game.GAME_NAME = "Genshin Impact"
        game.game_biz  = "hk4e_global"
        game.buy_mimo_currency = AsyncMock(return_value=[
            {"item_name": "Genesis Crystals", "code": "GENSHIN2024"},
        ])

        result = await _process_mimo_buys(game)

        assert len(result) == 1
        assert result[0]["success"] is True
        assert "GENSHIN2024" in result[0]["detail"]
        assert "genshin.hoyoverse.com" in result[0]["detail"]

    @pytest.mark.asyncio
    async def test_api_error_reported(self) -> None:
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


# ---------------------------------------------------------------------------
# Relogin real y renovacion del cookie_token_v2
# ---------------------------------------------------------------------------

class TestReloginIsAFreshLogin:

    @pytest.mark.asyncio
    async def test_relogin_forces_a_real_login(self, ctx_with_account: AppContext) -> None:
        """
        REGLA CLAVE: "Reingresar contrasena" pide un login REAL
        (force_fresh=True). Sin eso, si el ltoken seguia vivo el login
        devolvia las cookies guardadas y la contrasena nunca se usaba.
        """
        ctx_with_account.manager._mock_active.session.get_game_accounts = AsyncMock(return_value=[])
        await relogin_account(ctx_with_account, ALIAS_1, "new_password")

        assert ctx_with_account.manager.login.call_args.kwargs["force_fresh"] is True

    @pytest.mark.asyncio
    async def test_add_account_does_not_force_fresh(self, ctx: AppContext) -> None:
        """Agregar una cuenta si puede reutilizar una sesion guardada (soft-delete previo)."""
        ctx.manager._mock_active.session.get_game_accounts = AsyncMock(return_value=[])
        await add_account(ctx, ALIAS_1, EMAIL_1, "pass123")

        assert ctx.manager.login.call_args.kwargs.get("force_fresh", False) is False


def _os_account(**overrides) -> dict:
    account = {
        "alias": "A", "email": "a@x.com", "region": "os",
        "cookies": {"ltuid_v2": "1", "cookie_token_v2": "STALE"}, "device_id": "dev-1",
    }
    account.update(overrides)
    return account


def _hoyo_error(retcode: int = -1071) -> HoyoAPIError:
    return HoyoAPIError(retcode, "Please log in to your account first.", {})


class TestRestoreSessionSharesCookies:

    @pytest.mark.asyncio
    async def test_session_uses_the_same_cookie_dict_as_the_account(self, ctx: AppContext) -> None:
        """
        La renovacion del token muta account["cookies"] in place y espera
        que la sesion viva lo vea: restore_session() NO debe copiar el dict.
        """
        account = {"alias": ALIAS_1, "email": EMAIL_1, "region": "os",
                   "cookies": dict(TEST_COOKIES_OS), "device_id": TEST_DEVICE_ID}

        with patch("HoyoWrapper.session.HoyoSession.get_game_accounts", new=AsyncMock(return_value=[])):
            active = await restore_session(ctx, account)

        assert active.session.cookies is account["cookies"]


class TestDailyTokenRenewal:

    def _active_with_game(self) -> MagicMock:
        game = MagicMock()
        game.GAME_NAME = "Genshin Impact"
        game.game_biz = "hk4e_global"
        game.uid = "700000001"
        game.claim_daily_reward = AsyncMock(return_value={
            "message": "ok", "already_done": False, "total_sign_day": 1,
        })
        game.get_mimo_tasks = AsyncMock(side_effect=NotImplementedError)
        game.buy_mimo_currency = AsyncMock(side_effect=NotImplementedError)
        active = MagicMock(spec=ActiveSession)
        active.games = [game]
        return active

    @pytest.mark.asyncio
    async def test_os_account_token_is_renewed_every_daily_run(self, ctx: AppContext) -> None:
        """
        REGLA CLAVE: el token se renueva en CADA corrida diaria, aunque
        ese dia no haya codigos — el token caduca y solo lo usa el canje.
        """
        _seed_account(ctx.account_store, ALIAS_1, EMAIL_1)

        with patch("app.restore_session", new=AsyncMock(return_value=self._active_with_game())):
            with patch("app.random_delay", new=AsyncMock()):
                await run_daily_tasks(ctx)

        ctx.manager.renew_cookie_token.assert_awaited_once()
        args = ctx.manager.renew_cookie_token.await_args.args
        assert args[0] == EMAIL_1 and args[1] == "os"

    @pytest.mark.asyncio
    async def test_cn_account_is_never_renewed(self, ctx: AppContext) -> None:
        _seed_account(ctx.account_store, "CN", "cn@x.com", "cn")

        with patch("app.restore_session", new=AsyncMock(return_value=self._active_with_game())):
            with patch("app.random_delay", new=AsyncMock()):
                await run_daily_tasks(ctx)

        ctx.manager.renew_cookie_token.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_renewal_happens_before_the_game_tasks(self, ctx: AppContext) -> None:
        """Si el token se renovara DESPUES, un canje hecho en la misma corrida seguiria viendo el vencido."""
        _seed_account(ctx.account_store, ALIAS_1, EMAIL_1)
        events: list[str] = []
        active = self._active_with_game()

        async def renew(*a, **kw):
            events.append("renew")
            return True

        async def checkin(*a, **kw):
            events.append("checkin")
            return {"message": "ok", "already_done": False, "total_sign_day": 1}

        ctx.manager.renew_cookie_token = AsyncMock(side_effect=renew)
        active.games[0].claim_daily_reward = AsyncMock(side_effect=checkin)

        with patch("app.restore_session", new=AsyncMock(return_value=active)):
            with patch("app.random_delay", new=AsyncMock()):
                await run_daily_tasks(ctx)

        assert events == ["renew", "checkin"], f"Orden incorrecto: {events}"

    @pytest.mark.asyncio
    async def test_renewal_failure_is_a_warning_not_a_failure(self, ctx: AppContext) -> None:
        """
        Si no se puede renovar (ej. falta ltmid_v2), el check-in y demas
        tareas igual se hacen — no dependen de ese token — y el usuario
        recibe un aviso accionable en vez de enterarse dias despues
        cuando un canje falle.
        """
        _seed_account(ctx.account_store, ALIAS_1, EMAIL_1)
        ctx.manager.renew_cookie_token = AsyncMock(return_value=False)

        with patch("app.restore_session", new=AsyncMock(return_value=self._active_with_game())):
            with patch("app.random_delay", new=AsyncMock()):
                result = await run_daily_tasks(ctx)

        acc = result["accounts"][0]
        assert acc["success"] is True
        assert any(t["type"] == "checkin" for t in acc["tasks"]), "El check-in no debe saltarse."
        assert len(acc["warnings"]) == 1 and "Reingresar contrasena" in acc["warnings"][0]

    @pytest.mark.asyncio
    async def test_successful_renewal_leaves_no_warning(self, ctx: AppContext) -> None:
        _seed_account(ctx.account_store, ALIAS_1, EMAIL_1)

        with patch("app.restore_session", new=AsyncMock(return_value=self._active_with_game())):
            with patch("app.random_delay", new=AsyncMock()):
                result = await run_daily_tasks(ctx)

        assert result["accounts"][0]["warnings"] == []

    @pytest.mark.asyncio
    async def test_renewal_can_be_disabled_in_config(self, ctx: AppContext, monkeypatch) -> None:
        monkeypatch.setattr(config, "RENEW_COOKIE_TOKEN_ON_DAILY_TASKS", False)
        _seed_account(ctx.account_store, ALIAS_1, EMAIL_1)

        with patch("app.restore_session", new=AsyncMock(return_value=self._active_with_game())):
            with patch("app.random_delay", new=AsyncMock()):
                await run_daily_tasks(ctx)

        ctx.manager.renew_cookie_token.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_renewal_when_session_is_expired(self, ctx: AppContext) -> None:
        """Con la sesion base muerta no tiene sentido pedir un token: se reporta y se sigue."""
        _seed_account(ctx.account_store, ALIAS_1, EMAIL_1)

        with patch("app.restore_session", new=AsyncMock(return_value=None)):
            with patch("app.random_delay", new=AsyncMock()):
                result = await run_daily_tasks(ctx)

        ctx.manager.renew_cookie_token.assert_not_awaited()
        assert result["accounts"][0]["success"] is False


class TestRedeemTokenRefresh:
    """_redeem_with_token_refresh: UNA renovacion y UN reintento ante -1071, nunca mas."""

    @pytest.mark.asyncio
    async def test_success_on_first_try_never_renews(self, ctx: AppContext) -> None:
        game = MagicMock(redeem_code=AsyncMock(return_value=None))
        await _redeem_with_token_refresh(ctx, _os_account(), game, "CODE")

        game.redeem_code.assert_awaited_once()
        ctx.manager.renew_cookie_token.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_1071_renews_then_retries_once_and_succeeds(self, ctx: AppContext) -> None:
        account = _os_account()
        game = MagicMock(redeem_code=AsyncMock(side_effect=[_hoyo_error(), None]))
        delays: list = []

        async def fake_delay(min_s, max_s):
            delays.append((min_s, max_s))

        with patch("app.random_delay", side_effect=fake_delay):
            await _redeem_with_token_refresh(ctx, account, game, "CODE")

        assert game.redeem_code.await_count == 2
        ctx.manager.renew_cookie_token.assert_awaited_once_with(
            "a@x.com", "os", account["cookies"], "dev-1"
        )
        assert config.DELAY_TOKEN_REFRESH_TO_RETRY in delays, (
            f"Debe haber una pausa antes del reintento. Delays: {delays}"
        )

    @pytest.mark.asyncio
    async def test_failed_renewal_propagates_the_original_error_without_retry(self, ctx: AppContext) -> None:
        ctx.manager.renew_cookie_token = AsyncMock(return_value=False)
        game = MagicMock(redeem_code=AsyncMock(side_effect=_hoyo_error()))

        with patch("app.random_delay", new=AsyncMock()):
            with pytest.raises(HoyoAPIError) as exc:
                await _redeem_with_token_refresh(ctx, _os_account(), game, "CODE")

        assert exc.value.retcode == -1071
        assert game.redeem_code.await_count == 1, "Sin token renovado no tiene sentido reintentar."

    @pytest.mark.asyncio
    async def test_never_more_than_one_retry(self, ctx: AppContext) -> None:
        """Si tras renovar sigue fallando, el error se propaga: nada de bucles contra el servidor."""
        game = MagicMock(redeem_code=AsyncMock(side_effect=_hoyo_error()))

        with patch("app.random_delay", new=AsyncMock()):
            with pytest.raises(HoyoAPIError):
                await _redeem_with_token_refresh(ctx, _os_account(), game, "CODE")

        assert game.redeem_code.await_count == 2
        ctx.manager.renew_cookie_token.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_other_retcodes_never_trigger_a_renewal(self, ctx: AppContext) -> None:
        """-2017 (ya reclamado) es un resultado del CODIGO, no de la sesion: no se renueva nada."""
        game = MagicMock(redeem_code=AsyncMock(side_effect=_hoyo_error(-2017)))

        with pytest.raises(HoyoAPIError) as exc:
            await _redeem_with_token_refresh(ctx, _os_account(), game, "CODE")

        assert exc.value.retcode == -2017
        ctx.manager.renew_cookie_token.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_account_already_renewed_this_run_is_not_renewed_again(self, ctx: AppContext) -> None:
        """
        Con varios codigos pendientes y una cuenta sin remedio, el
        intento de renovar ocurre UNA vez por corrida y cuenta, no una
        vez por cada codigo (eso seria martillar el endpoint de tokens).
        """
        account = _os_account(_token_renewed_this_run=True)
        game = MagicMock(redeem_code=AsyncMock(side_effect=_hoyo_error()))

        with pytest.raises(HoyoAPIError):
            await _redeem_with_token_refresh(ctx, account, game, "CODE")

        ctx.manager.renew_cookie_token.assert_not_awaited()
        assert game.redeem_code.await_count == 1

    @pytest.mark.asyncio
    async def test_manual_redeem_recovers_and_records_success(self, ctx: AppContext) -> None:
        _seed_account(ctx.account_store, "Cuenta OS", "os1@x.com")
        game = MagicMock(game_biz="hk4e_global", GAME_NAME="Genshin Impact", uid="1")
        game.redeem_code = AsyncMock(side_effect=[_hoyo_error(), None])
        active = MagicMock(spec=ActiveSession)
        active.get_game = lambda biz: game

        with patch("app.restore_session", new=AsyncMock(return_value=active)):
            with patch("app.random_delay", new=AsyncMock()):
                result = await redeem_code_for_all(ctx, "hk4e_global", "RECOVERED")

        assert result[0]["success"] is True
        ctx.manager.renew_cookie_token.assert_awaited_once()
        code_id = ctx.code_store.get_or_create_code_id("hk4e_global", "RECOVERED")
        assert ctx.code_store.has_redeemed(code_id, "Cuenta OS") is True

    @pytest.mark.asyncio
    async def test_auto_redeem_renews_once_for_several_codes(self, ctx: AppContext) -> None:
        _seed_account(ctx.account_store, "Alice", "alice@x.com")
        calls: list[str] = []

        async def redeem(code, lang="en"):
            calls.append(code)
            if len(calls) == 1:
                raise _hoyo_error()

        game = MagicMock(game_biz="hk4e_global", GAME_NAME="Genshin Impact", uid="1")
        game.redeem_code = redeem
        active = MagicMock(spec=ActiveSession, games=[game])
        for code in ("CODEONE", "CODETWO"):
            await add_gift_code(ctx, "hk4e_global", code)

        with patch("app.restore_session", new=AsyncMock(return_value=active)):
            with patch("app.random_delay", new=AsyncMock()):
                results = await redeem_pending_codes(ctx)

        assert len(results) == 2 and all(r["success"] for r in results)
        assert calls[0] == calls[1], f"El primer codigo debia reintentarse. Llamadas: {calls}"
        assert len(calls) == 3
        ctx.manager.renew_cookie_token.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unrecoverable_1071_stays_pending_and_tells_the_user_what_to_do(self, ctx: AppContext) -> None:
        """
        Si ni renovando sirve, el codigo NO se descarta (sigue pendiente:
        el problema es la sesion, no el codigo) y el mensaje dice que hacer.
        """
        _seed_account(ctx.account_store, "Alice", "alice@x.com")
        game = MagicMock(game_biz="hk4e_global", GAME_NAME="Genshin Impact", uid="1")
        game.redeem_code = AsyncMock(side_effect=_hoyo_error())
        active = MagicMock(spec=ActiveSession, games=[game])
        await add_gift_code(ctx, "hk4e_global", "STILLGOOD")

        with patch("app.restore_session", new=AsyncMock(return_value=active)):
            with patch("app.random_delay", new=AsyncMock()):
                results = await redeem_pending_codes(ctx)

        assert results[0]["success"] is False
        assert "Reingresar contrasena" in results[0]["message"]
        assert len(ctx.code_store.get_pending_for_account("Alice", "hk4e_global")) == 1, (
            "Un fallo de sesion no debe descartar el codigo ni marcarlo para esa cuenta."
        )


# ---------------------------------------------------------------------------
# Medicion de la vida util del cookie_token_v2
#
# Cada canje demuestra si el token servia y con cuantas horas de edad. Con
# esos puntos, get_token_report() deduce el intervalo MAXIMO seguro entre
# corridas de tareas diarias: el dato que ni genshin.py ni HoYoverse dan.
# ---------------------------------------------------------------------------

def _hours_ago(hours: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def _issued(ctx: AppContext, hours_ago: float, email: str = "a@x.com") -> None:
    ctx.account_store.log_session_event(
        email, "os", config.TOKEN_EVENT_ISSUED, created_at=_hours_ago(hours_ago)
    )


def _events(ctx: AppContext, event: str) -> list[dict]:
    return ctx.account_store.list_session_events(event=event)


class TestTokenUseMeasurement:

    @pytest.mark.asyncio
    async def test_a_working_redeem_records_ok_with_the_token_age(self, ctx: AppContext) -> None:
        _issued(ctx, 5)
        game = MagicMock(redeem_code=AsyncMock(return_value=None))

        await _redeem_with_token_refresh(ctx, _os_account(), game, "CODE")

        (event,) = _events(ctx, config.TOKEN_EVENT_USE_OK)
        assert event["age_hours"] == pytest.approx(5.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_a_code_level_error_still_proves_the_token_works(self, ctx: AppContext) -> None:
        """
        -2017 ("ya reclamado") lo responde el servidor DESPUES de aceptar la
        sesion: el token servia. Contarlo como fallo falsearia la medicion.
        """
        _issued(ctx, 8)
        game = MagicMock(redeem_code=AsyncMock(side_effect=_hoyo_error(-2017)))

        with pytest.raises(HoyoAPIError):
            await _redeem_with_token_refresh(ctx, _os_account(), game, "CODE")

        assert len(_events(ctx, config.TOKEN_EVENT_USE_OK)) == 1
        assert _events(ctx, config.TOKEN_EVENT_USE_FAIL) == []

    @pytest.mark.asyncio
    async def test_a_session_failure_records_the_age_it_had_when_it_expired(self, ctx: AppContext) -> None:
        """EL DATO CLAVE: 'el token ya habia vencido con 72 h de edad' => no hay que pasar de ahi."""
        _issued(ctx, 72)
        game = MagicMock(redeem_code=AsyncMock(side_effect=[_hoyo_error(), None]))

        with patch("app.random_delay", new=AsyncMock()):
            await _redeem_with_token_refresh(ctx, _os_account(), game, "CODE")

        (event,) = _events(ctx, config.TOKEN_EVENT_USE_FAIL)
        assert event["age_hours"] == pytest.approx(72.0, abs=0.1)
        assert _events(ctx, config.TOKEN_EVENT_USE_OK) == [], (
            "El reintento tras renovar corre con un token de segundos de edad: no es un dato de vida util."
        )

    @pytest.mark.asyncio
    async def test_only_the_first_outcome_per_account_and_run_is_recorded(self, ctx: AppContext) -> None:
        """Cinco codigos seguidos no deben llenar la tabla con cinco puntos casi identicos."""
        _issued(ctx, 2)
        account = _os_account()
        game = MagicMock(redeem_code=AsyncMock(return_value=None))

        for code in ("A", "B", "C"):
            await _redeem_with_token_refresh(ctx, account, game, code)

        assert len(_events(ctx, config.TOKEN_EVENT_USE_OK)) == 1

    @pytest.mark.asyncio
    async def test_unknown_age_is_recorded_as_unknown(self, ctx: AppContext) -> None:
        """Cuenta anterior a la medicion (sin evento de emision): se anota igual, con edad desconocida."""
        game = MagicMock(redeem_code=AsyncMock(return_value=None))
        await _redeem_with_token_refresh(ctx, _os_account(), game, "CODE")

        (event,) = _events(ctx, config.TOKEN_EVENT_USE_OK)
        assert event["age_hours"] is None

    @pytest.mark.asyncio
    async def test_cn_accounts_are_not_measured(self, ctx: AppContext) -> None:
        game = MagicMock(redeem_code=AsyncMock(return_value=None))
        await _redeem_with_token_refresh(ctx, _os_account(region="cn"), game, "CODE")
        assert ctx.account_store.list_session_events() == []

    @pytest.mark.asyncio
    async def test_a_measurement_failure_never_breaks_the_redeem(self, ctx: AppContext) -> None:
        game = MagicMock(redeem_code=AsyncMock(return_value=None))
        with patch.object(ctx.account_store, "log_session_event", side_effect=RuntimeError("disco lleno")):
            await _redeem_with_token_refresh(ctx, _os_account(), game, "CODE")  # no debe lanzar

        game.redeem_code.assert_awaited_once()


class TestTokenReport:

    @staticmethod
    def _register(ctx: AppContext, alias: str, email: str, *, renewable: bool, region: str = "os") -> None:
        cookies = {"ltuid_v2": "1", "ltoken_v2": "lt", "cookie_token_v2": "ct"}
        if renewable:
            cookies.update({"stoken": "st", "ltmid_v2": "mid"})
        ctx.account_store.set(email, region, cookies, "dev")
        ctx.account_store.set_alias(email, region, alias)

    def test_no_accounts_gives_an_empty_report(self, ctx: AppContext) -> None:
        assert get_token_report(ctx) == {
            "accounts": [], "safe_interval_hours": None, "max_ok_age_hours": None,
        }

    def test_flags_accounts_that_cannot_renew_themselves(self, ctx: AppContext) -> None:
        """Una cuenta registrada por login web (sin stoken) lo muestra: ahi hace falta 'Reingresar contrasena'."""
        self._register(ctx, "vieja", "old@x.com", renewable=False)
        self._register(ctx, "nueva", "new@x.com", renewable=True)

        rows = {r["alias"]: r for r in get_token_report(ctx)["accounts"]}

        assert rows["vieja"]["can_renew"] is False
        assert rows["nueva"]["can_renew"] is True
        assert rows["vieja"]["has_cookie_token"] is True

    def test_cn_accounts_are_excluded(self, ctx: AppContext) -> None:
        self._register(ctx, "china", "cn@x.com", renewable=True, region="cn")
        assert get_token_report(ctx)["accounts"] == []

    def test_summarizes_ok_and_failed_ages_per_account(self, ctx: AppContext) -> None:
        self._register(ctx, "main", "a@x.com", renewable=True)
        _issued(ctx, 3)
        for age in (2.0, 20.0):
            ctx.account_store.log_session_event("a@x.com", "os", config.TOKEN_EVENT_USE_OK, age_hours=age)
        ctx.account_store.log_session_event("a@x.com", "os", config.TOKEN_EVENT_USE_FAIL, age_hours=70.0)

        report = get_token_report(ctx)
        (row,) = report["accounts"]

        assert row["age_hours"] == pytest.approx(3.0, abs=0.1)
        assert (row["ok_count"], row["fail_count"]) == (2, 1)
        assert row["max_ok_age_hours"] == 20.0 and row["min_fail_age_hours"] == 70.0
        assert report["max_ok_age_hours"] == 20.0 and report["safe_interval_hours"] == 70.0

    def test_safe_interval_is_the_smallest_age_at_which_any_token_expired(self, ctx: AppContext) -> None:
        """
        El limite seguro lo marca el token que vencio MAS JOVEN, en cualquier
        cuenta: si uno vencio a las 48 h, pasar de 48 h es arriesgado aunque
        otro haya aguantado 70.
        """
        self._register(ctx, "uno", "a@x.com", renewable=True)
        self._register(ctx, "dos", "b@x.com", renewable=True)
        ctx.account_store.log_session_event("a@x.com", "os", config.TOKEN_EVENT_USE_FAIL, age_hours=70.0)
        ctx.account_store.log_session_event("b@x.com", "os", config.TOKEN_EVENT_USE_FAIL, age_hours=48.0)

        assert get_token_report(ctx)["safe_interval_hours"] == 48.0

    def test_unknown_ages_do_not_pollute_the_summary(self, ctx: AppContext) -> None:
        """Un fallo con edad desconocida existe (cuenta) pero no puede entrar en un minimo."""
        self._register(ctx, "main", "a@x.com", renewable=True)
        ctx.account_store.log_session_event("a@x.com", "os", config.TOKEN_EVENT_USE_FAIL, age_hours=None)

        report = get_token_report(ctx)

        assert report["safe_interval_hours"] is None
        assert report["accounts"][0]["fail_count"] == 0

    def test_no_failures_yet_means_no_safe_interval_claimed(self, ctx: AppContext) -> None:
        """Sin ningun vencimiento observado NO se afirma un limite: solo cuanto aguanto como minimo."""
        self._register(ctx, "main", "a@x.com", renewable=True)
        ctx.account_store.log_session_event("a@x.com", "os", config.TOKEN_EVENT_USE_OK, age_hours=22.0)

        report = get_token_report(ctx)

        assert report["safe_interval_hours"] is None
        assert report["max_ok_age_hours"] == 22.0


# ---------------------------------------------------------------------------
# Fallos HTTP/de red (404, 5xx, timeout) — no deben abortar la corrida
#
# Incidente real (20/11/2026): el index del Mimo de Genshin respondio 404.
# session.request deja pasar aiohttp.ClientResponseError (a proposito, para que
# tenacity reintente los 429) y app.py solo capturaba HoyoAPIError: la
# excepcion subio hasta main.py y se perdieron los juegos y cuentas que faltaban.
# ---------------------------------------------------------------------------

def _http_error(status: int = 404, message: str = "Not Found") -> aiohttp.ClientResponseError:
    """Un fallo HTTP como el que lanza session.request via raise_for_status()."""
    return aiohttp.ClientResponseError(
        request_info=MagicMock(), history=(), status=status, message=message,
    )


def _daily_game(name: str = "Genshin Impact", biz: str = "hk4e_global", uid: str = "700000001") -> MagicMock:
    """Juego mockeado con todo en orden: check-in hecho, sin tareas Mimo ni compras."""
    game = MagicMock()
    game.GAME_NAME = name
    game.game_biz  = biz
    game.uid       = uid
    game.claim_daily_reward = AsyncMock(return_value={
        "message": "ok", "already_done": True, "total_sign_day": 3,
    })
    game.get_mimo_tasks     = AsyncMock(return_value=[])
    game.finish_mimo_task   = AsyncMock(return_value=None)
    game.claim_mimo_task_reward = AsyncMock(return_value=None)
    game.buy_mimo_currency  = AsyncMock(return_value=[])
    return game


async def _run_daily_with_games(ctx: AppContext, games: list) -> dict:
    _seed_account(ctx.account_store, ALIAS_1, EMAIL_1)
    mock_active = MagicMock(spec=ActiveSession)
    mock_active.games = games
    with patch("app.restore_session", new=AsyncMock(return_value=mock_active)):
        with patch("app.random_delay", new=AsyncMock()):
            return await run_daily_tasks(ctx)


class TestHttpErrorsDoNotAbortTheDailyRun:

    @pytest.mark.asyncio
    async def test_checkin_http_error_is_reported_and_the_run_continues(self, ctx: AppContext) -> None:
        game_1 = _daily_game("Genshin Impact", "hk4e_global")
        game_2 = _daily_game("Honkai: Star Rail", "hkrpg_global", uid="800000001")
        game_1.claim_daily_reward = AsyncMock(side_effect=_http_error(404))

        result = await _run_daily_with_games(ctx, [game_1, game_2])

        account = result["accounts"][0]
        assert account["success"] is True, "Un 404 en el check-in no debe romper la cuenta."
        checkins = {t["game_name"]: t for t in account["tasks"] if t["type"] == "checkin"}
        assert checkins["Genshin Impact"]["success"] is False
        assert "404" in checkins["Genshin Impact"]["message"], (
            f"El mensaje debe decir el estado HTTP. Obtenido: {checkins['Genshin Impact']['message']!r}"
        )
        assert checkins["Honkai: Star Rail"]["success"] is True, (
            "El juego siguiente debe procesarse aunque el anterior fallo."
        )
        game_2.claim_daily_reward.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unreachable_mimo_is_reported_skips_the_shop_and_continues(self, ctx: AppContext) -> None:
        """
        El caso del log: el listado de tareas da 404. La tienda empezaria por
        la misma request (el index no quedo en cache) y volveria a fallar:
        NO debe intentarse. El juego siguiente si se procesa completo.
        """
        game_1 = _daily_game("Genshin Impact", "hk4e_global")
        game_2 = _daily_game("Honkai: Star Rail", "hkrpg_global", uid="800000001")
        game_1.get_mimo_tasks = AsyncMock(side_effect=_http_error(404))

        result = await _run_daily_with_games(ctx, [game_1, game_2])

        tasks = result["accounts"][0]["tasks"]
        mimo_failures = [
            t for t in tasks
            if t["type"] == "mimo_task" and t["game_name"] == "Genshin Impact" and not t["success"]
        ]
        assert len(mimo_failures) == 1 and "HTTP 404" in mimo_failures[0]["message"], (
            f"Debe quedar UN fallo de Mimo con el estado HTTP. Tareas: {tasks}"
        )
        game_1.get_mimo_tasks.assert_awaited_once()
        game_1.buy_mimo_currency.assert_not_awaited()
        game_2.get_mimo_tasks.assert_awaited_once()
        game_2.buy_mimo_currency.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unreachable_mimo_flag_is_set_only_for_http_or_network_errors(self) -> None:
        """Un retcode de la API (HoyoAPIError) conserva el comportamiento anterior: no marca 'unreachable'."""
        api_game = _daily_game()
        api_game.get_mimo_tasks = AsyncMock(side_effect=HoyoAPIError(-100, "x", {}))
        http_game = _daily_game()
        http_game.get_mimo_tasks = AsyncMock(side_effect=_http_error(503, "Service Unavailable"))
        api_state: dict = {}
        http_state: dict = {}

        await _process_mimo_tasks(api_game, state=api_state)
        await _process_mimo_tasks(http_game, state=http_state)

        assert api_state == {}, "Un HoyoAPIError no debe marcar el Mimo como inalcanzable."
        assert http_state == {"unreachable": True}, "Un 503 si debe marcarlo."

    @pytest.mark.asyncio
    async def test_finish_http_error_stops_the_remaining_finishes(self) -> None:
        """ANTI-BANEO: si finish-task no responde, no se lo manda una vez por tarea."""
        game = _daily_game()
        game.get_mimo_tasks = AsyncMock(return_value=[
            {"task_id": 1, "task_name": "Visitar A", "status": 2, "task_type": 2},
            {"task_id": 2, "task_name": "Visitar B", "status": 2, "task_type": 2},
            {"task_id": 3, "task_name": "Visitar C", "status": 2, "task_type": 2},
        ])
        game.finish_mimo_task = AsyncMock(side_effect=_http_error(500, "Internal Server Error"))

        with patch("app.random_delay", new=AsyncMock()):
            result = await _process_mimo_tasks(game)

        assert game.finish_mimo_task.await_count == 1, (
            f"Tras el primer fallo HTTP no debe seguir. Intentos: {game.finish_mimo_task.await_count}"
        )
        failures = [r for r in result if not r["success"]]
        assert len(failures) == 1 and "HTTP 500" in failures[0]["message"]

    @pytest.mark.asyncio
    async def test_api_errors_on_finish_still_try_every_task(self) -> None:
        """Preservacion: un retcode de la API NO corta el paso (es por tarea, no del endpoint)."""
        game = _daily_game()
        game.get_mimo_tasks = AsyncMock(return_value=[
            {"task_id": 1, "task_name": "Visitar A", "status": 2, "task_type": 2},
            {"task_id": 2, "task_name": "Visitar B", "status": 2, "task_type": 2},
        ])
        game.finish_mimo_task = AsyncMock(side_effect=HoyoAPIError(-5, "no se puede", {}))

        with patch("app.random_delay", new=AsyncMock()):
            await _process_mimo_tasks(game)

        assert game.finish_mimo_task.await_count == 2

    @pytest.mark.asyncio
    async def test_claim_http_error_stops_the_remaining_claims(self) -> None:
        game = _daily_game()
        game.get_mimo_tasks = AsyncMock(return_value=[
            {"task_id": 1, "task_name": "Tarea A", "status": 1, "task_type": 5},
            {"task_id": 2, "task_name": "Tarea B", "status": 1, "task_type": 5},
        ])
        game.claim_mimo_task_reward = AsyncMock(side_effect=_http_error(502, "Bad Gateway"))

        with patch("app.random_delay", new=AsyncMock()):
            result = await _process_mimo_tasks(game)

        assert game.claim_mimo_task_reward.await_count == 1
        assert len(result) == 1 and result[0]["success"] is False
        assert "HTTP 502" in result[0]["message"]

    @pytest.mark.asyncio
    async def test_shop_http_error_is_reported_not_raised(self) -> None:
        game = _daily_game()
        game.buy_mimo_currency = AsyncMock(side_effect=_http_error(503, "Service Unavailable"))

        result = await _process_mimo_buys(game)

        assert len(result) == 1 and result[0]["success"] is False
        assert "HTTP 503" in result[0]["message"]

    @pytest.mark.asyncio
    async def test_timeout_is_handled_like_any_other_network_error(self) -> None:
        game = _daily_game()
        game.buy_mimo_currency = AsyncMock(side_effect=asyncio.TimeoutError())

        result = await _process_mimo_buys(game)

        assert len(result) == 1 and result[0]["success"] is False
        assert "TimeoutError" in result[0]["message"], (
            f"Un timeout trae str() vacio: el mensaje debe nombrar el tipo. Obtenido: {result[0]['message']!r}"
        )

    def test_error_text_does_not_include_the_url(self) -> None:
        """str(ClientResponseError) trae la URL completa; el texto para el resumen y el log, no."""
        request_info = MagicMock()
        request_info.real_url = "https://example.invalid/api?token=SECRETO"
        err = aiohttp.ClientResponseError(
            request_info=request_info, history=(), status=404, message="Not Found",
        )

        text = _transport_error_text(err)

        assert text == "HTTP 404 Not Found", f"Obtenido: {text!r}"
        assert "SECRETO" not in text and "example.invalid" not in text


# ---------------------------------------------------------------------------
# Canje de codigos ante fallos HTTP/de red
# ---------------------------------------------------------------------------

def _pending_aliases(ctx: AppContext, code: str, aliases: list[str], biz: str = "hk4e_global") -> list[str]:
    """Alias para los que `code` sigue pendiente de canjear."""
    return [
        a for a in aliases
        if any(r["code"] == code for r in ctx.code_store.get_pending_for_account(a, biz))
    ]


class TestHttpErrorsDuringRedeem:

    @pytest.mark.asyncio
    async def test_token_refresh_ignores_http_errors(self, ctx: AppContext) -> None:
        """Un 404/timeout no dice nada del token: no renueva, no reintenta, propaga tal cual."""
        game = MagicMock(redeem_code=AsyncMock(side_effect=_http_error(404)))

        with patch("app.random_delay", new=AsyncMock()):
            with pytest.raises(aiohttp.ClientResponseError):
                await _redeem_with_token_refresh(ctx, _os_account(), game, "CODE")

        assert game.redeem_code.await_count == 1, "No debe reintentarse un fallo HTTP."
        ctx.manager.renew_cookie_token.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_manual_redeem_http_error_saves_nothing_and_stops(self, ctx: AppContext) -> None:
        _seed_account(ctx.account_store, "Alice", "alice@example.com")
        _seed_account(ctx.account_store, "Bob", "bob@example.com")
        game_alice, game_bob = _daily_game(uid="1"), _daily_game(uid="2")
        game_alice.redeem_code = AsyncMock(side_effect=_http_error(404))
        game_bob.redeem_code   = AsyncMock(return_value=None)
        sessions = {
            "Alice": MagicMock(spec=ActiveSession, get_game=lambda biz: game_alice),
            "Bob":   MagicMock(spec=ActiveSession, get_game=lambda biz: game_bob),
        }

        async def fake_restore(ctx, account):
            return sessions[account["alias"]]

        with patch("app.restore_session", side_effect=fake_restore):
            with patch("app.random_delay", new=AsyncMock()):
                results = await redeem_code_for_all(ctx, "hk4e_global", "CODIGO1")

        assert len(results) == 1 and results[0]["success"] is False, (
            f"Debe quedar un solo fallo (se corta). Resultados: {results}"
        )
        assert "HTTP 404" in results[0]["message"]
        game_bob.redeem_code.assert_not_awaited()
        code_row = next(c for c in ctx.code_store.list_codes("hk4e_global") if c["code"] == "CODIGO1")
        assert code_row["status"] == "active", "Un fallo de red jamas marca un codigo como muerto."
        assert _pending_aliases(ctx, "CODIGO1", ["Alice", "Bob"]) == ["Alice", "Bob"], (
            "El codigo debe seguir pendiente para ambas cuentas (no se guardo ningun resultado)."
        )

    @pytest.mark.asyncio
    async def test_auto_redeem_http_error_saves_nothing_and_stops_everything(self, ctx: AppContext) -> None:
        """
        ANTI-BANEO: ante un fallo de infraestructura el canje automatico se corta
        completo (resto de codigos, juegos y cuentas); nada queda guardado, asi
        que todo se reintenta en la proxima corrida.
        """
        _seed_account(ctx.account_store, "Alice", "alice@example.com")
        _seed_account(ctx.account_store, "Bob", "bob@example.com")
        game_alice, game_bob = _daily_game(uid="1"), _daily_game(uid="2")
        game_alice.redeem_code = AsyncMock(side_effect=_http_error(503, "Service Unavailable"))
        game_bob.redeem_code   = AsyncMock(return_value=None)
        sessions = {
            "Alice": MagicMock(spec=ActiveSession, games=[game_alice]),
            "Bob":   MagicMock(spec=ActiveSession, games=[game_bob]),
        }

        async def fake_restore(ctx, account):
            return sessions[account["alias"]]

        for code in ["CODIGO1", "CODIGO2"]:
            await add_gift_code(ctx, "hk4e_global", code)

        with patch("app.restore_session", side_effect=fake_restore):
            with patch("app.random_delay", new=AsyncMock()):
                results = await redeem_pending_codes(ctx)

        assert game_alice.redeem_code.await_count == 1, "Tras el fallo no debe probar el 2.o codigo."
        game_bob.redeem_code.assert_not_awaited()
        assert len(results) == 1 and results[0]["success"] is False
        assert "HTTP 503" in results[0]["message"]
        for code in ["CODIGO1", "CODIGO2"]:
            assert _pending_aliases(ctx, code, ["Alice", "Bob"]) == ["Alice", "Bob"], (
                f"{code} debe seguir pendiente para ambas cuentas."
            )
        statuses = {c["code"]: c["status"] for c in ctx.code_store.list_codes("hk4e_global")}
        assert statuses == {"CODIGO1": "active", "CODIGO2": "active"}, f"Estados: {statuses}"


# ---------------------------------------------------------------------------
# Seleccion de cuentas y juegos para las tareas diarias
# ---------------------------------------------------------------------------

class TestParseIndexSelection:

    @pytest.mark.parametrize("text, count, expected", [
        ("1",        3, [0]),
        ("1,3",      3, [0, 2]),
        ("1 3",      3, [0, 2]),
        ("1, 3",     3, [0, 2]),
        ("1-3",      3, [0, 1, 2]),
        ("1, 3-4",   5, [0, 2, 3]),
        ("3,1",      3, [0, 2]),     # orden de entrada irrelevante: sale ordenado
        ("2,2,2",    3, [1]),        # sin repetidos
        ("  2  ",    3, [1]),
        ("0",        3, []),         # "ninguna / cancelar"
    ])
    def test_valid_selections(self, text: str, count: int, expected: list[int]) -> None:
        assert parse_index_selection(text, count) == expected, (
            f"{text!r} con {count} opciones debia dar {expected}."
        )

    @pytest.mark.parametrize("text", [
        "",          # vacio: quien llama decide que significa
        "   ",
        "4",         # fuera de rango (hay 3)
        "-1",
        "1-4",       # rango que se sale
        "3-1",       # rango invertido
        "a",
        "1,a",
        "1-",
        "-3",
        "1-2-3",
        "0,1",       # "0" no se mezcla con otros numeros
        "1.5",
        "\u00b2",   # digito unicode que int() no acepta
    ])
    def test_invalid_selections_return_none(self, text: str) -> None:
        assert parse_index_selection(text, 3) is None, (
            f"{text!r} no es una seleccion valida de 3 opciones y debe dar None."
        )

    def test_huge_range_is_rejected_without_iterating_it(self) -> None:
        """'1-999999999' se descarta por limites, no recorriendo el rango."""
        assert parse_index_selection("1-999999999", 3) is None


class TestDescribeGame:

    def test_includes_name_nickname_uid_and_server(self) -> None:
        game = MagicMock(GAME_NAME="Genshin Impact", nickname="Viajero", uid="700000001",
                         server_name="America", server="os_usa")
        assert describe_game(game) == "Genshin Impact - Viajero (UID 700000001, America)"

    def test_falls_back_to_server_code_and_omits_empty_nickname(self) -> None:
        game = MagicMock(GAME_NAME="Honkai: Star Rail", nickname="", uid="800000001",
                         server_name="", server="prod_official_usa")
        assert describe_game(game) == "Honkai: Star Rail (UID 800000001, prod_official_usa)"


def _two_games() -> tuple[MagicMock, MagicMock]:
    return (
        _daily_game("Genshin Impact", "hk4e_global", uid="1"),
        _daily_game("Honkai: Star Rail", "hkrpg_global", uid="2"),
    )


async def _run_daily_multi(
    ctx: AppContext,
    games_by_alias: dict[str, list],
    **kwargs,
) -> tuple[dict, AsyncMock, AsyncMock]:
    """
    Registra una cuenta por alias (en ese orden) y corre run_daily_tasks.
    Devuelve (resultado, mock de restore_session, mock de random_delay).
    """
    for alias in games_by_alias:
        _seed_account(ctx.account_store, alias, f"{alias.lower()}@example.com")
    actives = {}
    for alias, games in games_by_alias.items():
        active = MagicMock(spec=ActiveSession)
        active.games = games
        actives[alias] = active

    async def fake_restore(ctx_, account):
        return actives[account["alias"]]

    restore = AsyncMock(side_effect=fake_restore)
    delay = AsyncMock()
    with patch("app.restore_session", new=restore), patch("app.random_delay", new=delay):
        result = await run_daily_tasks(ctx, **kwargs)
    return result, restore, delay


def _delays_of(delay_mock: AsyncMock, which: tuple) -> int:
    return sum(1 for c in delay_mock.call_args_list if c.args == which)


class TestDailyTasksAccountSelection:

    @pytest.mark.asyncio
    async def test_without_arguments_every_account_is_processed(self, ctx: AppContext) -> None:
        """Preservacion: el comportamiento de siempre (todo) no cambia."""
        result, restore, _ = await _run_daily_multi(ctx, {
            "Alice": [_daily_game()], "Bob": [_daily_game()], "Carol": [_daily_game()],
        })
        assert [a["alias"] for a in result["accounts"]] == ["Alice", "Bob", "Carol"]
        assert restore.await_count == 3

    @pytest.mark.asyncio
    async def test_only_the_chosen_accounts_are_restored_and_processed(self, ctx: AppContext) -> None:
        g_alice, g_bob, g_carol = _daily_game(), _daily_game(), _daily_game()
        result, restore, _ = await _run_daily_multi(
            ctx, {"Alice": [g_alice], "Bob": [g_bob], "Carol": [g_carol]}, aliases=["Bob"],
        )
        assert [a["alias"] for a in result["accounts"]] == ["Bob"]
        assert restore.await_count == 1, (
            "Una cuenta no elegida no debe ni restaurar sesion (cada restore son requests)."
        )
        g_bob.claim_daily_reward.assert_awaited_once()
        g_alice.claim_daily_reward.assert_not_awaited()
        g_carol.claim_daily_reward.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_chosen_accounts_keep_registry_order_and_delay_applies_between_them(
        self, ctx: AppContext,
    ) -> None:
        result, _, delay = await _run_daily_multi(ctx, {
            "Alice": [_daily_game()], "Bob": [_daily_game()], "Carol": [_daily_game()],
        }, aliases=["Carol", "Alice"])   # el orden pedido no importa
        assert [a["alias"] for a in result["accounts"]] == ["Alice", "Carol"]
        assert _delays_of(delay, config.DELAY_BETWEEN_ACCOUNTS) == 1, (
            "Con 2 cuentas elegidas hay UNA pausa entre ellas (anti-deteccion), "
            "no una por cuenta registrada."
        )

    @pytest.mark.asyncio
    async def test_unknown_aliases_are_ignored(self, ctx: AppContext) -> None:
        result, restore, _ = await _run_daily_multi(
            ctx, {"Alice": [_daily_game()]}, aliases=["Alice", "NoExiste"],
        )
        assert [a["alias"] for a in result["accounts"]] == ["Alice"]
        assert restore.await_count == 1

    @pytest.mark.asyncio
    async def test_no_matching_account_means_nothing_to_do(self, ctx: AppContext) -> None:
        result, restore, _ = await _run_daily_multi(
            ctx, {"Alice": [_daily_game()]}, aliases=["NoExiste"],
        )
        assert result == {"accounts": []}
        restore.assert_not_awaited()


class TestDailyTasksGameSelection:

    @pytest.mark.asyncio
    async def test_callback_receives_alias_and_detected_games(self, ctx: AppContext) -> None:
        g1, g2 = _two_games()
        seen: list[tuple] = []

        async def chooser(alias, games):
            seen.append((alias, list(games)))
            return games

        await _run_daily_multi(ctx, {"Alice": [g1, g2]}, select_games=chooser)

        assert seen == [("Alice", [g1, g2])]

    @pytest.mark.asyncio
    async def test_only_the_chosen_games_are_processed(self, ctx: AppContext) -> None:
        g1, g2 = _two_games()
        g3 = _daily_game("Zenless Zone Zero", "nap_global", uid="3")

        async def chooser(alias, games):
            return [games[0], games[2]]   # Genshin y ZZZ; HSR no

        result, _, delay = await _run_daily_multi(ctx, {"Alice": [g1, g2, g3]}, select_games=chooser)

        g1.claim_daily_reward.assert_awaited_once()
        g3.claim_daily_reward.assert_awaited_once()
        g2.claim_daily_reward.assert_not_awaited()
        g2.get_mimo_tasks.assert_not_awaited()
        g2.buy_mimo_currency.assert_not_awaited()
        names = {t["game_name"] for t in result["accounts"][0]["tasks"]}
        assert names == {"Genshin Impact", "Zenless Zone Zero"}
        assert _delays_of(delay, config.DELAY_BETWEEN_GAMES) == 1, (
            "La pausa entre juegos se cuenta sobre los juegos ELEGIDOS (2 -> 1 pausa), "
            "no sobre los detectados."
        )

    @pytest.mark.asyncio
    async def test_games_run_in_detection_order_whatever_the_callback_returns(
        self, ctx: AppContext,
    ) -> None:
        g1, g2 = _two_games()
        order: list[str] = []
        g1.claim_daily_reward = AsyncMock(side_effect=lambda **k: order.append("g1") or {
            "message": "ok", "already_done": True, "total_sign_day": 1})
        g2.claim_daily_reward = AsyncMock(side_effect=lambda **k: order.append("g2") or {
            "message": "ok", "already_done": True, "total_sign_day": 1})

        async def chooser(alias, games):
            return [games[1], games[0]]   # al reves

        await _run_daily_multi(ctx, {"Alice": [g1, g2]}, select_games=chooser)

        assert order == ["g1", "g2"]

    @pytest.mark.parametrize("answer", [[], None])
    @pytest.mark.asyncio
    async def test_no_games_chosen_skips_the_account_without_renewing_or_running(
        self, ctx: AppContext, answer,
    ) -> None:
        g1, _ = _two_games()

        async def chooser(alias, games):
            return answer

        result, _, _ = await _run_daily_multi(ctx, {"Alice": [g1]}, select_games=chooser)

        account = result["accounts"][0]
        assert account["success"] is True and account["tasks"] == []
        assert account["message"] == "Sin juegos seleccionados para esta cuenta."
        g1.claim_daily_reward.assert_not_awaited()
        ctx.manager.renew_cookie_token.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skipped_account_does_not_stop_the_next_one(self, ctx: AppContext) -> None:
        g_alice = _daily_game()
        g_bob = _daily_game()

        async def chooser(alias, games):
            return [] if alias == "Alice" else games

        result, _, _ = await _run_daily_multi(
            ctx, {"Alice": [g_alice], "Bob": [g_bob]}, select_games=chooser,
        )

        assert [a["tasks"] != [] for a in result["accounts"]] == [False, True]
        g_alice.claim_daily_reward.assert_not_awaited()
        g_bob.claim_daily_reward.assert_awaited_once()
        assert ctx.manager.renew_cookie_token.await_count == 1, (
            "El token se renueva solo para la cuenta que si corre."
        )

    @pytest.mark.asyncio
    async def test_games_not_belonging_to_the_account_are_ignored(self, ctx: AppContext) -> None:
        g1, _ = _two_games()
        stranger = _daily_game("Ajeno", "otro", uid="9")

        async def chooser(alias, games):
            return [stranger]

        result, _, _ = await _run_daily_multi(ctx, {"Alice": [g1]}, select_games=chooser)

        stranger.claim_daily_reward.assert_not_awaited()
        g1.claim_daily_reward.assert_not_awaited()
        assert result["accounts"][0]["tasks"] == []

    @pytest.mark.asyncio
    async def test_callback_is_not_called_when_the_session_cannot_be_restored(
        self, ctx: AppContext,
    ) -> None:
        _seed_account(ctx.account_store, ALIAS_1, EMAIL_1)
        chooser = AsyncMock(return_value=[])
        with patch("app.restore_session", new=AsyncMock(return_value=None)):
            with patch("app.random_delay", new=AsyncMock()):
                result = await run_daily_tasks(ctx, select_games=chooser)

        chooser.assert_not_awaited()
        assert result["accounts"][0]["success"] is False

    @pytest.mark.asyncio
    async def test_callback_is_not_called_for_an_account_without_games(self, ctx: AppContext) -> None:
        chooser = AsyncMock(return_value=[])
        await _run_daily_multi(ctx, {"Alice": []}, select_games=chooser)
        chooser.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_account_and_game_selection_work_together(self, ctx: AppContext) -> None:
        g_alice_1, g_alice_2 = _two_games()
        g_bob_1, g_bob_2 = _two_games()

        async def chooser(alias, games):
            return [games[1]]   # solo el 2.o juego de cada cuenta elegida

        result, _, _ = await _run_daily_multi(
            ctx,
            {"Alice": [g_alice_1, g_alice_2], "Bob": [g_bob_1, g_bob_2], "Carol": [_daily_game()]},
            aliases=["Alice", "Bob"], select_games=chooser,
        )

        assert [a["alias"] for a in result["accounts"]] == ["Alice", "Bob"]
        g_alice_2.claim_daily_reward.assert_awaited_once()
        g_bob_2.claim_daily_reward.assert_awaited_once()
        g_alice_1.claim_daily_reward.assert_not_awaited()
        g_bob_1.claim_daily_reward.assert_not_awaited()
