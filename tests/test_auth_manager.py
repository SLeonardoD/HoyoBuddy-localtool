"""
tests/test_auth_manager.py
--------------------------
Tests de HoyoWrapper/auth_manager.py.

Verifica el flujo de login/logout sin trafico real a HoYoverse:
  - Reutilizacion de cookies validas del store (evita login innecesario).
  - Fallback automatico OS -> CN (solo por error de credenciales, nunca
    por captcha — miyoushe.com no es accesible fuera de China).
  - Persistencia de cookies y device_id tras login exitoso.
  - Intercambio stoken -> cookie_token_v2 (_fetch_cookie_token), el fix
    que hace funcionar redeem_code() (retcode=-1071 "Please log in
    first" con check-in/mimo OK, porque webLoginByPassword no entrega
    cookie_token_v2 y redeem_code() lo exige).
  - Auto-reparacion de sesiones viejas que no tienen cookie_token_v2.
  - logout() llama a store.delete().
  - Validacion de sesion.

Las llamadas HTTP se interceptan con aioresponses.
El store se reemplaza por MockAccountStore para aislar SQLite.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aioresponses import aioresponses

import config
from HoyoWrapper.auth_manager import AuthManager
from HoyoWrapper.session import HoyoAPIError, HoyoSession, LoginError
from tests.conftest import (
    TEST_EMAIL, TEST_PASSWORD, TEST_DEVICE_ID,
    TEST_COOKIES_OS, TEST_COOKIES_CN, TEST_COOKIES_OS_PRE_EXCHANGE,
    MockAccountStore,
)

GAME_ACCOUNTS_OS_URL = config.GAME_ACCOUNTS_OS_URL
GAME_ACCOUNTS_CN_URL = config.GAME_ACCOUNTS_CN_URL
COOKIE_V2_REFRESH_URL = config.COOKIE_V2_REFRESH_URL

_ACCOUNTS_OK = {
    "retcode": 0,
    "data": {"list": [{"game_biz": "hk4e_global", "game_uid": "700000001",
                       "nickname": "Traveler", "level": 60,
                       "region": "os_asia", "region_name": "Asia"}]},
}
_ACCOUNTS_ERROR = {"retcode": -100, "message": "Auth failed", "data": None}


# ---------------------------------------------------------------------------
# Tests de reutilizacion de cookies
# ---------------------------------------------------------------------------

class TestLoginCookieReuse:

    @pytest.mark.asyncio
    async def test_uses_cached_cookies_if_valid(self, mock_store: MockAccountStore) -> None:
        """
        Si el store tiene cookies validas, login() las usa directamente
        sin hacer ningun POST al endpoint de login de HoYoverse.
        """
        mock_store.seed(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)

        with aioresponses() as m:
            m.get(GAME_ACCOUNTS_OS_URL, payload=_ACCOUNTS_OK)
            auth = AuthManager(mock_store)
            session = await auth.login(TEST_EMAIL, TEST_PASSWORD, region="os")

        assert isinstance(session, HoyoSession)
        assert session.region == "os"
        set_calls = [c for c in mock_store.calls if c[0] == "set"]
        assert len(set_calls) == 0, (
            f"store.set() fue llamado aunque las cookies eran validas (ya "
            f"tenian cookie_token_v2, no debia disparar auto-reparacion).\n"
            f"  Llamadas al store: {mock_store.calls}"
        )

    @pytest.mark.asyncio
    async def test_relogins_if_cached_cookies_invalid(self, mock_store: MockAccountStore) -> None:
        """Si las cookies del store existen pero ya no son validas, login() hace login completo."""
        mock_store.seed(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        auth = AuthManager(mock_store)

        with aioresponses() as m:
            m.get(GAME_ACCOUNTS_OS_URL, payload=_ACCOUNTS_ERROR)
            with patch.object(auth, "_do_login", new=AsyncMock(
                return_value=(TEST_COOKIES_OS, TEST_DEVICE_ID)
            )):
                m.get(GAME_ACCOUNTS_OS_URL, payload=_ACCOUNTS_OK)
                await auth.login(TEST_EMAIL, TEST_PASSWORD, region="os")

        set_calls = [c for c in mock_store.calls if c[0] == "set"]
        assert len(set_calls) >= 1, (
            f"store.set() deberia haberse llamado tras relogin.\n  Llamadas: {mock_store.calls}"
        )

    @pytest.mark.asyncio
    async def test_skips_store_on_empty(self, mock_store: MockAccountStore) -> None:
        """Sin cookies en el store, login() procede directamente al flujo de login."""
        auth = AuthManager(mock_store)

        with patch.object(auth, "_do_login", new=AsyncMock(
            return_value=(TEST_COOKIES_OS, TEST_DEVICE_ID)
        )):
            with aioresponses() as m:
                m.get(GAME_ACCOUNTS_OS_URL, payload=_ACCOUNTS_OK)
                session = await auth.login(TEST_EMAIL, TEST_PASSWORD, region="os")

        assert session is not None
        set_calls = [c for c in mock_store.calls if c[0] == "set"]
        assert len(set_calls) == 1, (
            f"store.set() deberia llamarse exactamente 1 vez.\n  Llamadas: {mock_store.calls}"
        )


# ---------------------------------------------------------------------------
# Tests de fallback OS -> CN
# ---------------------------------------------------------------------------

class TestRegionFallback:

    @pytest.mark.asyncio
    async def test_falls_back_to_cn_if_os_fails_by_credentials(self, mock_store: MockAccountStore) -> None:
        """
        Solo se intenta CN si el fallo de OS es de CREDENCIALES
        (config.CREDENTIAL_ERROR_RETCODES). Un captcha ya significa que
        HoYoLab reconocio el email — no tiene sentido, y ademas
        miyoushe.com no es alcanzable fuera de China.
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

        assert call_order == ["os", "cn"], f"Orden esperado ['os','cn'], obtenido: {call_order}"
        assert session.region == "cn"

    @pytest.mark.asyncio
    async def test_does_not_fallback_on_captcha_error(self, mock_store: MockAccountStore) -> None:
        """
        Un error de captcha (no de credenciales) NO debe disparar
        fallback a CN, aunque la region no se haya especificado.
        """
        auth = AuthManager(mock_store)
        call_order = []

        async def mock_do_login(email, password, region):
            call_order.append(region)
            raise LoginError("[-3101] Captcha requerido (OS) pero no fue resuelto.")

        with patch.object(auth, "_do_login", side_effect=mock_do_login):
            with pytest.raises(LoginError):
                await auth.login(TEST_EMAIL, TEST_PASSWORD)

        assert call_order == ["os"], (
            f"Un error de captcha no deberia disparar fallback a CN. "
            f"Llamadas: {call_order}"
        )

    @pytest.mark.asyncio
    async def test_no_fallback_if_region_explicit(self, mock_store: MockAccountStore) -> None:
        """Si se especifica region='os' explicitamente y falla, NO se intenta CN."""
        auth = AuthManager(mock_store)

        async def mock_do_login(email, password, region):
            raise LoginError("[-3001] Credenciales incorrectas.")

        with patch.object(auth, "_do_login", side_effect=mock_do_login):
            with pytest.raises(LoginError):
                await auth.login(TEST_EMAIL, TEST_PASSWORD, region="os")


