"""
tests/test_captcha_server.py
-----------------------------
Servidor local del captcha Geetest v4 (login por app). Usa un servidor
REAL en localhost (el servidor ES lo que se prueba); solo se anula la
apertura del navegador y el delay humano.
"""

from __future__ import annotations

import asyncio
import socket

import aiohttp
import pytest

import config
from HoyoWrapper import captcha_server
from HoyoWrapper.captcha_server import SessionMMTv4, solve_geetest_v4


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


async def _wait_until_up(port: int) -> None:
    async with aiohttp.ClientSession() as http:
        for _ in range(60):
            try:
                async with http.get(f"http://localhost:{port}/mmt"):
                    return
            except aiohttp.ClientError:
                await asyncio.sleep(0.05)
    raise AssertionError("El servidor de captcha no arranco.")


@pytest.fixture
def mmt() -> SessionMMTv4:
    return SessionMMTv4(
        captcha_id="cap-123", risk_type="slide", new_captcha=1, success=1, session_id="sess-1",
    )


@pytest.fixture(autouse=True)
def _no_browser_no_human_delay(monkeypatch):
    monkeypatch.setattr(captcha_server.webbrowser, "open_new_tab", lambda url: True)
    monkeypatch.setattr(config, "DELAY_CAPTCHA_HUMAN", (0.0, 0.0))


FULL_RESULT = {
    "captcha_id": "cap-123", "lot_number": "lot", "pass_token": "pass",
    "gen_time": "1700000000", "captcha_output": "out", "session_id": "sess-1",
}


class TestGeetestV4Server:

    @pytest.mark.asyncio
    async def test_serves_v4_widget_page_with_api_server_and_lang(self, mmt) -> None:
        port = _free_port()
        task = asyncio.create_task(solve_geetest_v4(mmt, lang="es", api_server="api.test", port=port))
        await _wait_until_up(port)
        async with aiohttp.ClientSession() as http:
            page = await (await http.get(f"http://localhost:{port}/")).text()
            await http.post(f"http://localhost:{port}/send-data", json=FULL_RESULT)
        await asyncio.wait_for(task, 5)

        assert "initGeetest4" in page and "./gt/v4.js" in page
        assert "api.test" in page and '"es"' in page, "El servidor y el idioma deben inyectarse en la pagina."
        assert "__API_SERVER__" not in page and "__LANG__" not in page

    @pytest.mark.asyncio
    async def test_mmt_endpoint_exposes_what_the_widget_needs(self, mmt) -> None:
        port = _free_port()
        task = asyncio.create_task(solve_geetest_v4(mmt, port=port))
        await _wait_until_up(port)
        async with aiohttp.ClientSession() as http:
            data = await (await http.get(f"http://localhost:{port}/mmt")).json()
            await http.post(f"http://localhost:{port}/send-data", json=FULL_RESULT)
        await asyncio.wait_for(task, 5)

        assert data["captcha_id"] == "cap-123" and data["risk_type"] == "slide"
        assert data["session_id"] == "sess-1"

    @pytest.mark.asyncio
    async def test_complete_result_is_returned_in_aigis_field_order(self, mmt) -> None:
        port = _free_port()
        task = asyncio.create_task(solve_geetest_v4(mmt, port=port))
        await _wait_until_up(port)
        async with aiohttp.ClientSession() as http:
            resp = await http.post(f"http://localhost:{port}/send-data", json=FULL_RESULT)
        result = await asyncio.wait_for(task, 5)

        assert resp.status == 204
        assert dict(result) == FULL_RESULT
        assert list(result.keys()) == [
            "captcha_id", "lot_number", "pass_token", "gen_time", "captcha_output", "session_id",
        ], "El orden importa: es el orden en que se serializan dentro del header aigis."

    @pytest.mark.asyncio
    @pytest.mark.parametrize("missing", ["captcha_id", "lot_number", "pass_token", "gen_time", "captcha_output", "session_id"])
    async def test_incomplete_result_fails_fast_instead_of_sending_empty_fields(self, mmt, missing) -> None:
        """
        Un resultado incompleto no debe continuar con campos vacios (el
        servidor de HoYoverse contestaria otro -3101 sin explicar nada):
        falla ya, con un error que dice que campo falta.
        """
        port = _free_port()
        task = asyncio.create_task(solve_geetest_v4(mmt, port=port))
        await _wait_until_up(port)
        payload = {k: v for k, v in FULL_RESULT.items() if k != missing}
        async with aiohttp.ClientSession() as http:
            resp = await http.post(f"http://localhost:{port}/send-data", json=payload)

        with pytest.raises(RuntimeError, match=missing):
            await asyncio.wait_for(task, 5)
        assert resp.status == 400
