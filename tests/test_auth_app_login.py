"""
tests/test_auth_app_login.py
----------------------------
Login OS por app (appLoginByPassword) y medicion de la vida util del
cookie_token_v2 — HoyoWrapper/auth_manager.py.

POR QUE EXISTE EL LOGIN POR APP: el login web (el original) devuelve un
cookie_token_v2 pero NINGUN stoken (genshin.py: WebLoginResult no tiene
campo stoken). Sin stoken ese token no se puede renovar: vence, el canje
empieza a fallar con -1071 y la unica salida era volver a poner la
contrasena. El login por app (genshin.py: AppLoginResult) SI devuelve el
stoken, y con el se generan ltoken_v2 y cookie_token_v2 cuando haga falta.

Sin red real: _raw_post, _fetch_cookie_token y los solucionadores de
captcha se simulan. Lo que se verifica es el CONTRATO con HoYoverse
(cabeceras, firma, orden captcha -> email -> resultado), no a HoYoverse.
"""

from __future__ import annotations

import base64
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from aioresponses import aioresponses
from loguru import logger

import config
from db.store import AccountStore
from HoyoWrapper import auth_manager as am
from HoyoWrapper.auth_manager import (
    AuthManager, _build_aigis_header_v4, _generate_app_device_id,
    _log_cookie_lifetimes, _log_response, _parse_app_aigis, _redact,
)
from HoyoWrapper.session import LoginError
from tests.conftest import (
    TEST_COOKIES_OS, TEST_COOKIES_OS_PRE_EXCHANGE, TEST_DEVICE_ID,
    TEST_EMAIL, TEST_PASSWORD, MockAccountStore,
)

_APP_OK_DATA = {
    "token": {"token_type": 1, "token": "v2_STOKEN_VALUE"},
    "user_info": {"aid": 111222, "mid": "mid_abc"},
}


def _resp(retcode: int = 0, data=None, headers=None, message: str = "OK") -> dict:
    return {"retcode": retcode, "message": message, "data": data,
            "headers": headers or {}, "cookies": {}}


@contextmanager
def _patched_flow(auth: AuthManager, responses: list, exchange=None):
    """
    Simula _raw_post (con una cola de respuestas), el intercambio de tokens
    y los delays. Entrega (posts, fetch, delay); cada post guarda una COPIA
    de las cabeceras en el momento de la llamada (el codigo las muta).
    """
    posts: list[dict] = []
    queue = list(responses)

    async def fake_post(url, payload, headers):
        posts.append({"url": url, "payload": payload, "headers": dict(headers)})
        return queue.pop(0)

    exchange = {"cookie_token_v2": "CT", "ltoken_v2": "LT"} if exchange is None else exchange
    with patch.object(auth, "_raw_post", side_effect=fake_post), \
         patch.object(auth, "_fetch_cookie_token", new=AsyncMock(return_value=exchange)) as fetch, \
         patch("HoyoWrapper.auth_manager.random_delay", new=AsyncMock()) as delay:
        yield posts, fetch, delay


@pytest.fixture
def captured_logs():
    """Todo lo que loguru escribe durante el test, como lista de textos."""
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="DEBUG", format="{message}")
    yield messages
    logger.remove(sink_id)


def _app_flow_auth() -> AuthManager:
    return AuthManager(MockAccountStore())


# ---------------------------------------------------------------------------
# Flujo principal
# ---------------------------------------------------------------------------

