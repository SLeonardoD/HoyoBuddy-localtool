"""
tests/test_code_provider.py
---------------------------
El codigo de verificacion del email se le pide al usuario por UN solo punto
(AuthManager._ask_code), con un proveedor inyectable (captcha_server.CodeProvider).
Por defecto es la consola (enter_code), el comportamiento de siempre; una
interfaz distinta (la web) inyecta el suyo.

Lo que se prueba aqui:
  - Que el proveedor inyectado se usa en los DOS lugares donde el login pide
    el codigo, y que la consola NO se toca.
  - Que la logica posterior (reenviar, cancelar, verificar) no cambia: solo
    cambia COMO se obtiene la respuesta.
  - Que un proveedor que no cumple el contrato falla al construir, no a mitad
    de un login.
  - Que sin proveedor todo sigue como antes (los tests de test_auth_app_login
    parchean auth_manager.enter_code y deben seguir funcionando).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger

import config
from HoyoWrapper import captcha_server
from HoyoWrapper.auth_manager import AuthManager
from HoyoWrapper.session import LoginError
from HoyoWrapper.session_manager import HoyoManager
from tests.conftest import MockAccountStore

KEYWORD = config.EMAIL_CODE_RESEND_KEYWORD


class FakeProvider:
    """Proveedor de prueba: responde lo que se le diga y recuerda lo que le pidieron."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, object]] = []

    async def __call__(self, prompt: str, *, resend_keyword=None):
        self.calls.append((prompt, resend_keyword))
        return self.answers.pop(0)


def _auth(provider=None) -> AuthManager:
    return AuthManager(MockAccountStore(), code_provider=provider)


def _patched_app_steps(auth: AuthManager, *, send, verify=None):
    return (
        patch.object(auth, "_send_verification_email_app", new=AsyncMock(side_effect=send)),
        patch.object(auth, "_verify_email_code", new=AsyncMock(side_effect=verify)),
    )


@pytest.fixture
def console():
    """Reemplaza la consola: si algo la toca con un proveedor inyectado, el test falla."""
    with patch("HoyoWrapper.auth_manager.enter_code", new=AsyncMock(return_value="NO-DEBIA-LLAMARSE")) as m:
        yield m


@pytest.fixture
def captured_logs():
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="DEBUG", format="{message}")
    yield messages
    logger.remove(sink_id)


# ---------------------------------------------------------------------------
# Contrato: se valida al construir
# ---------------------------------------------------------------------------

class TestProviderContract:

    def test_no_provider_is_the_default_console_behaviour(self) -> None:
        assert _auth()._code_provider is None

    def test_accepts_a_keyword_only_resend_keyword(self) -> None:
        async def provider(prompt, *, resend_keyword=None):
            return ""
        _auth(provider)

    def test_accepts_a_positional_or_keyword_resend_keyword(self) -> None:
        async def provider(prompt, resend_keyword=None):
            return ""
        _auth(provider)

    def test_accepts_var_keyword(self) -> None:
        async def provider(prompt, **kwargs):
            return ""
        _auth(provider)

    def test_accepts_a_callable_object_and_a_mock(self) -> None:
        _auth(FakeProvider())
        _auth(AsyncMock())

    def test_rejects_a_provider_that_cannot_take_resend_keyword(self) -> None:
        async def provider(prompt):
            return ""
        with pytest.raises(TypeError, match="resend_keyword"):
            _auth(provider)

    def test_rejects_a_positional_only_resend_keyword(self) -> None:
        """El login lo pasa por nombre: un parametro solo posicional no sirve."""
        async def provider(prompt, resend_keyword=None, /):
            return ""
        with pytest.raises(TypeError, match="resend_keyword"):
            _auth(provider)

    def test_rejects_something_that_is_not_callable(self) -> None:
        with pytest.raises(TypeError, match="invocable"):
            _auth("no-soy-una-funcion")  # type: ignore[arg-type]

    def test_hoyo_manager_passes_the_provider_down(self) -> None:
        provider = FakeProvider()
        manager = HoyoManager(MockAccountStore(), code_provider=provider)
        assert manager._auth._code_provider is provider

    def test_hoyo_manager_without_provider_keeps_the_console(self) -> None:
        assert HoyoManager(MockAccountStore())._auth._code_provider is None


# ---------------------------------------------------------------------------
# Lugar 1: login OS por app (con reenvio y cancelacion)
# ---------------------------------------------------------------------------

