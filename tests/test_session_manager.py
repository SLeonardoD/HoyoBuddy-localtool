"""
tests/test_session_manager.py
-----------------------------
Tests de HoyoWrapper/session_manager.py.

Verifica: creación de ActiveSession, filtrado de juegos no soportados,
checkin_all con delays, manejo de errores por juego y get_game().
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch, call

import pytest

from HoyoWrapper.session import HoyoSession, HoyoAPIError, LoginError
from HoyoWrapper.session_manager import ActiveSession, HoyoManager
from HoyoWrapper.game_model import create_game
from HoyoWrapper.genshin_game import GenshinGame
from HoyoWrapper.starrail_game import StarRailGame
from HoyoWrapper.honkai_game import HonkaiGame
from tests.conftest import (
    TEST_EMAIL, TEST_PASSWORD, TEST_DEVICE_ID,
    TEST_COOKIES_OS, TEST_ACCOUNTS, MockAccountStore,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_active_session(accounts=None) -> ActiveSession:
    """Crea un ActiveSession con sesión OS y cuentas de prueba."""
    session = HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)
    return ActiveSession(session, accounts or TEST_ACCOUNTS)


# ---------------------------------------------------------------------------
# Tests de ActiveSession
# ---------------------------------------------------------------------------

class TestActiveSession:

    def test_creates_correct_game_objects(self) -> None:
        """
        ActiveSession instancia el tipo de objeto correcto para cada game_biz.
        """
        active = _make_active_session()

        types = {g.game_biz: type(g).__name__ for g in active.games}

        assert types.get("hk4e_global")  == "GenshinGame",  f"hk4e_global → {types.get('hk4e_global')}"
        assert types.get("hkrpg_global") == "StarRailGame", f"hkrpg_global → {types.get('hkrpg_global')}"
        assert types.get("nap_global")   == "ZZZGame",      f"nap_global → {types.get('nap_global')}"
        assert types.get("bh3_global")   == "HonkaiGame",   f"bh3_global → {types.get('bh3_global')}"

    def test_game_count_matches_accounts(self) -> None:
        """El número de objetos de juego coincide con el número de cuentas."""
        active = _make_active_session()
        assert len(active.games) == len(TEST_ACCOUNTS), (
            f"Esperados {len(TEST_ACCOUNTS)} juegos, obtenidos {len(active.games)}"
        )

    def test_get_game_returns_correct_object(self) -> None:
        """get_game() devuelve el objeto correcto para un game_biz dado."""
        active = _make_active_session()
        game = active.get_game("hk4e_global")

        assert game is not None, "get_game('hk4e_global') devolvió None."
        assert isinstance(game, GenshinGame), (
            f"get_game('hk4e_global') debería devolver GenshinGame, "
            f"obtenido: {type(game).__name__}"
        )
        assert game.uid == "700000001", (
            f"UID esperado '700000001', obtenido: {game.uid!r}"
        )

    def test_get_game_returns_none_for_unknown(self) -> None:
        """get_game() devuelve None para un game_biz que no existe."""
        active = _make_active_session()
        result = active.get_game("unknown_game")
        assert result is None, (
            f"get_game() debería devolver None para game_biz desconocido, "
            f"obtenido: {result}"
        )

    def test_region_property(self) -> None:
        """region expone la región de la sesión subyacente."""
        active = _make_active_session()
        assert active.region == "os", (
            f"region esperada 'os', obtenida: {active.region!r}"
        )

    def test_account_id_property(self) -> None:
        """account_id extrae el ID de las cookies de la sesión."""
        active = _make_active_session()
        assert active.account_id == 123456789, (
            f"account_id esperado 123456789, obtenido: {active.account_id}"
        )


# ---------------------------------------------------------------------------
# Tests de checkin_all
# ---------------------------------------------------------------------------

class TestCheckinAll:

    @pytest.mark.asyncio
    async def test_returns_result_for_each_game(self) -> None:
        """
        checkin_all() devuelve un resultado por cada juego (excepto los que
        lanzan NotImplementedError, que se ignoran silenciosamente).
        """
        active = _make_active_session()

        # Mockear claim_daily_reward en todos los juegos
        for game in active.games:
            game.claim_daily_reward = AsyncMock(return_value={
                "message": "Check-in realizado.",
                "already_done": False,
                "total_sign_day": 5,
            })

        with patch("HoyoWrapper.session_manager.random_delay", new=AsyncMock()):
            results = await active.checkin_all()

        assert len(results) == len(TEST_ACCOUNTS), (
            f"Esperados {len(TEST_ACCOUNTS)} resultados, obtenidos {len(results)}"
        )

    @pytest.mark.asyncio
    async def test_success_result_structure(self) -> None:
        """
        Cada resultado exitoso contiene game_name, uid, success=True,
        message, already_done y total_sign_day.
        """
        active = _make_active_session()
        for game in active.games:
            game.claim_daily_reward = AsyncMock(return_value={
                "message": "Check-in realizado.",
                "already_done": False,
                "total_sign_day": 7,
            })

        with patch("HoyoWrapper.session_manager.random_delay", new=AsyncMock()):
            results = await active.checkin_all()

        for r in results:
            assert r["success"] is True,       f"success debería ser True. Resultado: {r}"
            assert "game_name" in r,            f"Falta 'game_name' en resultado: {r}"
            assert "uid" in r,                  f"Falta 'uid' en resultado: {r}"
            assert "message" in r,              f"Falta 'message' en resultado: {r}"
            assert "already_done" in r,         f"Falta 'already_done' en resultado: {r}"
            assert "total_sign_day" in r,       f"Falta 'total_sign_day' en resultado: {r}"
            assert r["total_sign_day"] == 7,    f"total_sign_day esperado 7, obtenido: {r['total_sign_day']}"

    @pytest.mark.asyncio
    async def test_api_error_captured_per_game(self) -> None:
        """
        Si un juego lanza HoyoAPIError, su resultado tiene success=False
        con el mensaje de error. Los demás juegos continúan normalmente.
        """
        active = _make_active_session()
        games = active.games

        # El primer juego falla, el resto tiene éxito
        games[0].claim_daily_reward = AsyncMock(
            side_effect=HoyoAPIError(-100, "Rate limit reached", {})
        )
        for game in games[1:]:
            game.claim_daily_reward = AsyncMock(return_value={
                "message": "Check-in realizado.",
                "already_done": False,
                "total_sign_day": 3,
            })

        with patch("HoyoWrapper.session_manager.random_delay", new=AsyncMock()):
            results = await active.checkin_all()

        failed = [r for r in results if not r["success"]]
        succeeded = [r for r in results if r["success"]]

        assert len(failed) == 1, (
            f"Esperado 1 resultado fallido, obtenidos {len(failed)}.\n"
            f"  Fallidos: {failed}"
        )
        assert len(succeeded) == len(games) - 1, (
            f"Esperados {len(games)-1} resultados exitosos, obtenidos {len(succeeded)}."
        )
        assert "-100" in failed[0]["message"] or "Rate limit" in failed[0]["message"], (
            f"El mensaje de error debería contener el error de la API.\n"
            f"  Obtenido: {failed[0]['message']!r}"
        )

    @pytest.mark.asyncio
    async def test_not_implemented_game_silently_skipped(self) -> None:
        """
        Si un juego lanza NotImplementedError en claim_daily_reward,
        no aparece en los resultados (se ignora silenciosamente).
        """
        session = HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)
        active = ActiveSession(session, TEST_ACCOUNTS[:2])  # Solo 2 juegos

        active.games[0].claim_daily_reward = AsyncMock(
            side_effect=NotImplementedError("no soportado")
        )
        active.games[1].claim_daily_reward = AsyncMock(return_value={
            "message": "Check-in realizado.",
            "already_done": False,
            "total_sign_day": 2,
        })

        with patch("HoyoWrapper.session_manager.random_delay", new=AsyncMock()):
            results = await active.checkin_all()

        # Solo debería aparecer el juego que sí funcionó
        assert len(results) == 1, (
            f"NotImplementedError debería ignorarse. Resultados: {results}"
        )
        assert results[0]["success"] is True, (
            f"El único resultado debería ser exitoso. Resultado: {results[0]}"
        )

    @pytest.mark.asyncio
    async def test_delay_uses_config_range_not_hardcoded(self) -> None:
        """
        checkin_all() debe pasar config.DELAY_BETWEEN_GAMES a random_delay,
        no un rango hardcodeado en session_manager.py por separado. Si
        alguien ajusta el ritmo del bot en config.py, este cambio debe
        propagarse automaticamente sin tocar session_manager.py.
        """
        import config
        active = _make_active_session()
        for game in active.games:
            game.claim_daily_reward = AsyncMock(return_value={
                "message": "OK", "already_done": False, "total_sign_day": 1,
            })

        received_args = []

        async def mock_delay(*args, **kwargs):
            received_args.append(args)

        with patch("HoyoWrapper.session_manager.random_delay", side_effect=mock_delay):
            await active.checkin_all()

        assert all(args == config.DELAY_BETWEEN_GAMES for args in received_args), (
            f"random_delay deberia recibir config.DELAY_BETWEEN_GAMES "
            f"{config.DELAY_BETWEEN_GAMES}, recibio: {received_args}"
        )

    @pytest.mark.asyncio
    async def test_delay_called_between_games(self) -> None:
        """
        ANTI-DETECCIÓN: checkin_all() llama random_delay entre juegos.
        Para N juegos se esperan exactamente N-1 delays.
        """
        active = _make_active_session()
        for game in active.games:
            game.claim_daily_reward = AsyncMock(return_value={
                "message": "OK", "already_done": False, "total_sign_day": 1,
            })

        delay_call_count = 0

        async def mock_delay(*args, **kwargs):
            nonlocal delay_call_count
            delay_call_count += 1

        with patch("HoyoWrapper.session_manager.random_delay", side_effect=mock_delay):
            results = await active.checkin_all()

        expected_delays = len(active.games) - 1
        assert delay_call_count == expected_delays, (
            f"ANTI-DETECCIÓN: Se esperaban {expected_delays} delays entre {len(active.games)} juegos, "
            f"se produjeron {delay_call_count}.\n"
            f"  Sin delays entre juegos, el patrón de timing es detectablemente robótico."
        )

    @pytest.mark.asyncio
    async def test_no_delay_before_first_game(self) -> None:
        """
        ANTI-DETECCIÓN: NO se añade delay antes del primer juego.
        Solo entre juegos, nunca al inicio.
        """
        session = HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)
        active = ActiveSession(session, TEST_ACCOUNTS[:1])  # Un solo juego
        active.games[0].claim_daily_reward = AsyncMock(return_value={
            "message": "OK", "already_done": False, "total_sign_day": 1,
        })

        delay_call_count = 0

        async def mock_delay(*args, **kwargs):
            nonlocal delay_call_count
            delay_call_count += 1

        with patch("HoyoWrapper.session_manager.random_delay", side_effect=mock_delay):
            await active.checkin_all()

        assert delay_call_count == 0, (
            f"Con un solo juego no debería haber ningún delay, "
            f"se produjeron {delay_call_count}."
        )


# ---------------------------------------------------------------------------
# Tests de HoyoManager.login (filtrado de juegos)
# ---------------------------------------------------------------------------

class TestHoyoManagerLogin:

    @pytest.mark.asyncio
    async def test_delay_after_login_before_fetching_accounts(self) -> None:
        """
        HALLAZGO DE AUDITORIA ANTI-BANEO: antes, apenas terminaba el
        login (con o sin captcha), se disparaba de inmediato la consulta
        de cuentas de juego vinculadas — cero espera entre "loguearse" y
        "mirar mis cuentas". Ahora debe haber un delay con el rango de
        config.DELAY_AFTER_LOGIN entre ambos pasos.
        """
        import config
        mock_store = MockAccountStore()
        manager = HoyoManager(mock_store)

        mock_session = HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)
        mock_session.get_game_accounts = AsyncMock(return_value=TEST_ACCOUNTS)

        received: list[tuple] = []

        async def mock_delay(min_s, max_s):
            received.append((min_s, max_s))

        with patch.object(manager._auth, "login", new=AsyncMock(return_value=mock_session)):
            with patch("HoyoWrapper.session_manager.random_delay", side_effect=mock_delay):
                await manager.login(TEST_EMAIL, TEST_PASSWORD)

        assert config.DELAY_AFTER_LOGIN in received, (
            f"Deberia haberse llamado random_delay con config.DELAY_AFTER_LOGIN "
            f"{config.DELAY_AFTER_LOGIN} entre el login y get_game_accounts(). "
            f"Delays recibidos: {received}"
        )

    @pytest.mark.asyncio
    async def test_filters_unsupported_game_biz(self) -> None:
        """
        login() filtra juegos con game_biz no soportado (e.g. nxx_global — Tears of Themis).
        Solo los juegos soportados aparecen en session.games.
        """
        mock_store = MockAccountStore()
        manager = HoyoManager(mock_store)

        accounts_with_unsupported = TEST_ACCOUNTS + [
            {"game_biz": "nxx_global", "game_uid": "999",
             "nickname": "Attorney", "level": 1,
             "region": "os_eu", "region_name": "Europe"},
        ]

        mock_session = HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)
        mock_session.get_game_accounts = AsyncMock(return_value=accounts_with_unsupported)

        with patch.object(manager._auth, "login", new=AsyncMock(return_value=mock_session)):
            with patch("HoyoWrapper.session_manager.random_delay", new=AsyncMock()):
                active = await manager.login(TEST_EMAIL, TEST_PASSWORD)

        unsupported = [g for g in active.games if g.game_biz == "nxx_global"]
        assert len(unsupported) == 0, (
            f"nxx_global (Tears of Themis) no debería estar en session.games.\n"
            f"  Juegos encontrados: {[g.game_biz for g in active.games]}"
        )
        supported = [g for g in active.games if g.game_biz in
                     {"hk4e_global", "hkrpg_global", "nap_global", "bh3_global"}]
        assert len(supported) == len(TEST_ACCOUNTS), (
            f"Deberían estar los {len(TEST_ACCOUNTS)} juegos soportados.\n"
            f"  Obtenidos: {[g.game_biz for g in active.games]}"
        )

    @pytest.mark.asyncio
    async def test_login_raises_on_account_fetch_error(self) -> None:
        """
        Si get_game_accounts() falla tras el login, se lanza LoginError.
        """
        mock_store = MockAccountStore()
        manager = HoyoManager(mock_store)

        mock_session = HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)
        mock_session.get_game_accounts = AsyncMock(
            side_effect=HoyoAPIError(-100, "fetch failed", {})
        )

        with patch.object(manager._auth, "login", new=AsyncMock(return_value=mock_session)):
            with patch("HoyoWrapper.session_manager.random_delay", new=AsyncMock()):
                with pytest.raises(LoginError) as exc_info:
                    await manager.login(TEST_EMAIL, TEST_PASSWORD)

        assert "cuentas" in str(exc_info.value).lower() or "accounts" in str(exc_info.value).lower(), (
            f"El mensaje de LoginError debería mencionar el error de cuentas.\n"
            f"  Obtenido: {exc_info.value}"
        )

    @pytest.mark.asyncio
    async def test_logout_delegates_to_auth(self) -> None:
        """
        HoyoManager.logout() delega en auth_manager.logout().
        """
        mock_store = MockAccountStore()
        manager = HoyoManager(mock_store)

        with patch.object(manager._auth, "logout", new=AsyncMock()) as mock_logout:
            await manager.logout(TEST_EMAIL)

        mock_logout.assert_called_once_with(TEST_EMAIL)


class TestHoyoManagerTokenAndFreshLogin:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("flag", [True, False])
    async def test_login_passes_force_fresh_to_auth(self, flag: bool) -> None:
        """force_fresh llega intacto hasta AuthManager.login() (por defecto False)."""
        manager = HoyoManager(MockAccountStore())
        session = HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)
        session.get_game_accounts = AsyncMock(return_value=TEST_ACCOUNTS)

        with patch.object(manager._auth, "login", new=AsyncMock(return_value=session)) as auth_login:
            with patch("HoyoWrapper.session_manager.random_delay", new=AsyncMock()):
                if flag:
                    await manager.login(TEST_EMAIL, TEST_PASSWORD, region="os", force_fresh=True)
                else:
                    await manager.login(TEST_EMAIL, TEST_PASSWORD, region="os")

        assert auth_login.await_args.kwargs["force_fresh"] is flag

    @pytest.mark.asyncio
    async def test_renew_cookie_token_delegates_to_auth(self) -> None:
        manager = HoyoManager(MockAccountStore())
        cookies = dict(TEST_COOKIES_OS)

        with patch.object(manager._auth, "renew_cookie_token", new=AsyncMock(return_value=True)) as renew:
            result = await manager.renew_cookie_token(TEST_EMAIL, "os", cookies, TEST_DEVICE_ID)

        assert result is True
        renew.assert_awaited_once_with(TEST_EMAIL, "os", cookies, TEST_DEVICE_ID)