class TestAppLoginFlow:

    @pytest.mark.asyncio
    async def test_success_builds_a_complete_cookie_set(self) -> None:
        """
        Tras el login por app la sesion queda COMPLETA: el stoken (la
        credencial duradera), los ids/mid, y ltoken_v2 + cookie_token_v2
        generados con ese stoken — sin depender de ningun login web.
        """
        auth = _app_flow_auth()
        with _patched_flow(auth, [_resp(0, _APP_OK_DATA)]):
            cookies, _ = await auth._os_app_login_flow(TEST_EMAIL, TEST_PASSWORD)

        assert cookies == {
            "stoken": "v2_STOKEN_VALUE",
            "ltuid_v2": "111222", "account_id_v2": "111222",
            "ltmid_v2": "mid_abc", "account_mid_v2": "mid_abc",
            "ltoken_v2": "LT", "cookie_token_v2": "CT",
        }

    @pytest.mark.asyncio
    async def test_tokens_are_requested_with_the_stoken_and_mid(self) -> None:
        auth = _app_flow_auth()
        with _patched_flow(auth, [_resp(0, _APP_OK_DATA)]) as (_, fetch, _d):
            await auth._os_app_login_flow(TEST_EMAIL, TEST_PASSWORD)

        sent = fetch.await_args.args[0]
        assert sent["stoken"] == "v2_STOKEN_VALUE" and sent["ltmid_v2"] == "mid_abc"

    @pytest.mark.asyncio
    async def test_delay_happens_before_the_token_exchange(self) -> None:
        """ANTI-BANEO: pausa con DELAY_LOGIN_TO_COOKIE_EXCHANGE entre el login y la request de tokens."""
        auth = _app_flow_auth()
        events: list = []

        async def fake_delay(min_s, max_s):
            events.append(("delay", (min_s, max_s)))

        async def fake_fetch(cookies):
            events.append(("fetch",))
            return {"cookie_token_v2": "CT", "ltoken_v2": "LT"}

        with _patched_flow(auth, [_resp(0, _APP_OK_DATA)]):
            with patch("HoyoWrapper.auth_manager.random_delay", side_effect=fake_delay), \
                 patch.object(auth, "_fetch_cookie_token", side_effect=fake_fetch):
                await auth._os_app_login_flow(TEST_EMAIL, TEST_PASSWORD)

        assert events == [("delay", config.DELAY_LOGIN_TO_COOKIE_EXCHANGE), ("fetch",)]

    @pytest.mark.asyncio
    async def test_posts_to_the_app_endpoint_with_app_headers_and_encrypted_credentials(self) -> None:
        auth = _app_flow_auth()
        with _patched_flow(auth, [_resp(0, _APP_OK_DATA)]) as (posts, _f, _d):
            await auth._os_app_login_flow(TEST_EMAIL, TEST_PASSWORD)

        post = posts[0]
        assert post["url"] == config.APP_LOGIN_URL
        assert post["headers"]["x-rpc-client_type"] == "2", "Es el perfil de la APP, no del navegador."
        assert post["headers"]["x-rpc-aigis_v4"] == "true", "Sin esto HoYoverse no ofrece captcha v4."
        assert TEST_EMAIL not in json.dumps(post["payload"])
        assert TEST_PASSWORD not in json.dumps(post["payload"]), "Las credenciales viajan cifradas con RSA."
        assert set(post["payload"]) == {"account", "password"}

    @pytest.mark.asyncio
    async def test_signature_is_computed_over_the_body_for_every_post(self) -> None:
        """El 'ds' del login por app incluye el body y se recalcula en cada request (captcha incluido)."""
        auth = _app_flow_auth()
        aigis = json.dumps({"session_id": "S1", "mmt_type": 6,
                            "data": json.dumps({"gt": "G", "use_v4": True})})
        v4 = {"captcha_id": "G", "lot_number": "L", "pass_token": "P",
              "gen_time": "1", "captcha_output": "O", "session_id": "S1"}
        calls: list = []

        def fake_ds(body, salt):
            calls.append((body, salt))
            return "1,abcdef,hash"

        with _patched_flow(auth, [_resp(-3101, headers={"x-rpc-aigis": aigis}), _resp(0, _APP_OK_DATA)]) as (posts, _f, _d):
            with patch("HoyoWrapper.auth_manager.solve_geetest_v4", new=AsyncMock(return_value=v4)), \
                 patch("HoyoWrapper.auth_manager.ds_utils.generate_app_login_ds", side_effect=fake_ds):
                await auth._os_app_login_flow(TEST_EMAIL, TEST_PASSWORD)

        assert len(calls) == 2, "Un ds por cada POST."
        assert all(salt == config.DS_SALT_APP_LOGIN for _, salt in calls)
        assert all(set(body) == {"account", "password"} for body, _ in calls)
        assert all(p["headers"]["ds"] == "1,abcdef,hash" for p in posts)

    @pytest.mark.asyncio
    async def test_given_device_id_is_reused_and_sent(self) -> None:
        """
        REGLA CLAVE (genshin.py): el device_id debe ser CONSISTENTE entre
        logins de la misma cuenta, o HoYoverse pide verificacion por email
        en cada uno.
        """
        auth = _app_flow_auth()
        with _patched_flow(auth, [_resp(0, _APP_OK_DATA)]) as (posts, _f, _d):
            _, device_id = await auth._os_app_login_flow(TEST_EMAIL, TEST_PASSWORD, device_id="mydevice12345678")

        assert device_id == "mydevice12345678"
        assert posts[0]["headers"]["x-rpc-device_id"] == "mydevice12345678"

    @pytest.mark.asyncio
    async def test_generates_an_app_style_device_id_when_none_given(self) -> None:
        auth = _app_flow_auth()
        with _patched_flow(auth, [_resp(0, _APP_OK_DATA)]) as (posts, _f, _d):
            _, device_id = await auth._os_app_login_flow(TEST_EMAIL, TEST_PASSWORD)

        assert re.fullmatch(r"[a-z0-9]{16}", device_id)
        assert posts[0]["headers"]["x-rpc-device_id"] == device_id

    @pytest.mark.asyncio
    async def test_wrong_credentials_fail_fast_without_captcha_or_retry(self) -> None:
        auth = _app_flow_auth()
        with _patched_flow(auth, [_resp(-3001, message="bad pass")]) as (posts, fetch, _d):
            with pytest.raises(LoginError) as exc:
                await auth._os_app_login_flow(TEST_EMAIL, TEST_PASSWORD)

        assert str(exc.value) == config.LOGIN_ERROR_MESSAGES[-3001]
        assert len(posts) == 1
        fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_login_without_stoken_in_the_response_is_an_error(self) -> None:
        """retcode 0 pero sin token/aid/mid: no se inventa una sesion a medias."""
        auth = _app_flow_auth()
        with _patched_flow(auth, [_resp(0, {"user_info": {"aid": 1, "mid": "m"}})]) as (_p, fetch, _d):
            with pytest.raises(LoginError, match="stoken"):
                await auth._os_app_login_flow(TEST_EMAIL, TEST_PASSWORD)
        fetch.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exchange", [{}, {"cookie_token_v2": "CT"}, {"ltoken_v2": "LT"}])
    async def test_incomplete_token_exchange_is_an_error(self, exchange: dict) -> None:
        """
        Sin ltoken_v2 el check-in no funciona y sin cookie_token_v2 el
        canje tampoco: una sesion a medias se rechaza con un mensaje que
        dice como volver al login web.
        """
        auth = _app_flow_auth()
        with _patched_flow(auth, [_resp(0, _APP_OK_DATA)], exchange=exchange):
            with pytest.raises(LoginError, match="OS_LOGIN_METHOD"):
                await auth._os_app_login_flow(TEST_EMAIL, TEST_PASSWORD)

    @pytest.mark.asyncio
    async def test_new_device_triggers_the_email_verification_in_app_mode(self) -> None:
        auth = _app_flow_auth()
        verified = _resp(0, _APP_OK_DATA)
        with _patched_flow(auth, [_resp(config.APP_LOGIN_EMAIL_VERIFY_RETCODE)]):
            with patch.object(auth, "_handle_email_verification", new=AsyncMock(return_value=verified)) as verify:
                cookies, _ = await auth._os_app_login_flow(TEST_EMAIL, TEST_PASSWORD)

        assert verify.await_args.kwargs["app"] is True
        assert cookies["stoken"] == "v2_STOKEN_VALUE"


# ---------------------------------------------------------------------------
# Captcha del login por app (v4 y v3)
# ---------------------------------------------------------------------------

class TestAppLoginCaptcha:

    @staticmethod
    def _aigis(data: dict) -> str:
        return json.dumps({"session_id": "S1", "mmt_type": 6, "data": json.dumps(data)})

    @pytest.mark.asyncio
    async def test_v4_captcha_is_solved_and_login_retried_once(self) -> None:
        auth = _app_flow_auth()
        data = {"gt": "CAPID", "risk_type": "slide", "new_captcha": 1, "success": 1, "use_v4": True}
        v4 = {"captcha_id": "CAPID", "lot_number": "LOT", "pass_token": "PASS",
              "gen_time": "123", "captcha_output": "OUT", "session_id": "S1"}

        with _patched_flow(auth, [_resp(-3101, headers={"x-rpc-aigis": self._aigis(data)}),
                                  _resp(0, _APP_OK_DATA)]) as (posts, _f, _d):
            with patch("HoyoWrapper.auth_manager.solve_geetest_v4", new=AsyncMock(return_value=v4)) as solve, \
                 patch("HoyoWrapper.auth_manager.solve_geetest", new=AsyncMock()) as solve_v3:
                await auth._os_app_login_flow(TEST_EMAIL, TEST_PASSWORD)

        solve_v3.assert_not_awaited()
        mmt = solve.await_args.args[0]
        assert mmt["captcha_id"] == "CAPID" and mmt["session_id"] == "S1" and mmt["risk_type"] == "slide"
        assert solve.await_args.kwargs["api_server"] == config.GEETEST_V4_API_SERVER_OS

        assert len(posts) == 2
        assert "x-rpc-aigis" not in posts[0]["headers"]
        # Formato de genshin.py (get_aigis_header): "session_id;base64(json(campos v4))"
        expected_fields = {"captcha_id": "CAPID", "lot_number": "LOT", "pass_token": "PASS",
                           "gen_time": "123", "captcha_output": "OUT"}
        sid, _, encoded = posts[1]["headers"]["x-rpc-aigis"].partition(";")
        assert sid == "S1"
        assert json.loads(base64.b64decode(encoded)) == expected_fields

    @pytest.mark.asyncio
    async def test_v3_captcha_is_still_supported(self) -> None:
        auth = _app_flow_auth()
        data = {"gt": "GTX", "challenge": "CHAL", "new_captcha": 1, "success": 1}
        v3 = {"geetest_challenge": "C", "geetest_validate": "V",
              "geetest_seccode": "S", "session_id": "S1"}

        with _patched_flow(auth, [_resp(-3101, headers={"x-rpc-aigis": self._aigis(data)}),
                                  _resp(0, _APP_OK_DATA)]) as (posts, _f, _d):
            with patch("HoyoWrapper.auth_manager.solve_geetest", new=AsyncMock(return_value=v3)) as solve, \
                 patch("HoyoWrapper.auth_manager.solve_geetest_v4", new=AsyncMock()) as solve_v4:
                await auth._os_app_login_flow(TEST_EMAIL, TEST_PASSWORD)

        solve_v4.assert_not_awaited()
        assert solve.await_args.args[0]["gt"] == "GTX"
        assert solve.await_args.kwargs["api_server"] == config.GEETEST_API_SERVER_OS
        sid, _, encoded = posts[1]["headers"]["x-rpc-aigis"].partition(";")
        assert sid == "S1"
        assert {"geetest_challenge", "geetest_validate", "geetest_seccode"} <= set(json.loads(base64.b64decode(encoded)))

    @pytest.mark.asyncio
    async def test_a_rejected_captcha_is_not_retried_forever(self) -> None:
        """Si tras resolver el captcha HoYoverse lo vuelve a pedir: error. Nada de bucles."""
        auth = _app_flow_auth()
        data = {"gt": "CAPID", "use_v4": True}
        again = _resp(-3101, headers={"x-rpc-aigis": self._aigis(data)})
        v4 = {"captcha_id": "CAPID", "lot_number": "L", "pass_token": "P",
              "gen_time": "1", "captcha_output": "O", "session_id": "S1"}

        with _patched_flow(auth, [again, again]) as (posts, _f, _d):
            with patch("HoyoWrapper.auth_manager.solve_geetest_v4", new=AsyncMock(return_value=v4)) as solve:
                with pytest.raises(LoginError):
                    await auth._os_app_login_flow(TEST_EMAIL, TEST_PASSWORD)

        assert len(posts) == 2
        solve.assert_awaited_once()


