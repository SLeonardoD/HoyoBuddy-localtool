"""
tests/test_session.py
---------------------
Tests unitarios de HoyoWrapper/session.py.

Verifica el comportamiento de HoyoSession sin tráfico real a HoYoverse.
Las llamadas HTTP se interceptan con aioresponses.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from aioresponses import aioresponses

import config
from HoyoWrapper.session import HoyoSession, HoyoAPIError
from tests.conftest import (
    TEST_COOKIES_OS, TEST_COOKIES_CN, TEST_DEVICE_ID,
)

GAME_ACCOUNTS_OS_URL = config.GAME_ACCOUNTS_OS_URL
GAME_ACCOUNTS_CN_URL = config.GAME_ACCOUNTS_CN_URL

MOCK_ACCOUNTS_RESPONSE = {
    "retcode": 0,
    "message": "OK",
    "data": {
        "list": [
            {
                "game_biz":    "hk4e_global",
                "game_uid":    "700000001",
                "nickname":    "TravelerOS",
                "level":       60,
                "region":      "os_asia",
                "region_name": "Asia",
            }
        ]
    },
}


# ---------------------------------------------------------------------------
# Tests de account_id
# ---------------------------------------------------------------------------

class TestAccountId:

    def test_extracts_ltuid_v2(self) -> None:
        """account_id extrae correctamente de la clave ltuid_v2."""
        s = HoyoSession({"ltuid_v2": "123456"}, "os", TEST_DEVICE_ID)
        assert s.account_id == 123456, (
            f"account_id esperado 123456, obtenido {s.account_id}"
        )

    def test_extracts_account_id_v2_as_fallback(self) -> None:
        """account_id usa account_id_v2 si no hay ltuid_v2."""
        s = HoyoSession({"account_id_v2": "789"}, "os", TEST_DEVICE_ID)
        assert s.account_id == 789, (
            f"account_id esperado 789, obtenido {s.account_id}"
        )

    def test_extracts_legacy_ltuid(self) -> None:
        """account_id reconoce la clave legacy 'ltuid'."""
        s = HoyoSession({"ltuid": "555"}, "os", TEST_DEVICE_ID)
        assert s.account_id == 555, (
            f"account_id esperado 555, obtenido {s.account_id}"
        )

    def test_returns_none_if_no_id_cookie(self) -> None:
        """account_id devuelve None si no hay ninguna cookie de ID."""
        s = HoyoSession({"some_other_cookie": "value"}, "os", TEST_DEVICE_ID)
        assert s.account_id is None, (
            f"account_id debería ser None, obtenido {s.account_id}"
        )

    def test_returns_none_on_non_numeric_value(self) -> None:
        """account_id devuelve None si el valor no es numérico."""
        s = HoyoSession({"ltuid_v2": "not_a_number"}, "os", TEST_DEVICE_ID)
        assert s.account_id is None, (
            f"account_id debería ser None para valor no numérico, obtenido {s.account_id}"
        )


# ---------------------------------------------------------------------------
# Tests de device_id
# ---------------------------------------------------------------------------

class TestDeviceId:

    def test_uses_provided_device_id(self) -> None:
        """Si se proporciona device_id, se almacena tal cual."""
        s = HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)
        assert s.device_id == TEST_DEVICE_ID, (
            f"device_id esperado {TEST_DEVICE_ID!r}, obtenido {s.device_id!r}"
        )

    def test_generates_device_id_if_none(self) -> None:
        """Si no se pasa device_id, se genera uno automáticamente (no None)."""
        s = HoyoSession(TEST_COOKIES_OS, "os", None)
        assert s.device_id is not None, "device_id no debería ser None."
        assert len(s.device_id) == 36, (
            f"device_id generado no tiene formato UUID (36 chars): {s.device_id!r}"
        )

    def test_device_id_is_stable_after_creation(self) -> None:
        """El device_id no cambia entre accesos; es un atributo estable."""
        s = HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)
        d1 = s.device_id
        d2 = s.device_id
        assert d1 == d2, (
            f"device_id cambió entre accesos: {d1!r} → {d2!r}"
        )

    def test_two_sessions_without_device_id_get_different_ids(self) -> None:
        """
        Dos sesiones sin device_id explícito generan IDs distintos.
        Garantiza que no se comparte el mismo UUID accidentalmente.
        """
        s1 = HoyoSession(TEST_COOKIES_OS, "os", None)
        s2 = HoyoSession(TEST_COOKIES_OS, "os", None)
        assert s1.device_id != s2.device_id, (
            f"Dos sesiones sin device_id obtuvieron el mismo ID: {s1.device_id!r}"
        )


# ---------------------------------------------------------------------------
# Tests de request()
# ---------------------------------------------------------------------------

class TestRequest:

    @pytest.mark.asyncio
    async def test_request_returns_data_on_retcode_0(self) -> None:
        """
        request() devuelve data{} cuando retcode == 0.
        No lanza ninguna excepción.
        """
        s = HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)
        payload = {"retcode": 0, "message": "OK", "data": {"foo": "bar"}}

        with aioresponses() as m:
            m.get("https://example.com/api", payload=payload)
            result = await s.request("GET", "https://example.com/api")

        assert result == {"foo": "bar"}, (
            f"request() debería devolver data{{}}, obtenido: {result}"
        )

    @pytest.mark.asyncio
    async def test_request_raises_hoyo_api_error_on_nonzero_retcode(self) -> None:
        """
        request() lanza HoyoAPIError cuando retcode != 0.
        Verifica que el retcode y message se propagan correctamente.
        """
        s = HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)
        payload = {"retcode": -100, "message": "Something went wrong", "data": None}

        with aioresponses() as m:
            m.get("https://example.com/api", payload=payload)
            with pytest.raises(HoyoAPIError) as exc_info:
                await s.request("GET", "https://example.com/api")

        err = exc_info.value
        assert err.retcode == -100, (
            f"retcode esperado -100, obtenido {err.retcode}"
        )
        assert "Something went wrong" in str(err), (
            f"El mensaje de error no contiene 'Something went wrong': {err}"
        )

    @pytest.mark.asyncio
    async def test_request_sends_cookies(self) -> None:
        """
        request() adjunta las cookies de la sesión a cada petición.
        """
        cookies = {"ltuid_v2": "42", "ltoken_v2": "mytoken"}
        s = HoyoSession(cookies, "os", TEST_DEVICE_ID)
        payload = {"retcode": 0, "data": {}}

        with aioresponses() as m:
            m.get("https://example.com/api", payload=payload)
            await s.request("GET", "https://example.com/api")
            # aioresponses registra la request; si llegó aquí sin error = OK
            assert len(m.requests) == 1, (
                "Se esperaba exactamente 1 request HTTP, "
                f"encontradas: {len(m.requests)}"
            )

    @pytest.mark.asyncio
    async def test_get_game_accounts_os_uses_correct_url(self) -> None:
        """
        get_game_accounts() en región OS llama a la URL de OS, no la de CN.
        """
        s = HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)

        with aioresponses() as m:
            m.get(GAME_ACCOUNTS_OS_URL, payload=MOCK_ACCOUNTS_RESPONSE)
            accounts = await s.get_game_accounts()

        assert isinstance(accounts, list), (
            f"get_game_accounts() debería devolver list, obtenido: {type(accounts)}"
        )
        assert len(accounts) == 1, (
            f"Se esperaba 1 cuenta, obtenidas: {len(accounts)}"
        )
        assert accounts[0]["game_biz"] == "hk4e_global", (
            f"game_biz esperado 'hk4e_global', obtenido: {accounts[0].get('game_biz')!r}"
        )

    @pytest.mark.asyncio
    async def test_get_game_accounts_cn_uses_correct_url(self) -> None:
        """
        get_game_accounts() en región CN llama a la URL de CN, no la de OS.
        """
        s = HoyoSession(TEST_COOKIES_CN, "cn", TEST_DEVICE_ID)

        with aioresponses() as m:
            m.get(GAME_ACCOUNTS_CN_URL, payload=MOCK_ACCOUNTS_RESPONSE)
            accounts = await s.get_game_accounts()

        assert isinstance(accounts, list), (
            f"get_game_accounts() CN debería devolver list, obtenido: {type(accounts)}"
        )
