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

from unittest.mock import AsyncMock, MagicMock, patch
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

    async def fake_login(email, password, *, region=None):
        account_store.set(email, region or "os", dict(TEST_COOKIES_OS), TEST_DEVICE_ID)
        return mock_active

    manager.login = AsyncMock(side_effect=fake_login)
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