# ---------------------------------------------------------------------------
# Verificacion por email: modo app vs modo web
# ---------------------------------------------------------------------------

class TestEmailVerificationModes:

    @staticmethod
    def _resp_with_ticket() -> dict:
        verify = json.dumps({"risk_ticket": "RT", "verify_str": json.dumps({"ticket": "TK", "verify_type": "email"})})
        return {"headers": {"x-rpc-verify": verify}, "data": None}

    @pytest.mark.asyncio
    async def test_app_mode_uses_its_own_send_and_retries_without_the_consumed_captcha(self) -> None:
        """
        REGRESION (fallo real en vivo): el captcha del login ya se consumio. El
        reintento tras verificar el email NO debe volver a mandarlo
        (genshin.py solo manda x-rpc-verify), pero si conserva el device_id.
        """
        auth = _app_flow_auth()
        headers = {"x-rpc-device_id": "dev", "x-rpc-aigis": "consumido;abc"}
        retried: list[dict] = []

        async def fake_app_post(payload, hdrs):
            retried.append(dict(hdrs))
            return _resp(0)

        with patch.object(auth, "_send_verification_email_app", new=AsyncMock(return_value="sent")) as send_app, \
             patch.object(auth, "_send_verification_email", new=AsyncMock()) as send_web, \
             patch.object(auth, "_verify_email_code", new=AsyncMock()) as verify, \
             patch.object(auth, "_app_login_post", side_effect=fake_app_post), \
             patch.object(auth, "_raw_post", new=AsyncMock()) as raw_post, \
             patch("HoyoWrapper.auth_manager.enter_code", new=AsyncMock(return_value="123456")):
            await auth._handle_email_verification(self._resp_with_ticket(), {"account": "a"}, headers, app=True)

        send_app.assert_awaited_once_with("TK")
        send_web.assert_not_awaited()
        assert verify.await_args.kwargs["headers"]["x-rpc-client_type"] == "2"
        assert verify.await_args.kwargs["code"] == "123456"
        assert verify.await_args.kwargs["action_ticket"] == "RT"
        raw_post.assert_not_awaited()

        assert len(retried) == 1
        assert "x-rpc-aigis" not in retried[0], "El captcha del login ya se consumio."
        assert json.loads(retried[0]["x-rpc-verify"])["risk_ticket"] == "RT"
        assert retried[0]["x-rpc-device_id"] == "dev"

    @pytest.mark.asyncio
    async def test_web_mode_is_unchanged(self) -> None:
        """REGRESION: el flujo web (el que ya funcionaba en vivo) sigue usando sus cabeceras y su endpoint."""
        auth = _app_flow_auth()
        with patch.object(auth, "_send_verification_email", new=AsyncMock()) as send, \
             patch.object(auth, "_verify_email_code", new=AsyncMock()) as verify, \
             patch.object(auth, "_app_login_post", new=AsyncMock()) as app_post, \
             patch.object(auth, "_raw_post", new=AsyncMock(return_value=_resp(0))) as raw_post, \
             patch("HoyoWrapper.auth_manager.enter_code", new=AsyncMock(return_value="123456")):
            await auth._handle_email_verification(self._resp_with_ticket(), {"account": "a"}, {})

        assert send.await_args.kwargs["base_headers"] is None
        assert verify.await_args.kwargs["headers"] is None
        app_post.assert_not_awaited()
        assert raw_post.await_args.args[0] == config.WEB_LOGIN_URL


# ---------------------------------------------------------------------------
# Eleccion del metodo de login OS
# ---------------------------------------------------------------------------

class TestDoLoginDispatch:

    @pytest.mark.asyncio
    async def test_os_uses_the_app_login_by_default_and_forwards_the_device_id(self) -> None:
        assert config.OS_LOGIN_METHOD == "app", "El metodo por defecto es el que entrega stoken."
        auth = _app_flow_auth()
        with patch.object(auth, "_os_app_login_flow", new=AsyncMock(return_value=({}, "d"))) as app_flow, \
             patch.object(auth, "_os_login_flow", new=AsyncMock()) as web_flow:
            await auth._do_login(TEST_EMAIL, TEST_PASSWORD, "os", device_id="dev123")

        app_flow.assert_awaited_once_with(TEST_EMAIL, TEST_PASSWORD, device_id="dev123")
        web_flow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_config_can_switch_back_to_the_web_login(self, monkeypatch) -> None:
        """Si el login por app deja de funcionar, volver al web es UNA linea en config.py."""
        monkeypatch.setattr(config, "OS_LOGIN_METHOD", "web")
        auth = _app_flow_auth()
        with patch.object(auth, "_os_app_login_flow", new=AsyncMock()) as app_flow, \
             patch.object(auth, "_os_login_flow", new=AsyncMock(return_value=({}, "d"))) as web_flow:
            await auth._do_login(TEST_EMAIL, TEST_PASSWORD, "os")

        web_flow.assert_awaited_once()
        app_flow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cn_never_uses_the_app_login(self) -> None:
        auth = _app_flow_auth()
        with patch.object(auth, "_cn_login_flow", new=AsyncMock(return_value=({}, "d"))) as cn_flow, \
             patch.object(auth, "_os_app_login_flow", new=AsyncMock()) as app_flow:
            await auth._do_login(TEST_EMAIL, TEST_PASSWORD, "cn", device_id="ignored")

        cn_flow.assert_awaited_once()
        app_flow.assert_not_awaited()