class TestAppFlowWithProvider:

    @pytest.mark.asyncio
    async def test_the_provider_gets_the_prompt_and_the_resend_keyword(self, console) -> None:
        provider = FakeProvider("123456")
        auth = _auth(provider)
        send_p, verify_p = _patched_app_steps(auth, send=["sent"])
        with send_p, verify_p as verify:
            await auth._verify_email_app("TK", "RT")

        prompt, resend_keyword = provider.calls[0]
        assert resend_keyword == KEYWORD, "El proveedor debe saber que aqui se puede reenviar."
        assert "Codigo de verificacion" in prompt
        assert verify.await_args.kwargs["code"] == "123456"
        console.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resend_works_exactly_as_with_the_console(self, console) -> None:
        provider = FakeProvider(KEYWORD, "123456")
        auth = _auth(provider)
        send_p, verify_p = _patched_app_steps(auth, send=["captcha", "sent"])
        with send_p as send, verify_p as verify:
            await auth._verify_email_app("TK", "RT")

        assert send.await_count == 2
        assert send.await_args_list[1].kwargs == {"solve_captcha": True}
        assert verify.await_args.kwargs["code"] == "123456", "La palabra de reenvio no se manda como codigo."
        console.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer", ["", "   ", None])
    async def test_empty_or_none_cancels_without_calling_hoyoverse(self, answer, console) -> None:
        """None = el usuario cerro el cuadro: se trata igual que Enter vacio."""
        auth = _auth(FakeProvider(answer))
        send_p, verify_p = _patched_app_steps(auth, send=["sent"])
        with send_p, verify_p as verify:
            with pytest.raises(LoginError, match="cancelada"):
                await auth._verify_email_app("TK", "RT")

        verify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_wrong_code_asks_the_provider_again(self, console) -> None:
        provider = FakeProvider("111111", "222222")
        auth = _auth(provider)
        send_p, verify_p = _patched_app_steps(auth, send=["sent"])
        with send_p, verify_p as verify:
            verify.side_effect = [LoginError("incorrecto"), None]
            await auth._verify_email_app("TK", "RT")

        assert len(provider.calls) == 2 and verify.await_count == 2

    @pytest.mark.asyncio
    async def test_a_provider_returning_a_non_string_is_a_clear_error(self, console) -> None:
        auth = _auth(FakeProvider(123456))
        send_p, verify_p = _patched_app_steps(auth, send=["sent"])
        with send_p, verify_p as verify:
            with pytest.raises(TypeError, match="debe devolver str"):
                await auth._verify_email_app("TK", "RT")

        verify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_code_itself_is_never_logged(self, console, captured_logs) -> None:
        auth = _auth(FakeProvider("987654"))
        send_p, verify_p = _patched_app_steps(auth, send=["sent"])
        with send_p, verify_p:
            await auth._verify_email_app("TK", "RT")

        joined = "\n".join(captured_logs)
        assert "987654" not in joined, "El codigo de verificacion no debe quedar en el log."
        assert "longitud=6" in joined


# ---------------------------------------------------------------------------
# Lugar 2: flujo sin app (CN / web)
# ---------------------------------------------------------------------------

def _email_verify_response() -> dict:
    verify_str = json.dumps({"ticket": "TICKET-X"})
    return {
        "headers": {"x-rpc-verify": json.dumps({"risk_ticket": "RISK-X", "verify_str": verify_str})},
        "data": None,
    }


class TestNonAppFlowWithProvider:

    @pytest.mark.asyncio
    async def test_the_provider_is_used_and_no_resend_is_offered(self, console) -> None:
        provider = FakeProvider("555000")
        auth = _auth(provider)
        with patch.object(auth, "_send_verification_email", new=AsyncMock()), \
             patch.object(auth, "_verify_email_code", new=AsyncMock()) as verify, \
             patch.object(auth, "_raw_post", new=AsyncMock(return_value={"ok": 1})) as post:
            result = await auth._handle_email_verification(
                _email_verify_response(), {"p": 1}, {}, app=False,
            )

        prompt, resend_keyword = provider.calls[0]
        assert resend_keyword is None, "Este flujo no tiene reenvio: no se debe ofrecer."
        assert "codigo de verificacion" in prompt
        assert verify.await_args.kwargs["code"] == "555000"
        assert verify.await_args.kwargs["action_ticket"] == "RISK-X"
        assert result == {"ok": 1} and post.await_count == 1, "Tras verificar se reintenta el login igual que antes."
        console.assert_not_awaited()


# ---------------------------------------------------------------------------
# Sin proveedor: la consola de siempre
# ---------------------------------------------------------------------------

class TestWithoutProviderNothingChanges:

    @pytest.mark.asyncio
    async def test_the_console_is_used_and_looked_up_at_call_time(self) -> None:
        """Los tests existentes parchean auth_manager.enter_code DESPUES de construir el AuthManager."""
        auth = _auth()   # sin proveedor
        send_p, verify_p = _patched_app_steps(auth, send=["sent"])
        with patch("HoyoWrapper.auth_manager.enter_code", new=AsyncMock(return_value="123456")) as ask, \
             send_p, verify_p as verify:
            await auth._verify_email_app("TK", "RT")

        ask.assert_awaited_once()
        assert ask.await_args.kwargs == {"resend_keyword": KEYWORD}
        assert verify.await_args.kwargs["code"] == "123456"

    @pytest.mark.asyncio
    async def test_the_real_console_function_ignores_resend_keyword_and_returns_the_typed_text(self) -> None:
        shown: list[str] = []

        def fake_input(prompt: str) -> str:
            shown.append(prompt)
            return "424242"

        with patch("builtins.input", new=fake_input):
            answer = await captcha_server.enter_code("Codigo: ", resend_keyword="r")

        assert answer == "424242" and shown == ["Codigo: "], "El prompt de consola no cambia."

    @pytest.mark.asyncio
    async def test_the_real_console_function_still_works_with_just_a_prompt(self) -> None:
        with patch("builtins.input", new=lambda prompt: "1"):
            assert await captcha_server.enter_code("x") == "1"
            assert await captcha_server.enter_code() == "1"