# ---------------------------------------------------------------------------
# Tests de logout
# ---------------------------------------------------------------------------

class TestLogout:

    @pytest.mark.asyncio
    async def test_logout_calls_store_delete(self, mock_store: MockAccountStore) -> None:
        mock_store.seed(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        auth = AuthManager(mock_store)
        await auth.logout(TEST_EMAIL)

        delete_calls = [c for c in mock_store.calls if c[0] == "delete"]
        assert len(delete_calls) == 1
        assert delete_calls[0][1][0] == TEST_EMAIL

    @pytest.mark.asyncio
    async def test_logout_removes_cookies_from_store(self, mock_store: MockAccountStore) -> None:
        mock_store.seed(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        auth = AuthManager(mock_store)
        await auth.logout(TEST_EMAIL)

        cookies, _ = mock_store.get(TEST_EMAIL, "os")
        assert cookies is None


# ---------------------------------------------------------------------------
# Tests de validacion de sesion
# ---------------------------------------------------------------------------

class TestValidateSession:

    @pytest.mark.asyncio
    async def test_returns_true_if_accounts_ok(self, mock_store: MockAccountStore) -> None:
        session = HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)
        auth = AuthManager(mock_store)

        with aioresponses() as m:
            m.get(GAME_ACCOUNTS_OS_URL, payload=_ACCOUNTS_OK)
            result = await auth._validate_session(session)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_if_api_error(self, mock_store: MockAccountStore) -> None:
        session = HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)
        auth = AuthManager(mock_store)

        with aioresponses() as m:
            m.get(GAME_ACCOUNTS_OS_URL, payload=_ACCOUNTS_ERROR)
            result = await auth._validate_session(session)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_network_error(self, mock_store: MockAccountStore) -> None:
        import aiohttp
        session = HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)
        auth = AuthManager(mock_store)

        with aioresponses() as m:
            m.get(GAME_ACCOUNTS_OS_URL, exception=aiohttp.ClientError("network error"))
            result = await auth._validate_session(session)
        assert result is False