# ---------------------------------------------------------------------------
# login(): device_id conocido
# ---------------------------------------------------------------------------

class TestLoginDeviceIdReuse:

    @pytest.mark.asyncio
    async def test_known_device_id_is_forwarded_on_a_fresh_login(self, mock_store: MockAccountStore) -> None:
        mock_store.seed(TEST_EMAIL, "os", dict(TEST_COOKIES_OS), TEST_DEVICE_ID)
        auth = AuthManager(mock_store)

        with aioresponses() as m:
            m.get(config.GAME_ACCOUNTS_OS_URL, payload={"retcode": 0, "data": {"list": []}}, repeat=True)
            with patch.object(auth, "_do_login", new=AsyncMock(return_value=(dict(TEST_COOKIES_OS), TEST_DEVICE_ID))) as do_login:
                await auth.login(TEST_EMAIL, TEST_PASSWORD, region="os", force_fresh=True)

        assert do_login.await_args.kwargs["device_id"] == TEST_DEVICE_ID

    @pytest.mark.asyncio
    async def test_device_id_is_forwarded_when_cookies_expired_too(self, mock_store: MockAccountStore) -> None:
        mock_store.seed(TEST_EMAIL, "os", dict(TEST_COOKIES_OS), TEST_DEVICE_ID)
        auth = AuthManager(mock_store)

        with aioresponses() as m:
            m.get(config.GAME_ACCOUNTS_OS_URL, payload={"retcode": -100, "message": "x"})
            m.get(config.GAME_ACCOUNTS_OS_URL, payload={"retcode": 0, "data": {"list": []}}, repeat=True)
            with patch.object(auth, "_do_login", new=AsyncMock(return_value=(dict(TEST_COOKIES_OS), TEST_DEVICE_ID))) as do_login:
                await auth.login(TEST_EMAIL, TEST_PASSWORD, region="os")

        assert do_login.await_args.kwargs["device_id"] == TEST_DEVICE_ID

    @pytest.mark.asyncio
    async def test_first_ever_login_passes_no_device_id(self, mock_store: MockAccountStore) -> None:
        """Sin dispositivo previo no se manda nada y el flujo genera uno (firma original de _do_login intacta)."""
        auth = AuthManager(mock_store)

        async def strict_do_login(email, password, region):  # falla con TypeError si le llega device_id
            return dict(TEST_COOKIES_OS), "generated"

        with aioresponses() as m:
            m.get(config.GAME_ACCOUNTS_OS_URL, payload={"retcode": 0, "data": {"list": []}}, repeat=True)
            with patch.object(auth, "_do_login", side_effect=strict_do_login):
                session = await auth.login(TEST_EMAIL, TEST_PASSWORD, region="os")

        assert session.device_id == "generated"


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

class TestAppLoginHelpers:

    def test_app_device_id_format(self) -> None:
        """16 caracteres [a-z0-9], NO un UUID (genshin.py: generate_app_device_id)."""
        ids = {_generate_app_device_id() for _ in range(20)}
        assert len(ids) == 20, "Deben ser distintos entre si."
        assert all(re.fullmatch(r"[a-z0-9]{16}", i) for i in ids)

    def test_parse_aigis_accepts_data_as_json_string_or_dict(self) -> None:
        for data in (json.dumps({"gt": "G", "use_v4": True}), {"gt": "G", "use_v4": True}):
            raw = json.dumps({"session_id": "S", "mmt_type": 6, "data": data})
            parsed = _parse_app_aigis({"headers": {"x-rpc-aigis": raw}})
            assert parsed["use_v4"] is True and parsed["session_id"] == "S" and parsed["data"]["gt"] == "G"

    def test_parse_aigis_without_use_v4_is_v3(self) -> None:
        raw = json.dumps({"session_id": "S", "data": {"gt": "G", "challenge": "C"}})
        assert _parse_app_aigis({"headers": {"x-rpc-aigis": raw}})["use_v4"] is False

    @pytest.mark.parametrize("headers", [
        {},                                                   # sin header
        {"x-rpc-aigis": "no es json"},                        # JSON invalido
        {"x-rpc-aigis": json.dumps({"data": {"foo": 1}})},    # sin 'gt'
        {"x-rpc-aigis": json.dumps({"data": "tampoco"})},     # data no es JSON valido
    ])
    def test_parse_aigis_rejects_unexpected_shapes_with_a_clear_error(self, headers: dict) -> None:
        with pytest.raises(LoginError):
            _parse_app_aigis({"headers": headers})

    def test_v4_aigis_header_has_fields_in_the_expected_order(self) -> None:
        header = _build_aigis_header_v4({
            "session_id": "SID", "captcha_id": "a", "lot_number": "b",
            "pass_token": "c", "gen_time": "d", "captcha_output": "e",
        })
        sid, _, encoded = header.partition(";")
        assert sid == "SID"
        assert list(json.loads(base64.b64decode(encoded))) == [
            "captcha_id", "lot_number", "pass_token", "gen_time", "captcha_output",
        ]


# ---------------------------------------------------------------------------
# Secretos fuera de los logs
#
# El login por app devuelve el stoken EN CLARO dentro de `data`, y los
# Set-Cookie traen ltoken_v2/cookie_token_v2. Los logs se comparten para
# diagnosticar, asi que ninguno de esos valores puede aparecer en ellos.
# ---------------------------------------------------------------------------

class TestSecretsStayOutOfLogs:

    def test_redact_hides_secret_values_but_keeps_structure(self) -> None:
        original = {"token": {"token_type": 1, "token": "SECRET"}, "user_info": {"aid": 7},
                    "password": "hunter2", "list": [{"stoken": "S2"}]}
        redacted = _redact(original)

        assert redacted["token"]["token"] == "<oculto len=6>"
        assert redacted["token"]["token_type"] == 1, "Los enteros no secretos se conservan."
        assert redacted["user_info"] == {"aid": 7}
        assert redacted["password"] == "<oculto len=7>"
        assert redacted["list"][0]["stoken"] == "<oculto len=2>"
        assert original["token"]["token"] == "SECRET", "No debe mutar el original."

    def test_log_response_never_prints_tokens_or_set_cookie_values(self, captured_logs: list) -> None:
        _log_response("prueba", {
            "retcode": 0, "message": "OK",
            "data": {"token": {"token": "STOKEN_SECRET"}, "user_info": {"aid": 1}},
            "headers": {"set-cookie": "ltoken_v2=COOKIE_SECRET; Path=/"},
            "cookies": {"ltoken_v2": "COOKIE_SECRET"},
        })

        text = "\n".join(captured_logs)
        assert "STOKEN_SECRET" not in text
        assert "COOKIE_SECRET" not in text
        assert "ltoken_v2" in text, "Si se conserva el NOMBRE de la cookie: sirve para diagnosticar."


