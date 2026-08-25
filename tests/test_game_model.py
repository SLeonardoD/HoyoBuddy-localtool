"""
tests/test_game_model.py
------------------------
Tests unitarios de HoyoWrapper/game_model.py y las subclases de juego.

Verifica: params/headers de check-in, lógica de claim, soporte de mimo,
construcción de URLs, anti-detección (device_id persistente), retry y redeem.
Las llamadas HTTP se interceptan con aioresponses.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from aioresponses import aioresponses

from HoyoWrapper.session import HoyoAPIError, HoyoSession
from HoyoWrapper.game_model import (
    BaseGame, create_game, random_delay,
    _REWARD_URLS, _ACT_IDS, _MIMO_BASE_URL,
)
from HoyoWrapper.genshin_game import GenshinGame
from HoyoWrapper.starrail_game import StarRailGame
from HoyoWrapper.zzz_game import ZZZGame
from HoyoWrapper.honkai_game import HonkaiGame
from tests.conftest import (
    TEST_DEVICE_ID, TEST_COOKIES_OS, TEST_COOKIES_CN,
    TEST_ACCOUNTS, TEST_ACCOUNT_CN,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_checkin_info_response(is_sign: bool, total_sign_day: int = 5) -> dict:
    return {"retcode": 0, "data": {"is_sign": is_sign, "total_sign_day": total_sign_day}}


def _make_ok_response(data: dict = None) -> dict:
    return {"retcode": 0, "data": data or {}}


# ---------------------------------------------------------------------------
# Tests de constantes y propiedades
# ---------------------------------------------------------------------------

class TestGameProperties:

    def test_genshin_constants(self, genshin: GenshinGame) -> None:
        """GenshinGame tiene GAME_BIZ, GAME_NAME y GAME_ID correctos."""
        assert genshin.GAME_BIZ  == "hk4e_global",    f"GAME_BIZ: {genshin.GAME_BIZ!r}"
        assert genshin.GAME_NAME == "Genshin Impact",  f"GAME_NAME: {genshin.GAME_NAME!r}"
        assert genshin.GAME_ID   == 2,                 f"GAME_ID: {genshin.GAME_ID}"

    def test_starrail_constants(self, starrail: StarRailGame) -> None:
        """StarRailGame tiene GAME_BIZ, GAME_NAME y GAME_ID correctos."""
        assert starrail.GAME_BIZ  == "hkrpg_global",       f"GAME_BIZ: {starrail.GAME_BIZ!r}"
        assert starrail.GAME_NAME == "Honkai: Star Rail",   f"GAME_NAME: {starrail.GAME_NAME!r}"
        assert starrail.GAME_ID   == 6,                     f"GAME_ID: {starrail.GAME_ID}"

    def test_zzz_constants(self, zzz: ZZZGame) -> None:
        """ZZZGame tiene GAME_BIZ, GAME_NAME y GAME_ID correctos."""
        assert zzz.GAME_BIZ  == "nap_global",         f"GAME_BIZ: {zzz.GAME_BIZ!r}"
        assert zzz.GAME_NAME == "Zenless Zone Zero",   f"GAME_NAME: {zzz.GAME_NAME!r}"
        assert zzz.GAME_ID   == 8,                     f"GAME_ID: {zzz.GAME_ID}"

    def test_honkai_has_no_mimo(self, honkai: HonkaiGame) -> None:
        """HonkaiGame tiene GAME_ID=0 indicando ausencia de mimo."""
        assert honkai.GAME_ID == 0, (
            f"Honkai debería tener GAME_ID=0, obtenido: {honkai.GAME_ID}"
        )

    def test_game_biz_from_account(self, genshin: GenshinGame) -> None:
        """game_biz se lee del dict account, no del atributo de clase."""
        assert genshin.game_biz == "hk4e_global", (
            f"game_biz esperado 'hk4e_global', obtenido: {genshin.game_biz!r}"
        )

    def test_uid_from_account(self, genshin: GenshinGame) -> None:
        assert genshin.uid == "700000001", f"uid: {genshin.uid!r}"

    def test_is_chinese_region_os(self, genshin: GenshinGame) -> None:
        assert genshin.is_chinese_region is False, "OS no debería ser región CN."

    def test_is_chinese_region_cn(self, genshin_cn: GenshinGame) -> None:
        assert genshin_cn.is_chinese_region is True, "CN debería ser región CN."


# ---------------------------------------------------------------------------
# Tests de check-in: params y headers
# ---------------------------------------------------------------------------

class TestCheckinParams:

    def test_os_params_include_lang_and_act_id(self, genshin: GenshinGame) -> None:
        """
        Params OS incluyen act_id y lang, pero NO uid ni region.
        Enviar uid en OS es un error que podría indicar bot a HoYoverse.
        """
        params = genshin._checkin_params("en")

        assert "act_id" in params, f"Falta act_id en params OS. Params: {params}"
        assert "lang"   in params, f"Falta lang en params OS. Params: {params}"
        assert "uid"    not in params, f"uid no debería estar en params OS. Params: {params}"
        assert "region" not in params, f"region no debería estar en params OS. Params: {params}"
        assert params["act_id"] == _ACT_IDS["hk4e_global"], (
            f"act_id incorrecto.\n"
            f"  Esperado: {_ACT_IDS['hk4e_global']!r}\n"
            f"  Obtenido: {params['act_id']!r}"
        )

    def test_cn_params_include_uid_and_region(self, genshin_cn: GenshinGame) -> None:
        """
        Params CN incluyen uid y region, pero NO lang.
        CN requiere identificación explícita del jugador en cada request.
        """
        params = genshin_cn._checkin_params("en")

        assert "uid"    in params, f"Falta uid en params CN. Params: {params}"
        assert "region" in params, f"Falta region en params CN. Params: {params}"
        assert "act_id" in params, f"Falta act_id en params CN. Params: {params}"
        assert "lang"   not in params, f"lang no debería estar en params CN. Params: {params}"
        assert params["uid"] == genshin_cn.uid, (
            f"uid en params CN no coincide con el del juego.\n"
            f"  Esperado: {genshin_cn.uid!r}\n"
            f"  Obtenido: {params['uid']!r}"
        )


class TestCheckinHeaders:

    def test_os_headers_include_signgame_and_referer(self, genshin: GenshinGame) -> None:
        """Headers OS incluyen x-rpc-signgame y Referer."""
        headers = genshin._checkin_headers()

        assert "x-rpc-signgame" in headers, (
            f"Falta x-rpc-signgame en headers OS. Headers: {list(headers.keys())}"
        )
        assert headers["x-rpc-signgame"] == "hk4e", (
            f"signgame incorrecto para Genshin.\n"
            f"  Esperado: 'hk4e'\n"
            f"  Obtenido: {headers['x-rpc-signgame']!r}"
        )
        assert "Referer" in headers, (
            f"Falta Referer en headers OS. Headers: {list(headers.keys())}"
        )

    def test_cn_headers_use_session_device_id(self, genshin_cn: GenshinGame) -> None:
        """
        ANTI-DETECCIÓN: headers CN usan el device_id de la sesión (persistente),
        no uno generado en el momento. Un dispositivo real siempre tiene el mismo ID.
        """
        headers = genshin_cn._checkin_headers()

        assert "x-rpc-device_id" in headers, (
            f"Falta x-rpc-device_id en headers CN. Headers: {list(headers.keys())}"
        )
        assert headers["x-rpc-device_id"] == TEST_DEVICE_ID, (
            f"device_id en headers CN no coincide con el de la sesión.\n"
            f"  Esperado (session.device_id): {TEST_DEVICE_ID!r}\n"
            f"  Obtenido (en header):         {headers['x-rpc-device_id']!r}\n"
            f"  ¡Un device_id diferente en cada request es señal de bot!"
        )

    def test_cn_headers_include_device_model(self, genshin_cn: GenshinGame) -> None:
        """
        Headers CN incluyen x-rpc-device_model con el account_id.
        Igual que genshin.py — omitirlo aumenta la probabilidad de captcha.
        """
        headers = genshin_cn._checkin_headers()

        assert "x-rpc-device_model" in headers, (
            f"Falta x-rpc-device_model en headers CN. Headers: {list(headers.keys())}"
        )

    def test_cn_headers_do_not_have_referer(self, genshin_cn: GenshinGame) -> None:
        """Headers CN no incluyen Referer de HoYoLab (es un header de OS)."""
        headers = genshin_cn._checkin_headers()
        assert "Referer" not in headers, (
            f"Referer de HoYoLab no debería estar en headers CN.\n"
            f"  Headers: {list(headers.keys())}"
        )

    def test_device_id_same_across_multiple_header_calls(self, genshin_cn: GenshinGame) -> None:
        """
        ANTI-DETECCIÓN: llamar _checkin_headers() varias veces produce
        siempre el mismo device_id. No se genera uno nuevo en cada llamada.
        """
        h1 = genshin_cn._checkin_headers()
        h2 = genshin_cn._checkin_headers()
        h3 = genshin_cn._checkin_headers()

        ids = {h1["x-rpc-device_id"], h2["x-rpc-device_id"], h3["x-rpc-device_id"]}
        assert len(ids) == 1, (
            f"device_id cambió entre llamadas a _checkin_headers().\n"
            f"  IDs obtenidos: {ids}\n"
            f"  ¡Esto simula dispositivos distintos y es detectable!"
        )


# ---------------------------------------------------------------------------
# Tests de claim_daily_reward
# ---------------------------------------------------------------------------

class TestClaimDailyReward:

    @pytest.mark.asyncio
    async def test_skips_post_if_already_signed(self, genshin: GenshinGame) -> None:
        """
        Si is_sign=True, no se hace POST al endpoint /sign.
        Mockeamos session.request directamente para evitar problemas
        de compatibilidad entre aioresponses y ClientSession instanciada
        dentro de HoyoSession.request().
        """
        call_log: list[tuple[str, str]] = []

        async def mock_request(method: str, url: str, **kwargs):
            call_log.append((method, url))
            if "/info" in url:
                return {"is_sign": True, "total_sign_day": 10}
            return {}

        genshin.session.request = mock_request  # type: ignore[method-assign]
        result = await genshin.claim_daily_reward()

        post_calls = [c for c in call_log if c[0] == "POST"]
        assert len(post_calls) == 0, (
            f"Se hizo POST a /sign aunque is_sign=True.\n"
            f"  Llamadas realizadas: {call_log}"
        )
        assert result["already_done"] is True, (
            f"already_done deberia ser True, obtenido: {result['already_done']}"
        )
        assert result["total_sign_day"] == 10, (
            f"total_sign_day esperado 10, obtenido: {result['total_sign_day']}"
        )

    @pytest.mark.asyncio
    async def test_posts_sign_if_not_signed(self, genshin: GenshinGame) -> None:
        """
        Si is_sign=False, se hace POST a /sign y se vuelve a consultar /info.
        """
        call_count = {"info": 0}

        async def mock_request(method: str, url: str, **kwargs):
            if "/info" in url:
                call_count["info"] += 1
                # Primera llamada: no firmado. Segunda: ya firmado.
                if call_count["info"] == 1:
                    return {"is_sign": False, "total_sign_day": 4}
                return {"is_sign": True, "total_sign_day": 5}
            if "/sign" in url and method == "POST":
                return {}
            return {}

        genshin.session.request = mock_request  # type: ignore[method-assign]
        result = await genshin.claim_daily_reward()

        assert result["already_done"] is False, (
            f"already_done deberia ser False tras firmar, obtenido: {result['already_done']}"
        )
        assert result["total_sign_day"] == 5, (
            f"total_sign_day esperado 5 tras firmar, obtenido: {result['total_sign_day']}"
        )
        assert "realizado" in result["message"].lower(), (
            f"message deberia indicar exito, obtenido: {result['message']!r}"
        )

    @pytest.mark.asyncio
    async def test_propagates_api_error(self, genshin: GenshinGame) -> None:
        """
        Si la API devuelve retcode != 0 en /info, se propaga HoyoAPIError.
        """
        async def mock_request(method: str, url: str, **kwargs):
            raise HoyoAPIError(-100, "Server error", {})

        genshin.session.request = mock_request  # type: ignore[method-assign]

        with pytest.raises(HoyoAPIError) as exc_info:
            await genshin.claim_daily_reward()

        assert exc_info.value.retcode == -100, (
            f"retcode esperado -100, obtenido: {exc_info.value.retcode}"
        )


# ---------------------------------------------------------------------------
# Tests de mimo: soporte y URLs
# ---------------------------------------------------------------------------

class TestMimoSupport:

    @pytest.mark.asyncio
    async def test_honkai_mimo_raises_not_implemented(self, honkai: HonkaiGame) -> None:
        """
        Honkai Impact 3rd no tiene mimo. get_mimo_tasks() lanza NotImplementedError.
        """
        with pytest.raises(NotImplementedError) as exc_info:
            await honkai.get_mimo_tasks()

        assert "Mimo" in str(exc_info.value) or "mimo" in str(exc_info.value).lower(), (
            f"El mensaje de error debería mencionar 'Mimo'.\n"
            f"  Obtenido: {exc_info.value}"
        )

    @pytest.mark.asyncio
    async def test_cn_region_mimo_raises_not_implemented(self, genshin_cn: GenshinGame) -> None:
        """
        El mimo solo está disponible en OS. CN lanza NotImplementedError.
        """
        with pytest.raises(NotImplementedError) as exc_info:
            await genshin_cn.get_mimo_tasks()

        msg = str(exc_info.value).lower()
        assert "os" in msg or "overseas" in msg or "region" in msg, (
            f"El error debería mencionar la restricción de región.\n"
            f"  Obtenido: {exc_info.value}"
        )

    @pytest.mark.asyncio
    async def test_honkai_buy_mimo_raises_not_implemented(self, honkai: HonkaiGame) -> None:
        """buy_mimo_currency() también lanza NotImplementedError para Honkai."""
        with pytest.raises(NotImplementedError):
            await honkai.buy_mimo_currency()

    @pytest.mark.asyncio
    async def test_honkai_redeem_raises_not_implemented(self, honkai: HonkaiGame) -> None:
        """redeem_code() lanza NotImplementedError para Honkai."""
        with pytest.raises(NotImplementedError):
            await honkai.redeem_code("TESTCODE")


class TestMimoUrls:

    def test_genshin_mimo_url_uses_nata_prefix(self, genshin: GenshinGame) -> None:
        """
        Genshin (GAME_ID=2) usa /nata/ en las URLs del mimo.
        Usar la URL estandar para Genshin devuelve 404 o error de la API.
        Fuente: genshin.py/hoyolab.py/_request_mimo()
        """
        url = genshin._mimo_url("task-list")
        assert "/nata/" in url, (
            f"URL de mimo de Genshin deberia contener '/nata/'.\n"
            f"  Obtenido: {url!r}"
        )
        assert "/qiuqiu/" not in url, (
            f"URL de mimo de Genshin ya no debe usar '/qiuqiu/' (obsoleto).\n"
            f"  Obtenido: {url!r}"
        )
        assert _MIMO_BASE_URL in url, (
            f"URL de mimo debería contener la base URL.\n"
            f"  Base: {_MIMO_BASE_URL!r}\n"
            f"  URL:  {url!r}"
        )

    def test_genshin_mimo_url_converts_hyphens(self, genshin: GenshinGame) -> None:
        """
        Genshin convierte guiones en guiones_bajos en el endpoint.
        Documentado en genshin.py/_request_mimo.
        """
        url = genshin._mimo_url("task-list")
        assert "task_list" in url, (
            f"Genshin deberia convertir 'task-list' a 'task_list'.\n"
            f"  Obtenido: {url!r}"
        )
        assert "task-list" not in url, (
            f"Genshin no deberia tener guiones en el endpoint.\n"
            f"  Obtenido: {url!r}"
        )
        assert "/nata/" in url, (
            f"Genshin deberia usar el prefijo '/nata/'.\n"
            f"  Obtenido: {url!r}"
        )

    def test_starrail_mimo_url_no_nata(self, starrail: StarRailGame) -> None:
        """StarRail NO usa /nata/ — usa la ruta estandar directa."""
        url = starrail._mimo_url("task-list")
        assert "/nata/" not in url, (
            f"StarRail no deberia usar '/nata/' en el mimo.\n"
            f"  Obtenido: {url!r}"
        )
        assert "/qiuqiu/" not in url, (
            f"StarRail no deberia usar '/qiuqiu/' en el mimo.\n"
            f"  Obtenido: {url!r}"
        )

    def test_zzz_mimo_url_no_nata(self, zzz: ZZZGame) -> None:
        """ZZZ NO usa /nata/ — usa la ruta estandar directa."""
        url = zzz._mimo_url("exchange-list")
        assert "/nata/" not in url, (
            f"ZZZ no deberia usar '/nata/' en el mimo.\n"
            f"  Obtenido: {url!r}"
        )
        assert "/qiuqiu/" not in url, (
            f"ZZZ no deberia usar '/qiuqiu/' en el mimo.\n"
            f"  Obtenido: {url!r}"
        )


# ---------------------------------------------------------------------------
# Tests de create_game (factory)
# ---------------------------------------------------------------------------

class TestCreateGame:

    def test_creates_genshin_for_hk4e_global(self, session_os: HoyoSession) -> None:
        game = create_game(session_os, TEST_ACCOUNTS[0])
        assert isinstance(game, GenshinGame), (
            f"create_game para 'hk4e_global' debería devolver GenshinGame, "
            f"obtenido: {type(game).__name__}"
        )

    def test_creates_starrail_for_hkrpg_global(self, session_os: HoyoSession) -> None:
        game = create_game(session_os, TEST_ACCOUNTS[1])
        assert isinstance(game, StarRailGame), (
            f"create_game para 'hkrpg_global' debería devolver StarRailGame, "
            f"obtenido: {type(game).__name__}"
        )

    def test_creates_zzz_for_nap_global(self, session_os: HoyoSession) -> None:
        game = create_game(session_os, TEST_ACCOUNTS[2])
        assert isinstance(game, ZZZGame), (
            f"create_game para 'nap_global' debería devolver ZZZGame, "
            f"obtenido: {type(game).__name__}"
        )

    def test_creates_honkai_for_bh3_global(self, session_os: HoyoSession) -> None:
        game = create_game(session_os, TEST_ACCOUNTS[3])
        assert isinstance(game, HonkaiGame), (
            f"create_game para 'bh3_global' debería devolver HonkaiGame, "
            f"obtenido: {type(game).__name__}"
        )

    def test_creates_base_game_for_unknown_biz(self, session_os: HoyoSession) -> None:
        """game_biz desconocido devuelve BaseGame genérico sin lanzar excepción."""
        game = create_game(session_os, {"game_biz": "unknown_game"})
        assert type(game) is BaseGame, (
            f"game_biz desconocido debería devolver BaseGame, "
            f"obtenido: {type(game).__name__}"
        )

    def test_creates_genshin_for_hk4e_cn(self, session_cn: HoyoSession) -> None:
        """La variante CN también instancia GenshinGame."""
        game = create_game(session_cn, TEST_ACCOUNT_CN)
        assert isinstance(game, GenshinGame), (
            f"create_game para 'hk4e_cn' debería devolver GenshinGame, "
            f"obtenido: {type(game).__name__}"
        )


# ---------------------------------------------------------------------------
# Tests de random_delay
# ---------------------------------------------------------------------------

class TestRandomDelay:

    @pytest.mark.asyncio
    async def test_delay_within_bounds(self) -> None:
        """random_delay() espera entre min_s y max_s segundos."""
        start = time.monotonic()
        await random_delay(0.05, 0.15)
        elapsed = time.monotonic() - start

        assert elapsed >= 0.04, (
            f"random_delay duró menos de min_s=0.05s: {elapsed:.3f}s"
        )
        assert elapsed < 0.5, (
            f"random_delay duró más de lo esperado: {elapsed:.3f}s"
        )

    @pytest.mark.asyncio
    async def test_delay_is_not_zero(self) -> None:
        """random_delay() siempre espera algo — nunca retorna instantáneamente."""
        start = time.monotonic()
        await random_delay(0.01, 0.05)
        elapsed = time.monotonic() - start
        assert elapsed > 0.005, (
            f"random_delay retornó casi instantáneamente ({elapsed:.4f}s). "
            f"¿Se está ejecutando sin sleep?"
        )