# ---------------------------------------------------------------------------
# Tests del intercambio stoken -> cookie_token_v2 (_fetch_cookie_token)
#
# CONTEXTO DE LA REGLA: redeem_code() exige cookie_token_v2 en las
# cookies (ver managers.requires_cookie_token en genshin.py). El login
# por password NO lo entrega — solo entrega ltoken_v2/ltuid_v2/stoken/
# ltmid_v2. Sin este intercambio, redeem_code() falla con retcode=-1071
# "Please log in to your account first" aunque check-in y mimo funcionen
# perfecto (esos no necesitan cookie_token_v2).
# ---------------------------------------------------------------------------

class TestFetchCookieToken:

    @pytest.mark.asyncio
    async def test_returns_empty_dict_without_stoken(self, mock_store: MockAccountStore) -> None:
        """Sin 'stoken' en las cookies, no hay nada que intercambiar — no debe intentar la request."""
        auth = AuthManager(mock_store)
        result = await auth._fetch_cookie_token({"ltuid_v2": "1", "ltmid_v2": "m"})
        assert result == {}, f"Sin stoken deberia devolver {{}}, obtenido: {result}"

    @pytest.mark.asyncio
    async def test_returns_empty_dict_without_ltmid_v2(self, mock_store: MockAccountStore) -> None:
        """
        Sin 'ltmid_v2' tampoco se puede completar el intercambio (el
        endpoint requiere el parametro 'mid', derivado de ltmid_v2).
        Cuentas logueadas con una version muy vieja del wrapper que
        nunca capturo ltmid_v2 caen en este caso — no se puede auto-reparar.
        """
        auth = AuthManager(mock_store)
        result = await auth._fetch_cookie_token({"stoken": "s", "ltuid_v2": "1"})
        assert result == {}, f"Sin ltmid_v2 deberia devolver {{}}, obtenido: {result}"

    @pytest.mark.asyncio
    async def test_successful_exchange_returns_cookie_token_v2(self, mock_store: MockAccountStore) -> None:
        """
        Con stoken + ltmid_v2, un intercambio exitoso devuelve
        cookie_token_v2 (token_type=4) y ltoken_v2 (token_type=2),
        parseados de la lista data.tokens[] — NO de un campo plano
        'cookie_token', que era la hipotesis original antes de leer el
        codigo fuente real de genshin.py.
        """
        auth = AuthManager(mock_store)
        payload = {
            "retcode": 0,
            "data": {"tokens": [
                {"token_type": 2, "token": "new_ltoken_v2_value"},
                {"token_type": 4, "token": "new_cookie_token_v2_value"},
            ]},
        }
        with aioresponses() as m:
            m.post(COOKIE_V2_REFRESH_URL, payload=payload)
            result = await auth._fetch_cookie_token(TEST_COOKIES_OS_PRE_EXCHANGE)

        assert result.get("cookie_token_v2") == "new_cookie_token_v2_value", (
            f"cookie_token_v2 no se extrajo correctamente. Resultado: {result}"
        )
        assert result.get("ltoken_v2") == "new_ltoken_v2_value", (
            f"ltoken_v2 no se extrajo correctamente. Resultado: {result}"
        )

    @pytest.mark.asyncio
    async def test_sends_mid_derived_from_ltmid_v2(self, mock_store: MockAccountStore) -> None:
        """El request al endpoint debe incluir la cookie 'mid' derivada de 'ltmid_v2'."""
        auth = AuthManager(mock_store)
        payload = {"retcode": 0, "data": {"tokens": [{"token_type": 4, "token": "x"}]}}

        with aioresponses() as m:
            m.post(COOKIE_V2_REFRESH_URL, payload=payload)
            await auth._fetch_cookie_token(TEST_COOKIES_OS_PRE_EXCHANGE)

            sent_requests = list(m.requests.values())[0]
            sent_cookies = sent_requests[0].kwargs.get("cookies", {})

        assert sent_cookies.get("mid") == TEST_COOKIES_OS_PRE_EXCHANGE["ltmid_v2"], (
            f"El request deberia enviar 'mid' con el valor de 'ltmid_v2'.\n"
            f"  Cookies enviadas: {sent_cookies}"
        )

    @pytest.mark.asyncio
    async def test_sends_ds_header_with_correct_format(self, mock_store: MockAccountStore) -> None:
        """
        El header 'ds' debe tener el formato "t,r,hash" (3 partes
        separadas por coma) — sin este header el endpoint rechaza la
        request con un retcode de firma invalida.
        """
        auth = AuthManager(mock_store)
        payload = {"retcode": 0, "data": {"tokens": [{"token_type": 4, "token": "x"}]}}

        with aioresponses() as m:
            m.post(COOKIE_V2_REFRESH_URL, payload=payload)
            await auth._fetch_cookie_token(TEST_COOKIES_OS_PRE_EXCHANGE)

            sent_requests = list(m.requests.values())[0]
            sent_headers = sent_requests[0].kwargs.get("headers", {})

        assert "ds" in sent_headers, "Falta el header 'ds' en la request de intercambio."
        parts = sent_headers["ds"].split(",")
        assert len(parts) == 3, (
            f"El header 'ds' deberia tener 3 partes (t,r,hash), obtenido: {sent_headers['ds']!r}"
        )

    @pytest.mark.asyncio
    async def test_sends_correct_dst_token_types_in_body(self, mock_store: MockAccountStore) -> None:
        """El body debe pedir explicitamente los token_type 2 (ltoken_v2) y 4 (cookie_token_v2)."""
        auth = AuthManager(mock_store)
        payload = {"retcode": 0, "data": {"tokens": [{"token_type": 4, "token": "x"}]}}

        with aioresponses() as m:
            m.post(COOKIE_V2_REFRESH_URL, payload=payload)
            await auth._fetch_cookie_token(TEST_COOKIES_OS_PRE_EXCHANGE)

            sent_requests = list(m.requests.values())[0]
            sent_json = sent_requests[0].kwargs.get("json", {})

        assert sent_json.get("dst_token_types") == [2, 4], (
            f"El body deberia pedir dst_token_types=[2,4], obtenido: {sent_json}"
        )

    @pytest.mark.asyncio
    async def test_returns_empty_dict_on_api_error(self, mock_store: MockAccountStore) -> None:
        """
        Si el endpoint devuelve retcode!=0, se absorbe el error y se
        devuelve {} — un fallo en la reparacion de cookie_token_v2 NO debe
        tumbar todo el login (que por lo demas fue exitoso).
        """
        auth = AuthManager(mock_store)
        with aioresponses() as m:
            m.post(COOKIE_V2_REFRESH_URL, payload={"retcode": -100, "message": "fail"})
            result = await auth._fetch_cookie_token(TEST_COOKIES_OS_PRE_EXCHANGE)
        assert result == {}

    @pytest.mark.asyncio
    async def test_returns_empty_dict_if_token_type_4_missing(self, mock_store: MockAccountStore) -> None:
        """Si la respuesta no trae token_type=4 (cookie_token_v2), se considera fallido."""
        auth = AuthManager(mock_store)
        payload = {"retcode": 0, "data": {"tokens": [{"token_type": 2, "token": "only_ltoken"}]}}
        with aioresponses() as m:
            m.post(COOKIE_V2_REFRESH_URL, payload=payload)
            result = await auth._fetch_cookie_token(TEST_COOKIES_OS_PRE_EXCHANGE)
        assert result == {}, (
            f"Sin cookie_token_v2 en la respuesta deberia devolver {{}}, obtenido: {result}"
        )

    @pytest.mark.asyncio
    async def test_returns_empty_dict_on_network_error(self, mock_store: MockAccountStore) -> None:
        """Un error de red no debe propagarse — se absorbe y devuelve {}."""
        import aiohttp
        auth = AuthManager(mock_store)
        with aioresponses() as m:
            m.post(COOKIE_V2_REFRESH_URL, exception=aiohttp.ClientError("network down"))
            result = await auth._fetch_cookie_token(TEST_COOKIES_OS_PRE_EXCHANGE)
        assert result == {}