# ---------------------------------------------------------------------------
# Medicion: vencimiento declarado por el servidor
# ---------------------------------------------------------------------------

class TestDeclaredLifetimeMeasurement:

    @pytest.mark.asyncio
    async def test_raw_post_reports_max_age_and_expires_of_set_cookie(self) -> None:
        auth = _app_flow_auth()
        with aioresponses() as m:
            m.post("https://example.test/login", payload={"retcode": 0, "message": "OK", "data": {}},
                   headers={"Set-Cookie": "cookie_token_v2=abc; Max-Age=3600; Path=/"})
            resp = await auth._raw_post("https://example.test/login", {"a": 1}, {})

        assert resp["cookies"]["cookie_token_v2"] == "abc"
        assert resp["cookie_lifetimes"]["cookie_token_v2"]["max-age"] == "3600"

    def test_logs_the_declared_lifetimes(self, captured_logs: list) -> None:
        _log_cookie_lifetimes("login web OS", {"cookie_lifetimes": {"cookie_token_v2": {"max-age": "3600", "expires": ""}}})
        text = "\n".join(captured_logs)
        assert "[MEDICION]" in text and "3600" in text

    def test_says_so_when_no_cookie_declares_a_lifetime(self, captured_logs: list) -> None:
        """Sin Max-Age/Expires es una cookie de sesion: el log lo dice, para no confundir 'no mide' con 'dura siempre'."""
        _log_cookie_lifetimes("login web OS", {"cookie_lifetimes": {}})
        assert "ninguna cookie declara vencimiento" in "\n".join(captured_logs)

    @pytest.mark.asyncio
    async def test_token_exchange_logs_extra_fields_but_not_the_token(self, captured_logs: list) -> None:
        """Si getBySToken devuelve algun vencimiento junto al token, queda en el log — sin el valor secreto."""
        auth = _app_flow_auth()
        payload = {"retcode": 0, "data": {"tokens": [
            {"token_type": 4, "token": "SECRETVALUE", "expires_in": 3600},
        ]}}
        with aioresponses() as m:
            m.post(config.COOKIE_V2_REFRESH_URL, payload=payload)
            await auth._fetch_cookie_token(dict(TEST_COOKIES_OS_PRE_EXCHANGE))

        text = "\n".join(captured_logs)
        assert "expires_in" in text and "3600" in text
        assert "SECRETVALUE" not in text


# ---------------------------------------------------------------------------
# Medicion: emision de tokens (tabla session_events)
# ---------------------------------------------------------------------------

def _hours_ago(h: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=h)


