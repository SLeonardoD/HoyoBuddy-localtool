"""
tests/test_auth_manager.py
--------------------------
Tests de HoyoWrapper/auth_manager.py.

Verifica el flujo de login/logout sin tráfico real a HoYoverse:
  - Reutilización de cookies válidas del store (evita login innecesario).
  - Fallback automático OS → CN.
  - Persistencia de cookies y device_id tras login exitoso.
  - logout() llama a store.delete().
  - Validación de sesión.

Las llamadas HTTP se interceptan con aioresponses.
El store se reemplaza por MockCookieStore para aislar SQLite.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aioresponses import aioresponses

from HoyoWrapper.auth_manager import AuthManager
from HoyoWrapper.session import HoyoAPIError, HoyoSession, LoginError
from tests.conftest import (
    TEST_EMAIL, TEST_PASSWORD, TEST_DEVICE_ID,
    TEST_COOKIES_OS, TEST_COOKIES_CN,
    MockCookieStore,
)

GAME_ACCOUNTS_OS_URL = (
    "https://api-os-takumi.mihoyo.com/binding/api/getUserGameRolesByCookie"
)
GAME_ACCOUNTS_CN_URL = (
    "https://api-takumi.mihoyo.com/binding/api/getUserGameRolesByCookie"
)

_ACCOUNTS_OK = {
    "retcode": 0,
    "data": {"list": [{"game_biz": "hk4e_global", "game_uid": "700000001",
                       "nickname": "Traveler", "level": 60,
                       "region": "os_asia", "region_name": "Asia"}]},
}
_ACCOUNTS_ERROR = {"retcode": -100, "message": "Auth failed", "data": None}


# ---------------------------------------------------------------------------
# Tests de reutilización de cookies
# ---------------------------------------------------------------------------

class TestLoginCookieReuse:

    @pytest.mark.asyncio
    async def test_uses_cached_cookies_if_valid(self, mock_store: MockCookieStore) -> None:
        """
        Si el store tiene cookies válidas, login() las usa directamente
        sin hacer ningún POST al endpoint de login de HoYoverse.
        """
        mock_store.seed(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)

        with aioresponses() as m:
            # Solo permitimos la llamada de validación (get_game_accounts)
            m.get(GAME_ACCOUNTS_OS_URL, payload=_ACCOUNTS_OK)

            auth = AuthManager(mock_store)
            session = await auth.login(TEST_EMAIL, TEST_PASSWORD, region="os")

        assert isinstance(session, HoyoSession), (
            f"login() debería devolver HoyoSession, obtenido: {type(session)}"
        )
        assert session.region == "os", (
            f"Región esperada 'os', obtenida: {session.region!r}"
        )
        # El store.set() no debería haberse llamado (no hubo login nuevo)
        set_calls = [c for c in mock_store.calls if c[0] == "set"]
        assert len(set_calls) == 0, (
            f"store.set() fue llamado aunque las cookies eran válidas.\n"
            f"  Llamadas al store: {mock_store.calls}"
        )

    @pytest.mark.asyncio
    async def test_relogins_if_cached_cookies_invalid(self, mock_store: MockCookieStore) -> None:
        """
        Si las cookies del store existen pero ya no son válidas,
        login() hace el login completo y actualiza el store.
        """
        mock_store.seed(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)

        # Simulamos _do_login completo mockeando los métodos internos
        auth = AuthManager(mock_store)

        with aioresponses() as m:
            # Primera validación falla (cookies caducadas)
            m.get(GAME_ACCOUNTS_OS_URL, payload=_ACCOUNTS_ERROR)

            # El _do_login llama a _raw_post y luego valida de nuevo
            with patch.object(auth, "_do_login", new=AsyncMock(
                return_value=(TEST_COOKIES_OS, TEST_DEVICE_ID)
            )):
                m.get(GAME_ACCOUNTS_OS_URL, payload=_ACCOUNTS_OK)
                session = await auth.login(TEST_EMAIL, TEST_PASSWORD, region="os")

        set_calls = [c for c in mock_store.calls if c[0] == "set"]
        assert len(set_calls) >= 1, (
            f"store.set() debería haberse llamado tras relogin.\n"
            f"  Llamadas al store: {mock_store.calls}"
        )

    @pytest.mark.asyncio
    async def test_skips_store_on_empty(self, mock_store: MockCookieStore) -> None:
        """
        Sin cookies en el store, login() procede directamente al flujo de login.
        """
        auth = AuthManager(mock_store)

        with patch.object(auth, "_do_login", new=AsyncMock(
            return_value=(TEST_COOKIES_OS, TEST_DEVICE_ID)
        )):
            with aioresponses() as m:
                m.get(GAME_ACCOUNTS_OS_URL, payload=_ACCOUNTS_OK)
                session = await auth.login(TEST_EMAIL, TEST_PASSWORD, region="os")

        assert session is not None, "login() devolvió None."
        set_calls = [c for c in mock_store.calls if c[0] == "set"]
        assert len(set_calls) == 1, (
            f"store.set() debería haberse llamado exactamente 1 vez.\n"
            f"  Llamadas: {mock_store.calls}"
        )


# ---------------------------------------------------------------------------
# Tests de fallback OS → CN
# ---------------------------------------------------------------------------

class TestRegionFallback:

    @pytest.mark.asyncio
    async def test_falls_back_to_cn_if_os_fails(self, mock_store: MockCookieStore) -> None:
        """
        Si el login OS falla con error de credenciales y no se especifico region,
        AuthManager intenta CN automaticamente.
        El mensaje debe contener el retcode -3001 para que la condicion
        de fallback lo reconozca como error de credenciales.
        """
        auth = AuthManager(mock_store)

        call_order = []

        async def mock_do_login(email, password, region):
            call_order.append(region)
            if region == "os":
                raise LoginError("[-3001] Cuenta o contrasena incorrectos.")
            return TEST_COOKIES_CN, TEST_DEVICE_ID

        with patch.object(auth, "_do_login", side_effect=mock_do_login):
            with aioresponses() as m:
                m.get(GAME_ACCOUNTS_CN_URL, payload=_ACCOUNTS_OK)
                session = await auth.login(TEST_EMAIL, TEST_PASSWORD)

        assert "os" in call_order, (
            f"Deberia haber intentado OS primero. Orden: {call_order}"
        )
        assert "cn" in call_order, (
            f"Deberia haber intentado CN como fallback. Orden: {call_order}"
        )
        assert call_order.index("os") < call_order.index("cn"), (
            f"OS deberia intentarse antes que CN. Orden: {call_order}"
        )
        assert session.region == "cn", (
            f"Sesion deberia ser CN tras el fallback, obtenido: {session.region!r}"
        )

    @pytest.mark.asyncio
    async def test_no_fallback_if_region_explicit(self, mock_store: MockCookieStore) -> None:
        """
        Si se especifica region='os' explícitamente y falla, NO se intenta CN.
        El usuario eligió explícitamente la región.
        """
        auth = AuthManager(mock_store)

        async def mock_do_login(email, password, region):
            raise LoginError("Credenciales incorrectas.")

        with patch.object(auth, "_do_login", side_effect=mock_do_login):
            with pytest.raises(LoginError):
                await auth.login(TEST_EMAIL, TEST_PASSWORD, region="os")


# ---------------------------------------------------------------------------
# Tests de logout
# ---------------------------------------------------------------------------

class TestLogout:

    @pytest.mark.asyncio
    async def test_logout_calls_store_delete(self, mock_store: MockCookieStore) -> None:
        """
        logout() llama a store.delete() con el email correcto.
        No lanza excepción si el usuario no existía.
        """
        mock_store.seed(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)

        auth = AuthManager(mock_store)
        await auth.logout(TEST_EMAIL)

        delete_calls = [c for c in mock_store.calls if c[0] == "delete"]
        assert len(delete_calls) == 1, (
            f"store.delete() debería haberse llamado 1 vez, "
            f"llamadas: {delete_calls}"
        )
        assert delete_calls[0][1][0] == TEST_EMAIL, (
            f"store.delete() recibió email incorrecto.\n"
            f"  Esperado: {TEST_EMAIL!r}\n"
            f"  Obtenido: {delete_calls[0][1][0]!r}"
        )

    @pytest.mark.asyncio
    async def test_logout_removes_cookies_from_store(self, mock_store: MockCookieStore) -> None:
        """
        Tras logout(), el store no tiene cookies del usuario.
        """
        mock_store.seed(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)

        auth = AuthManager(mock_store)
        await auth.logout(TEST_EMAIL)

        cookies, _ = mock_store.get(TEST_EMAIL, "os")
        assert cookies is None, (
            f"Tras logout(), el store debería devolver None para el usuario.\n"
            f"  Obtenido: {cookies}"
        )


# ---------------------------------------------------------------------------
# Tests de validación de sesión
# ---------------------------------------------------------------------------

class TestValidateSession:

    @pytest.mark.asyncio
    async def test_returns_true_if_accounts_ok(self, mock_store: MockCookieStore) -> None:
        """
        _validate_session() devuelve True si get_game_accounts() tiene éxito.
        """
        session = HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)
        auth = AuthManager(mock_store)

        with aioresponses() as m:
            m.get(GAME_ACCOUNTS_OS_URL, payload=_ACCOUNTS_OK)
            result = await auth._validate_session(session)

        assert result is True, (
            f"_validate_session() debería devolver True con cookies válidas."
        )

    @pytest.mark.asyncio
    async def test_returns_false_if_api_error(self, mock_store: MockCookieStore) -> None:
        """
        _validate_session() devuelve False si la API devuelve error.
        No propaga la excepción — la absorbe y devuelve False.
        """
        session = HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)
        auth = AuthManager(mock_store)

        with aioresponses() as m:
            m.get(GAME_ACCOUNTS_OS_URL, payload=_ACCOUNTS_ERROR)
            result = await auth._validate_session(session)

        assert result is False, (
            f"_validate_session() debería devolver False con cookies inválidas."
        )

    @pytest.mark.asyncio
    async def test_returns_false_on_network_error(self, mock_store: MockCookieStore) -> None:
        """
        _validate_session() devuelve False ante errores de red.
        No propaga la excepción.
        """
        import aiohttp
        session = HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)
        auth = AuthManager(mock_store)

        with aioresponses() as m:
            m.get(GAME_ACCOUNTS_OS_URL, exception=aiohttp.ClientError("network error"))
            result = await auth._validate_session(session)

        assert result is False, (
            f"_validate_session() debería devolver False ante error de red."
        )