# ---------------------------------------------------------------------------
# Tests de auto-reparacion (login() repara sesiones viejas sin cookie_token_v2)
# ---------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_delay_between_login_and_cookie_exchange(self, mock_store: MockAccountStore) -> None:
        """
        HALLAZGO DE AUDITORIA ANTI-BANEO: el POST de login y la request
        de intercambio stoken->cookie_token_v2 se disparaban una detras
        de la otra sin pausa. Ahora debe haber un delay con el rango de
        config.DELAY_LOGIN_TO_COOKIE_EXCHANGE entre ambos.
        """
        auth = AuthManager(mock_store)

        fake_login_response = {
            "retcode": 0, "message": "OK",
            "data": {"stoken": "test_stoken_os"},
            "cookies": {"ltuid_v2": "1", "ltoken_v2": "t", "ltmid_v2": "m"},
        }

        received: list[tuple] = []

        async def mock_delay(min_s, max_s):
            received.append((min_s, max_s))

        with patch.object(auth, "_raw_post", new=AsyncMock(return_value=fake_login_response)):
            with patch.object(auth, "_fetch_cookie_token", new=AsyncMock(
                return_value={"cookie_token_v2": "x"}
            )):
                with patch("HoyoWrapper.auth_manager.random_delay", side_effect=mock_delay):
                    await auth._os_login_flow(TEST_EMAIL, TEST_PASSWORD)

        assert config.DELAY_LOGIN_TO_COOKIE_EXCHANGE in received, (
            f"Deberia haberse llamado random_delay con "
            f"config.DELAY_LOGIN_TO_COOKIE_EXCHANGE {config.DELAY_LOGIN_TO_COOKIE_EXCHANGE} "
            f"antes del intercambio. Delays recibidos: {received}"
        )