class TestTokenIssuedEvents:

    @pytest.mark.asyncio
    async def test_login_records_the_token_issue_with_its_method(self, account_store: AccountStore) -> None:
        auth = AuthManager(account_store)
        with aioresponses() as m:
            m.get(config.GAME_ACCOUNTS_OS_URL, payload={"retcode": 0, "data": {"list": []}}, repeat=True)
            with patch.object(auth, "_do_login", new=AsyncMock(return_value=(dict(TEST_COOKIES_OS), "dev"))):
                await auth.login(TEST_EMAIL, TEST_PASSWORD, region="os")

        events = account_store.list_session_events(event=config.TOKEN_EVENT_ISSUED)
        assert len(events) == 1
        assert events[0]["email"] == TEST_EMAIL and events[0]["region"] == "os"
        assert f"login_{config.OS_LOGIN_METHOD}" in events[0]["detail"]

    @pytest.mark.asyncio
    async def test_no_event_without_a_cookie_token(self, account_store: AccountStore) -> None:
        auth = AuthManager(account_store)
        no_token = {k: v for k, v in TEST_COOKIES_OS.items() if k != "cookie_token_v2"}
        with aioresponses() as m:
            m.get(config.GAME_ACCOUNTS_OS_URL, payload={"retcode": 0, "data": {"list": []}}, repeat=True)
            with patch.object(auth, "_do_login", new=AsyncMock(return_value=(no_token, "dev"))):
                await auth.login(TEST_EMAIL, TEST_PASSWORD, region="os")

        assert account_store.list_session_events() == []

    @pytest.mark.asyncio
    async def test_cn_logins_are_never_recorded(self, account_store: AccountStore) -> None:
        """cookie_token_v2 es un concepto de HoYoLab (OS); en CN no hay nada que medir."""
        auth = AuthManager(account_store)
        with aioresponses() as m:
            m.get(config.GAME_ACCOUNTS_CN_URL, payload={"retcode": 0, "data": {"list": []}}, repeat=True)
            with patch.object(auth, "_do_login", new=AsyncMock(return_value=(dict(TEST_COOKIES_OS), "dev"))):
                await auth.login(TEST_EMAIL, TEST_PASSWORD, region="cn")

        assert account_store.list_session_events() == []

    @pytest.mark.asyncio
    async def test_renewal_records_the_age_of_the_previous_token(self, account_store: AccountStore) -> None:
        """
        La edad del token anterior al renovarlo muestra cada cuanto se
        renueva de verdad (30 h aqui = la corrida diaria se atraso).
        """
        account_store.log_session_event(TEST_EMAIL, "os", config.TOKEN_EVENT_ISSUED, created_at=_hours_ago(30))
        auth = AuthManager(account_store)

        with patch.object(auth, "_fetch_cookie_token", new=AsyncMock(return_value={"cookie_token_v2": "NEW"})), \
             patch("HoyoWrapper.auth_manager.random_delay", new=AsyncMock()):
            await auth.renew_cookie_token(TEST_EMAIL, "os", dict(TEST_COOKIES_OS_PRE_EXCHANGE), "dev")

        newest = account_store.list_session_events(event=config.TOKEN_EVENT_ISSUED)[-1]
        assert newest["detail"] == "source=renewal"
        assert newest["age_hours"] == pytest.approx(30.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_first_ever_issue_has_unknown_previous_age(self, account_store: AccountStore) -> None:
        auth = AuthManager(account_store)
        with patch.object(auth, "_fetch_cookie_token", new=AsyncMock(return_value={"cookie_token_v2": "NEW"})), \
             patch("HoyoWrapper.auth_manager.random_delay", new=AsyncMock()):
            await auth.renew_cookie_token(TEST_EMAIL, "os", dict(TEST_COOKIES_OS_PRE_EXCHANGE), "dev")

        assert account_store.list_session_events()[0]["age_hours"] is None

    @pytest.mark.asyncio
    async def test_failed_renewal_records_nothing(self, account_store: AccountStore) -> None:
        auth = AuthManager(account_store)
        with patch.object(auth, "_fetch_cookie_token", new=AsyncMock(return_value={})), \
             patch("HoyoWrapper.auth_manager.random_delay", new=AsyncMock()):
            await auth.renew_cookie_token(TEST_EMAIL, "os", dict(TEST_COOKIES_OS_PRE_EXCHANGE), "dev")

        assert account_store.list_session_events() == []

    def test_a_store_without_event_support_is_fine(self) -> None:
        """MockAccountStore (y cualquier store a medida) no implementa eventos: medir se omite sin error."""
        AuthManager(MockAccountStore())._record_token_issued(TEST_EMAIL, "os", source="test")

    def test_measurement_failures_never_break_the_flow(self, account_store: AccountStore) -> None:
        auth = AuthManager(account_store)
        with patch.object(account_store, "log_session_event", side_effect=RuntimeError("disco lleno")):
            auth._record_token_issued(TEST_EMAIL, "os", source="test")  # no debe lanzar


# ---------------------------------------------------------------------------
# De punta a punta: login() real -> HTTP simulado -> base de datos real
#
# Los tests de arriba simulan _raw_post. Este NO: usa el cifrado RSA real, la
# firma ds real, el parseo real de la respuesta y SQLite real, y solo
# reemplaza a HoYoverse (aioresponses). Es el que detecta que las piezas
# no encajan entre si.
# ---------------------------------------------------------------------------

class TestAppLoginEndToEnd:

    @pytest.mark.asyncio
    async def test_login_then_daily_renewal_with_only_the_stored_stoken(self, account_store: AccountStore) -> None:
        """
        EL OBJETIVO DE TODO ESTE CAMBIO: la contrasena se usa UNA vez. Despues
        el token se renueva solo, con el stoken guardado, sin volver a pedirla.
        """
        auth = AuthManager(account_store)
        tokens = lambda lt, ct: {"retcode": 0, "data": {"tokens": [  # noqa: E731
            {"token_type": 2, "token": lt}, {"token_type": 4, "token": ct}]}}

        with aioresponses() as m:
            m.post(config.APP_LOGIN_URL, payload={"retcode": 0, "message": "OK", "data": _APP_OK_DATA})
            m.post(config.COOKIE_V2_REFRESH_URL, payload=tokens("LT1", "CT1"))
            m.get(config.GAME_ACCOUNTS_OS_URL, payload={"retcode": 0, "data": {"list": []}}, repeat=True)

            with patch("HoyoWrapper.auth_manager.random_delay", new=AsyncMock()):
                session = await auth.login(TEST_EMAIL, TEST_PASSWORD, region="os")

                # --- el login pidio lo correcto, con la forma correcta ---
                app_call = next(
                    calls[0] for (method, url), calls in m.requests.items()
                    if method == "POST" and str(url) == config.APP_LOGIN_URL
                )
                assert app_call.kwargs["headers"]["x-rpc-client_type"] == "2"
                assert len(app_call.kwargs["headers"]["ds"].split(",")) == 3
                assert set(app_call.kwargs["json"]) == {"account", "password"}
                assert TEST_PASSWORD not in json.dumps(app_call.kwargs["json"])

                # --- la sesion quedo completa y persistida ---
                assert session.cookies["stoken"] == "v2_STOKEN_VALUE"
                assert session.cookies["cookie_token_v2"] == "CT1"
                stored, device_id = account_store.get(TEST_EMAIL, "os")
                assert stored == dict(session.cookies)
                assert re.fullmatch(r"[a-z0-9]{16}", device_id)

                # --- renovacion diaria: SIN contrasena, solo con lo guardado ---
                m.post(config.COOKIE_V2_REFRESH_URL, payload=tokens("LT2", "CT2"))
                renewed = await auth.renew_cookie_token(TEST_EMAIL, "os", stored, device_id)

        assert renewed is True
        fresh, _ = account_store.get(TEST_EMAIL, "os")
        assert fresh["cookie_token_v2"] == "CT2" and fresh["ltoken_v2"] == "LT2"
        assert fresh["stoken"] == "v2_STOKEN_VALUE", "El stoken (la credencial duradera) no cambia."

        sources = [e["detail"] for e in account_store.list_session_events(event=config.TOKEN_EVENT_ISSUED)]
        assert sources == [f"source=login_{config.OS_LOGIN_METHOD}", "source=renewal"]


# ---------------------------------------------------------------------------
# Envio del codigo por email en modo app
#
# FALLOS REALES EN VIVO (dos, en dos intentos distintos):
#  1) -3102 (GeetestFailed): se reenviaba al endpoint del email el captcha
#     ya consumido del login.
#  2) -3206 (VerificationCodeRateLimited): el envio pedia captcha (-3101), el
#     correo SALIA IGUAL, la app reenviaba tras resolverlo, chocaba con el
#     limite de frecuencia y abortaba ANTES de pedirle el codigo al usuario
#     — que ya lo tenia en su correo. Parecia que "la app no sabia recibir
#     el codigo": el paso existia, pero nunca llegaba a ejecutarse.
# ---------------------------------------------------------------------------

class TestAppEmailSend:

    @staticmethod
    def _capture(auth: AuthManager, responses: list):
        posts: list[dict] = []
        queue = list(responses)

        async def fake_post(url, payload, headers):
            posts.append({"url": url, "payload": payload, "headers": dict(headers)})
            return queue.pop(0)

        return posts, patch.object(auth, "_raw_post", side_effect=fake_post)

    @pytest.mark.asyncio
    async def test_sends_only_app_headers_never_the_login_captcha_or_device_id(self) -> None:
        auth = _app_flow_auth()
        posts, patcher = self._capture(auth, [_resp(0)])
        with patcher:
            status = await auth._send_verification_email_app("TK")

        assert status == am._EMAIL_SENT
        (post,) = posts
        assert post["url"] == config.SEND_VERIFICATION_URL
        assert post["payload"] == {"action_type": "verify_for_component", "action_ticket": "TK"}
        assert post["headers"]["x-rpc-client_type"] == "2"
        assert "x-rpc-aigis" not in post["headers"]
        assert "x-rpc-device_id" not in post["headers"]

    @pytest.mark.asyncio
    async def test_rate_limit_is_not_an_error(self) -> None:
        """-3206 = "ya se envio un codigo hace poco": hay que usarlo, no abortar."""
        auth = _app_flow_auth()
        posts, patcher = self._capture(auth, [_resp(-3206, message="Verification code requests too frequent.")])
        with patcher:
            status = await auth._send_verification_email_app("TK")

        assert status == am._EMAIL_RECENT
        assert len(posts) == 1

    @pytest.mark.asyncio
    async def test_a_captcha_on_send_is_not_resent_by_default(self) -> None:
        """
        HoYoverse pide captcha pero el correo sale igual con la primera
        peticion (asi lo trata genshin.py, y asi lo observamos). Reenviar es
        una request de mas que ademas choca con el limite de frecuencia.
        """
        auth = _app_flow_auth()
        posts, patcher = self._capture(auth, [_resp(-3101, headers={"x-rpc-aigis": "{}"})])
        with patcher, patch.object(auth, "_solve_app_captcha", new=AsyncMock()) as solve:
            status = await auth._send_verification_email_app("TK")

        assert status == am._EMAIL_CAPTCHA
        assert len(posts) == 1
        solve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_resend_solves_a_fresh_captcha_and_resends_once(self) -> None:
        auth = _app_flow_auth()
        posts, patcher = self._capture(auth, [_resp(-3101, headers={"x-rpc-aigis": "{}"}), _resp(0)])
        with patcher, patch.object(auth, "_solve_app_captcha", new=AsyncMock(return_value="S;nuevo")) as solve:
            status = await auth._send_verification_email_app("TK", solve_captcha=True)

        assert status == am._EMAIL_SENT
        solve.assert_awaited_once()
        assert len(posts) == 2
        assert "x-rpc-aigis" not in posts[0]["headers"]
        assert posts[1]["headers"]["x-rpc-aigis"] == "S;nuevo"

    @pytest.mark.asyncio
    async def test_a_resend_can_also_report_the_rate_limit(self) -> None:
        auth = _app_flow_auth()
        posts, patcher = self._capture(auth, [_resp(-3101, headers={"x-rpc-aigis": "{}"}), _resp(-3206)])
        with patcher, patch.object(auth, "_solve_app_captcha", new=AsyncMock(return_value="S;x")):
            status = await auth._send_verification_email_app("TK", solve_captcha=True)

        assert status == am._EMAIL_RECENT

    @pytest.mark.asyncio
    async def test_other_errors_are_reported_with_their_code_and_not_retried(self) -> None:
        auth = _app_flow_auth()
        posts, patcher = self._capture(auth, [_resp(-3102, message="Verification code failed, please try again")])
        with patcher, patch.object(auth, "_solve_app_captcha", new=AsyncMock()) as solve:
            with pytest.raises(LoginError, match=r"\[-3102\]"):
                await auth._send_verification_email_app("TK", solve_captcha=True)

        assert len(posts) == 1
        solve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_second_captcha_request_is_an_error_not_a_loop(self) -> None:
        auth = _app_flow_auth()
        again = _resp(-3101, headers={"x-rpc-aigis": "{}"})
        posts, patcher = self._capture(auth, [again, again])
        with patcher, patch.object(auth, "_solve_app_captcha", new=AsyncMock(return_value="S;x")) as solve:
            with pytest.raises(LoginError):
                await auth._send_verification_email_app("TK", solve_captcha=True)

        assert len(posts) == 2
        solve.assert_awaited_once()


# ---------------------------------------------------------------------------
# El prompt del codigo: recibirlo del usuario y mandarselo a HoYoverse
# ---------------------------------------------------------------------------

class TestEmailCodePrompt:

    @staticmethod
    def _patched(auth: AuthManager, *, send, answers, verify=None):
        return (
            patch.object(auth, "_send_verification_email_app", new=AsyncMock(side_effect=send)),
            patch("HoyoWrapper.auth_manager.enter_code", new=AsyncMock(side_effect=answers)),
            patch.object(auth, "_verify_email_code", new=AsyncMock(side_effect=verify)),
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["sent", "recent", "captcha"])
    async def test_the_code_is_always_asked_whatever_the_send_status(self, status: str) -> None:
        """
        REGRESION del fallo real: con "recent" (-3206) o "captcha" (-3101) el
        correo igual llega, asi que la app DEBE pedir el codigo y mandarselo a
        HoYoverse. Antes abortaba antes de preguntar.
        """
        auth = _app_flow_auth()
        send_p, ask_p, verify_p = self._patched(auth, send=[status], answers=["123456"])
        with send_p as send, ask_p as ask, verify_p as verify:
            await auth._verify_email_app("TK", "RT")

        send.assert_awaited_once()
        ask.assert_awaited_once()
        assert verify.await_args.kwargs == {
            "code": "123456", "action_ticket": "RT", "verify_ticket": "TK",
            "headers": am._APP_EMAIL_VERIFY_HEADERS,
        }

    @pytest.mark.asyncio
    async def test_a_captcha_status_does_not_resend_on_its_own(self) -> None:
        auth = _app_flow_auth()
        send_p, ask_p, verify_p = self._patched(auth, send=["captcha"], answers=["123456"])
        with send_p as send, ask_p, verify_p:
            await auth._verify_email_app("TK", "RT")

        send.assert_awaited_once_with("TK")

    @pytest.mark.asyncio
    async def test_an_empty_answer_cancels_without_calling_hoyoverse(self) -> None:
        auth = _app_flow_auth()
        send_p, ask_p, verify_p = self._patched(auth, send=["sent"], answers=["   "])
        with send_p, ask_p, verify_p as verify:
            with pytest.raises(LoginError, match="cancelada"):
                await auth._verify_email_app("TK", "RT")

        verify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_wrong_code_can_be_retyped(self) -> None:
        auth = _app_flow_auth()
        send_p, ask_p, verify_p = self._patched(
            auth, send=["sent"], answers=["111111", "222222"],
            verify=[LoginError("incorrecto"), None],
        )
        with send_p, ask_p as ask, verify_p as verify:
            await auth._verify_email_app("TK", "RT")

        assert verify.await_count == 2 and ask.await_count == 2

    @pytest.mark.asyncio
    async def test_gives_up_after_the_maximum_number_of_wrong_codes(self) -> None:
        """No se martilla el endpoint de verificacion con intentos ilimitados."""
        auth = _app_flow_auth()
        n = config.EMAIL_CODE_MAX_ATTEMPTS
        send_p, ask_p, verify_p = self._patched(
            auth, send=["sent"], answers=[str(i) for i in range(n)],
            verify=[LoginError("incorrecto")] * n,
        )
        with send_p, ask_p, verify_p as verify:
            with pytest.raises(LoginError, match="incorrecto"):
                await auth._verify_email_app("TK", "RT")

        assert verify.await_count == n

    @pytest.mark.asyncio
    @pytest.mark.parametrize("keyword", ["r", "R"])
    async def test_the_resend_keyword_resends_once_with_a_new_captcha(self, keyword: str) -> None:
        auth = _app_flow_auth()
        send_p, ask_p, verify_p = self._patched(auth, send=["captcha", "sent"], answers=[keyword, "123456"])
        with send_p as send, ask_p, verify_p as verify:
            await auth._verify_email_app("TK", "RT")

        assert send.await_count == 2
        assert send.await_args_list[1].kwargs == {"solve_captcha": True}
        assert verify.await_args.kwargs["code"] == "123456", "'r' no se manda como si fuera el codigo."

    @pytest.mark.asyncio
    async def test_a_second_resend_is_refused(self, captured_logs: list) -> None:
        """Cada reenvio exige un captcha nuevo y alimenta el limite de frecuencia: solo uno."""
        auth = _app_flow_auth()
        send_p, ask_p, verify_p = self._patched(auth, send=["captcha", "sent"], answers=["r", "r", "123456"])
        with send_p as send, ask_p, verify_p as verify:
            await auth._verify_email_app("TK", "RT")

        assert send.await_count == 2, "El envio inicial + UN reenvio, no mas."
        verify.assert_awaited_once()
        assert "Ya reenviaste" in "\n".join(captured_logs)

    @pytest.mark.asyncio
    async def test_a_failed_resend_keeps_asking_for_the_code(self) -> None:
        """Si el reenvio falla, el usuario todavia puede tener un codigo valido de antes."""
        auth = _app_flow_auth()
        send_p, ask_p, verify_p = self._patched(
            auth, send=["captcha", LoginError("boom")], answers=["r", "123456"],
        )
        with send_p, ask_p, verify_p as verify:
            await auth._verify_email_app("TK", "RT")

        verify.assert_awaited_once()

    def test_each_status_tells_the_user_what_to_do(self, captured_logs: list) -> None:
        for status in (am._EMAIL_SENT, am._EMAIL_RECENT, am._EMAIL_CAPTCHA):
            AuthManager._announce_email_status(status)
        text = "\n".join(captured_logs)

        assert "envio el codigo" in text
        assert "-3206" in text and "MAS RECIENTE" in text
        assert "-3101" in text and f"'{config.EMAIL_CODE_RESEND_KEYWORD}'" in text


class TestEmailConfig:

    def test_values_are_sane(self) -> None:
        assert config.APP_EMAIL_RATE_LIMIT_RETCODE == -3206  # VerificationCodeRateLimited (genshin.py)
        assert config.EMAIL_CODE_MAX_ATTEMPTS >= 1
        assert config.EMAIL_CODE_MAX_RESENDS >= 0
        keyword = config.EMAIL_CODE_RESEND_KEYWORD
        assert keyword and keyword == keyword.lower() and " " not in keyword, (
            "El prompt compara en minusculas: una palabra con mayusculas nunca coincidiria."
        )
        assert not keyword.isdigit(), "No puede confundirse con un codigo de verificacion."


# ---------------------------------------------------------------------------
# Secuencias completas de dispositivo nuevo (login real, HTTP simulado)
# ---------------------------------------------------------------------------

class TestNewDeviceLoginSequence:

    _AIGIS = json.dumps({"session_id": "S1", "mmt_type": 1,
                         "data": json.dumps({"gt": "CAPID", "use_v4": True, "risk_type": "slide"})})
    _VERIFY = json.dumps({"risk_ticket": "RT",
                          "verify_str": json.dumps({"ticket": "TK", "verify_type": "2"})})
    _V4 = {"captcha_id": "CAPID", "lot_number": "L", "pass_token": "P",
           "gen_time": "1", "captcha_output": "O", "session_id": "S1"}

    def _login_prefix(self) -> list:
        return [
            _resp(-3101, headers={"x-rpc-aigis": self._AIGIS}),    # login: pide captcha
            _resp(-3239, headers={"x-rpc-verify": self._VERIFY}),  # login + captcha: dispositivo nuevo
        ]

    @pytest.mark.asyncio
    async def test_captcha_then_new_device_then_email_then_login(self) -> None:
        """Camino feliz: el envio del correo responde 0."""
        auth = _app_flow_auth()
        responses = self._login_prefix() + [_resp(0), _resp(0), _resp(0, _APP_OK_DATA)]

        with _patched_flow(auth, responses) as (posts, _f, _d):
            with patch("HoyoWrapper.auth_manager.solve_geetest_v4", new=AsyncMock(return_value=self._V4)), \
                 patch("HoyoWrapper.auth_manager.enter_code", new=AsyncMock(return_value="654321")):
                cookies, _ = await auth._os_app_login_flow(TEST_EMAIL, TEST_PASSWORD, device_id="dev12345678901ab")

        assert [p["url"] for p in posts] == [
            config.APP_LOGIN_URL, config.APP_LOGIN_URL, config.SEND_VERIFICATION_URL,
            config.VERIFY_EMAIL_URL, config.APP_LOGIN_URL,
        ]
        assert "x-rpc-aigis" in posts[1]["headers"], "El reintento tras el captcha si lo lleva."
        assert "x-rpc-aigis" not in posts[2]["headers"], "El captcha del login ya se consumio."
        assert "x-rpc-device_id" not in posts[2]["headers"]
        final = posts[4]["headers"]
        assert "x-rpc-aigis" not in final, "Tras verificar el email no se reenvia el captcha viejo."
        assert json.loads(final["x-rpc-verify"])["risk_ticket"] == "RT"
        assert final["x-rpc-device_id"] == "dev12345678901ab"
        assert cookies["stoken"] == "v2_STOKEN_VALUE"

    @pytest.mark.asyncio
    async def test_the_real_failing_sequence_now_asks_for_the_code(self) -> None:
        """
        LA SECUENCIA DEL LOG: el envio responde -3101 (captcha) y el correo
        llega igual. Antes: se resolvia otro captcha, se reenviaba, -3206 y
        abortaba sin preguntar nada. Ahora: UN solo envio, se le pide el
        codigo al usuario, se verifica y se completa el login.
        """
        auth = _app_flow_auth()
        send_asks_captcha = _resp(-3101, headers={"x-rpc-aigis": "{}"})
        responses = self._login_prefix() + [send_asks_captcha, _resp(0), _resp(0, _APP_OK_DATA)]

        with _patched_flow(auth, responses) as (posts, _f, _d):
            with patch("HoyoWrapper.auth_manager.solve_geetest_v4", new=AsyncMock(return_value=self._V4)) as solve, \
                 patch("HoyoWrapper.auth_manager.enter_code", new=AsyncMock(return_value="654321")) as ask:
                cookies, _ = await auth._os_app_login_flow(TEST_EMAIL, TEST_PASSWORD)

        assert [p["url"] for p in posts] == [
            config.APP_LOGIN_URL, config.APP_LOGIN_URL, config.SEND_VERIFICATION_URL,
            config.VERIFY_EMAIL_URL, config.APP_LOGIN_URL,
        ], "Un solo envio: sin reenvio automatico."
        solve.assert_awaited_once()   # solo el captcha del login
        ask.assert_awaited_once()     # y SI se le pidio el codigo al usuario
        assert posts[3]["payload"]["email_captcha"] == "654321"
        assert cookies["stoken"] == "v2_STOKEN_VALUE"

    @pytest.mark.asyncio
    async def test_resend_on_demand_when_the_email_never_arrives(self) -> None:
        """El usuario escribe 'r': se reenvia con un captcha NUEVO y despues se verifica el codigo."""
        auth = _app_flow_auth()
        ask_captcha = _resp(-3101, headers={"x-rpc-aigis": "{}"})
        responses = self._login_prefix() + [
            ask_captcha,                # envio inicial: pide captcha
            ask_captcha, _resp(-3206),  # reenvio: pide captcha, se resuelve, llega -3206 (ya salio uno)
            _resp(0),                   # verificar el codigo
            _resp(0, _APP_OK_DATA),     # login final
        ]

        with _patched_flow(auth, responses) as (posts, _f, _d):
            with patch.object(auth, "_solve_app_captcha", new=AsyncMock(return_value="S;x")), \
                 patch("HoyoWrapper.auth_manager.enter_code", new=AsyncMock(side_effect=["r", "654321"])):
                cookies, _ = await auth._os_app_login_flow(TEST_EMAIL, TEST_PASSWORD)

        urls = [p["url"] for p in posts]
        assert urls.count(config.SEND_VERIFICATION_URL) == 3
        sends = [p for p in posts if p["url"] == config.SEND_VERIFICATION_URL]
        assert "x-rpc-aigis" not in sends[0]["headers"] and "x-rpc-aigis" not in sends[1]["headers"]
        assert sends[2]["headers"]["x-rpc-aigis"] == "S;x", "El reenvio lleva el captcha NUEVO."
        assert cookies["stoken"] == "v2_STOKEN_VALUE"