class TestAutoRepairOnRestore:

    @pytest.mark.asyncio
    async def test_repairs_session_missing_cookie_token_v2(self, mock_store: MockAccountStore) -> None:
        """
        REGLA CLAVE: una sesion restaurada (cookies validas, login exitoso)
        que tiene stoken+ltmid_v2 pero NO cookie_token_v2 (cuenta logueada
        antes de este fix) se repara automaticamente, sin que el usuario
        tenga que volver a loguearse manualmente.
        """
        mock_store.seed(TEST_EMAIL, "os", TEST_COOKIES_OS_PRE_EXCHANGE, TEST_DEVICE_ID)
        auth = AuthManager(mock_store)

        with aioresponses() as m:
            m.get(GAME_ACCOUNTS_OS_URL, payload=_ACCOUNTS_OK)
            m.post(COOKIE_V2_REFRESH_URL, payload={
                "retcode": 0,
                "data": {"tokens": [{"token_type": 4, "token": "repaired_token"}]},
            })
            with patch("HoyoWrapper.auth_manager.random_delay", new=AsyncMock()):
                session = await auth.login(TEST_EMAIL, TEST_PASSWORD, region="os")

        assert session.cookies.get("cookie_token_v2") == "repaired_token", (
            f"La sesion devuelta deberia tener el cookie_token_v2 reparado.\n"
            f"  Cookies: {session.cookies}"
        )
        persisted_cookies, _ = mock_store.get(TEST_EMAIL, "os")
        assert persisted_cookies.get("cookie_token_v2") == "repaired_token", (
            "El cookie_token_v2 reparado deberia persistirse en el store, "
            "no perderse la proxima vez que se restaure la sesion."
        )

    @pytest.mark.asyncio
    async def test_does_not_touch_session_that_already_has_cookie_token_v2(
        self, mock_store: MockAccountStore
    ) -> None:
        """Una sesion que YA tiene cookie_token_v2 no dispara ninguna request de reparacion."""
        mock_store.seed(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        auth = AuthManager(mock_store)

        with aioresponses() as m:
            m.get(GAME_ACCOUNTS_OS_URL, payload=_ACCOUNTS_OK)
            # A proposito NO registramos COOKIE_V2_REFRESH_URL — si el
            # codigo intentara llamarlo, aioresponses lanzaria un error
            # de "no match" y el test fallaria.
            await auth.login(TEST_EMAIL, TEST_PASSWORD, region="os")

    @pytest.mark.asyncio
    async def test_cn_sessions_never_trigger_repair(self, mock_store: MockAccountStore) -> None:
        """
        El intercambio stoken->cookie_token_v2 es exclusivo de OS. Una
        sesion CN sin cookie_token_v2 no debe intentar repararse (CN usa
        un sistema de cuentas distinto, Miyoushe, sin este concepto).
        """
        cn_cookies_no_token = {"ltuid_v2": "1", "stoken": "s", "ltmid_v2": "m"}
        mock_store.seed(TEST_EMAIL, "cn", cn_cookies_no_token, TEST_DEVICE_ID)
        auth = AuthManager(mock_store)

        with aioresponses() as m:
            m.get(GAME_ACCOUNTS_CN_URL, payload=_ACCOUNTS_OK)
            # Sin registrar COOKIE_V2_REFRESH_URL: si se llamara, fallaria.
            await auth.login(TEST_EMAIL, TEST_PASSWORD, region="cn")

    @pytest.mark.asyncio
    async def test_new_login_also_fetches_cookie_token_v2(self, mock_store: MockAccountStore) -> None:
        """
        No solo la restauracion: un login NUEVO tambien debe completar el
        intercambio automaticamente antes de devolver las cookies, para
        que redeem_code() funcione desde el primer dia sin esperar a un
        "restore" posterior.

        Este test prueba _os_login_flow() DIRECTAMENTE (no a traves de
        login()) mockeando _raw_post (la request HTTP de login en si) y
        _fetch_cookie_token (el intercambio, ya cubierto por sus propios
        tests arriba) — asi se verifica el PUNTO DE INTEGRACION exacto:
        que _os_login_flow llama a _fetch_cookie_token con las cookies
        recien obtenidas y fusiona el resultado, sin volver a probar la
        mecanica interna de ninguno de los dos por separado.
        """
        auth = AuthManager(mock_store)

        fake_login_response = {
            "retcode": 0,
            "message": "OK",
            "data": {"stoken": "test_stoken_os"},
            "cookies": {
                "ltuid_v2": "123456789",
                "ltoken_v2": "test_ltoken_os",
                "ltmid_v2": "test_mid_os",
            },
        }

        with patch.object(auth, "_raw_post", new=AsyncMock(return_value=fake_login_response)):
            with patch.object(auth, "_fetch_cookie_token", new=AsyncMock(
                return_value={"cookie_token_v2": "brand_new_token"}
            )) as mock_fetch:
                with patch("HoyoWrapper.auth_manager.random_delay", new=AsyncMock()):
                    cookies, device_id = await auth._os_login_flow(TEST_EMAIL, TEST_PASSWORD)

        mock_fetch.assert_awaited_once()
        assert cookies.get("cookie_token_v2") == "brand_new_token", (
            f"_os_login_flow deberia fusionar el resultado de _fetch_cookie_token "
            f"en las cookies devueltas. Cookies: {cookies}"
        )
        assert cookies.get("stoken") == "test_stoken_os", (
            "El stoken de la respuesta de login deberia seguir presente."
        )


# ---------------------------------------------------------------------------
# Tests de mensajes de error de login (consolidados en config.py)
# ---------------------------------------------------------------------------

class TestLoginErrorMessages:
    """
    Antes habia 2 dicts de mensajes de retcode duplicados (uno en
    _raise_login_error, otro en _MESSAGES_FOR_FALLBACK) que se podian
    desincronizar. Ahora ambos leen de config.LOGIN_ERROR_MESSAGES.
    """

    def test_known_retcode_produces_descriptive_message(self) -> None:
        from HoyoWrapper.auth_manager import _raise_login_error

        with pytest.raises(LoginError) as exc_info:
            _raise_login_error(-3001, "raw message from api")

        assert str(exc_info.value) == config.LOGIN_ERROR_MESSAGES[-3001], (
            f"El mensaje deberia venir de config.LOGIN_ERROR_MESSAGES.\n"
            f"  Obtenido: {exc_info.value}"
        )

    def test_unknown_retcode_falls_back_to_raw_message(self) -> None:
        from HoyoWrapper.auth_manager import _raise_login_error

        with pytest.raises(LoginError) as exc_info:
            _raise_login_error(-99999, "mensaje crudo de la api")

        assert "-99999" in str(exc_info.value)
        assert "mensaje crudo de la api" in str(exc_info.value)

    def test_fallback_credential_messages_come_from_same_config_dict(self) -> None:
        """
        _MESSAGES_FOR_FALLBACK (usado para detectar errores de
        credenciales) debe ser un subconjunto de config.LOGIN_ERROR_MESSAGES,
        no una copia separada que se pueda desincronizar.
        """
        from HoyoWrapper.auth_manager import _MESSAGES_FOR_FALLBACK

        for code in config.CREDENTIAL_ERROR_RETCODES:
            assert _MESSAGES_FOR_FALLBACK[code] == config.LOGIN_ERROR_MESSAGES[code], (
                f"Mensaje desincronizado para retcode {code}."
            )
