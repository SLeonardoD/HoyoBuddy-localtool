"""
HoyoWrapper/game_model.py
-------------------------
Clase base para todos los módulos de juego.

Contiene la implementación real de check-in diario, mimo viajero y canje
de códigos. Las subclases solo necesitan declarar sus constantes de clase.

Arquitectura:
  - BaseGame implementa toda la lógica compartida.
  - Las subclases son declarativas: solo definen GAME_BIZ, GAME_NAME, GAME_ID.
  - Escalar a un juego nuevo = crear un archivo con ~15 líneas de constantes.

Medidas anti-detección:
  - device_id tomado de HoyoSession (persistente desde el login).
  - x-rpc-device_model en headers CN.
  - Retry con backoff exponencial + jitter ante rate limits (tenacity).
  - Delays aleatorios entre operaciones múltiples.

Notas de compatibilidad:
  - HoyoSession.request() usa 'body' (no 'json') como parámetro del cuerpo.
  - El mimo solo está disponible en región OS. CN no lo soporta.
  - Honkai Impact 3rd no tiene mimo ni canje de códigos vía esta API.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import typing

import tenacity

if typing.TYPE_CHECKING:
    from HoyoWrapper.session import HoyoSession

from HoyoWrapper.session import HoyoAPIError

__all__ = [
    "BaseGame",
    "GAME_BIZ_MAP",
    "GAME_ID_MAP",
    "CURRENCY_ITEM_KEYWORDS",
    "create_game",
    "random_delay",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mapeos de identificadores de juego
# ---------------------------------------------------------------------------

GAME_BIZ_MAP: dict[str, str] = {
    "hk4e_global":  "Genshin Impact",
    "hk4e_cn":      "Genshin Impact (CN)",
    "hkrpg_global": "Honkai: Star Rail",
    "hkrpg_cn":     "Honkai: Star Rail (CN)",
    "nap_global":   "Zenless Zone Zero",
    "nap_cn":       "Zenless Zone Zero (CN)",
    "bh3_global":   "Honkai Impact 3rd",
    "bh3_cn":       "Honkai Impact 3rd (CN)",
    "nxx_global":   "Tears of Themis",
    "nxx_cn":       "Tears of Themis (CN)",
}

GAME_ID_MAP: dict[str, int] = {
    "hk4e_global":  2,
    "hk4e_cn":      2,
    "hkrpg_global": 6,
    "hkrpg_cn":     6,
    "nap_global":   8,
    "nap_cn":       8,
}

CURRENCY_ITEM_KEYWORDS: dict[str, list[str]] = {
    "hk4e_global":  ["genesis crystal", "genesis crystals"],
    "hk4e_cn":      ["创世结晶"],
    "hkrpg_global": ["oneiric shard", "oneiric shards"],
    "hkrpg_cn":     ["星琼"],
    "nap_global":   ["monochrome", "polychrome"],
    "nap_cn":       ["菲林", "加密母带"],
    "bh3_global":   ["ether fuel"],
    "bh3_cn":       ["以太燃料"],
}

# ---------------------------------------------------------------------------
# URLs de endpoints
# ---------------------------------------------------------------------------

_REWARD_URLS: dict[str, str] = {
    "hk4e_global":  "https://sg-hk4e-api.hoyolab.com/event/sol",
    "hk4e_cn":      "https://api-takumi.mihoyo.com/event/bbs_sign_reward",
    "hkrpg_global": "https://sg-public-api.hoyolab.com/event/luna/os",
    "hkrpg_cn":     "https://api-takumi.mihoyo.com/event/luna",
    "nap_global":   "https://sg-public-api.hoyolab.com/event/luna/zzz/os",
    "nap_cn":       "https://api-takumi.mihoyo.com/event/luna/zzz",
    "bh3_global":   "https://sg-public-api.hoyolab.com/event/luna/os",
    "bh3_cn":       "https://api-takumi.mihoyo.com/event/luna",
}

_ACT_IDS: dict[str, str] = {
    "hk4e_global":  "e202102251931481",
    "hk4e_cn":      "e202009291139501",
    "hkrpg_global": "e202303301540311",
    "hkrpg_cn":     "e202304121516551",
    "nap_global":   "e202406031448091",
    "nap_cn":       "e202406031448091",
    "bh3_global":   "e202110291205111",
    "bh3_cn":       "e202110291205111",
}

_SIGNGAME: dict[str, str] = {
    "hk4e_global":  "hk4e",
    "hk4e_cn":      "hk4e",
    "hkrpg_global": "hkrpg",
    "hkrpg_cn":     "hkrpg",
    "nap_global":   "zzz",
    "nap_cn":       "zzz",
    "bh3_global":   "bh3",
    "bh3_cn":       "bh3",
}

_CODE_URLS: dict[str, str] = {
    "hk4e_global":  "https://sg-hk4e-api.hoyoverse.com/common/apicdkey/api/webExchangeCdkey",
    "hkrpg_global": "https://sg-public-api.hoyoverse.com/common/apicdkey/api/webExchangeCdkeyRisk",
    "nap_global":   "https://public-operation-nap.hoyoverse.com/common/apicdkey/api/webExchangeCdkeyRisk",
}

_CODE_POST_GAMES: frozenset[str] = frozenset({"hkrpg_global", "nap_global"})

_CODE_ORIGINS: dict[str, str] = {
    "hk4e_global":  "https://genshin.hoyoverse.com",
    "hkrpg_global": "https://hsr.hoyoverse.com",
    "nap_global":   "https://zenless.hoyoverse.com",
}

_MIMO_BASE_URL = "https://sg-public-api.hoyolab.com/event/miyoubi"
_DS_SALT_CN_SIGNIN = "xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs"

# ---------------------------------------------------------------------------
# Utilidades anti-detección
# ---------------------------------------------------------------------------

async def random_delay(min_s: float = 1.0, max_s: float = 3.0) -> None:
    """
    Espera aleatoria entre operaciones para evitar patrones de timing robótico.
    Exportada para que session_manager pueda usarla en checkin_all().
    """
    delay = random.uniform(min_s, max_s)
    logger.debug("Anti-detección: delay de %.2fs.", delay)
    await asyncio.sleep(delay)


def _is_rate_limit(exc: BaseException) -> bool:
    """True si la excepción es un rate limit de HoYoverse (retcode -429 o HTTP 429)."""
    if isinstance(exc, HoyoAPIError) and exc.retcode in (-429, 429):
        return True
    try:
        import aiohttp
        if isinstance(exc, aiohttp.ClientResponseError) and exc.status == 429:
            return True
    except ImportError:
        pass
    return False


_RETRY_KWARGS = dict(
    retry=tenacity.retry_if_exception(_is_rate_limit),
    wait=tenacity.wait_random_exponential(multiplier=2, min=5, max=60),
    stop=tenacity.stop_after_attempt(4),
    before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
    reraise=True,
)

# ---------------------------------------------------------------------------
# Clase base
# ---------------------------------------------------------------------------

class BaseGame:
    """
    Clase base para los módulos de juego.

    Implementa check-in, mimo y redeem. Las subclases declaran solo:
        GAME_BIZ, GAME_NAME, GAME_ID

    Parámetros:
        session     HoyoSession autenticada (contiene device_id persistente).
        account     Dict de HoyoSession.get_game_accounts().
    """

    GAME_BIZ:  str = ""
    GAME_NAME: str = ""
    GAME_ID:   int = 0    # 0 = sin soporte de mimo

    def __init__(self, session: "HoyoSession", account: dict) -> None:
        self.session = session
        self.account = account
        self._mimo_version_id: typing.Optional[int] = None

    # ------------------------------------------------------------------
    # Propiedades
    # ------------------------------------------------------------------

    @property
    def uid(self) -> str:
        return str(self.account.get("game_uid", ""))

    @property
    def nickname(self) -> str:
        return self.account.get("nickname", "")

    @property
    def level(self) -> int:
        return int(self.account.get("level", 0))

    @property
    def server(self) -> str:
        return self.account.get("region", "")

    @property
    def server_name(self) -> str:
        return self.account.get("region_name", "")

    @property
    def game_biz(self) -> str:
        return self.account.get("game_biz", self.GAME_BIZ)

    @property
    def is_chinese_region(self) -> bool:
        return self.session.region == "cn"

    @property
    def _reward_base_url(self) -> str:
        return _REWARD_URLS.get(self.game_biz, "")

    @property
    def _act_id(self) -> str:
        return _ACT_IDS.get(self.game_biz, "")

    @property
    def _signgame(self) -> str:
        return _SIGNGAME.get(self.game_biz, "")

    # ------------------------------------------------------------------
    # HTTP interno con retry
    # ------------------------------------------------------------------

    async def _get(
        self,
        url: str,
        params: typing.Optional[dict] = None,
        headers: typing.Optional[dict] = None,
    ) -> typing.Any:
        """GET autenticado con retry ante rate limits."""
        @tenacity.retry(**_RETRY_KWARGS)
        async def _inner() -> typing.Any:
            return await self.session.request("GET", url, params=params, headers=headers)
        return await _inner()

    async def _post(
        self,
        url: str,
        data: typing.Optional[dict] = None,
        params: typing.Optional[dict] = None,
        headers: typing.Optional[dict] = None,
    ) -> typing.Any:
        """POST autenticado con retry ante rate limits.
        Usa 'body' porque HoyoSession.request() así lo requiere.
        """
        @tenacity.retry(**_RETRY_KWARGS)
        async def _inner() -> typing.Any:
            return await self.session.request(
                "POST", url, params=params, body=data, headers=headers
            )
        return await _inner()

    # ------------------------------------------------------------------
    # Check-in diario
    # ------------------------------------------------------------------

    def _checkin_headers(self) -> dict:
        """
        Headers de check-in según región.
        CN usa device_id de la sesión (persistente) para simular mismo dispositivo.
        """
        headers: dict[str, str] = {"x-rpc-signgame": self._signgame}

        if not self.is_chinese_region:
            headers["Referer"] = "https://act.hoyolab.com/"
        else:
            headers.update({
                "x-rpc-app_version":  "2.70.1",
                "x-rpc-client_type":  "5",
                "x-rpc-device_id":    self.session.device_id,
                "x-rpc-sys_version":  "12",
                "x-rpc-platform":     "android",
                "x-rpc-channel":      "miyousheluodi",
                "x-rpc-device_model": str(self.session.account_id or ""),
            })
            try:
                from _ds_utility import generate_dynamic_secret  # type: ignore[import]
                headers["ds"] = generate_dynamic_secret(_DS_SALT_CN_SIGNIN)
            except ImportError:
                pass

        return headers

    def _checkin_params(self, lang: str = "en") -> dict:
        params: dict[str, typing.Any] = {"act_id": self._act_id}
        if not self.is_chinese_region:
            params["lang"] = lang
        else:
            params["uid"] = self.uid
            params["region"] = self.server
        return params

    async def get_daily_reward_info(self, *, lang: str = "en") -> dict:
        """
        Estado del check-in diario.

        Devuelve:
            is_sign         bool
            total_sign_day  int
        """
        data = await self._get(
            f"{self._reward_base_url}/info",
            params=self._checkin_params(lang),
            headers=self._checkin_headers(),
        )
        return {
            "is_sign":        data.get("is_sign", False),
            "total_sign_day": data.get("total_sign_day", 0),
        }

    async def claim_daily_reward(self, *, lang: str = "en") -> dict:
        """
        Realiza el check-in diario si no se ha hecho aún.

        Devuelve:
            message         str
            already_done    bool
            total_sign_day  int
        """
        info = await self.get_daily_reward_info(lang=lang)

        if info["is_sign"]:
            logger.info("[%s] uid=%s ya hizo check-in hoy (día %d).",
                        self.GAME_NAME, self.uid, info["total_sign_day"])
            return {"message": "Check-in ya realizado hoy.",
                    "already_done": True,
                    "total_sign_day": info["total_sign_day"]}

        await self._post(
            f"{self._reward_base_url}/sign",
            params=self._checkin_params(lang),
            headers=self._checkin_headers(),
        )

        info_after = await self.get_daily_reward_info(lang=lang)
        logger.info("[%s] uid=%s Check-in realizado (día %d).",
                    self.GAME_NAME, self.uid, info_after["total_sign_day"])
        return {"message": "Check-in realizado.",
                "already_done": False,
                "total_sign_day": info_after["total_sign_day"]}

    # ------------------------------------------------------------------
    # Mimo viajero
    # ------------------------------------------------------------------

    def _check_mimo_support(self) -> None:
        if self.GAME_ID == 0:
            raise NotImplementedError(f"{self.GAME_NAME} no tiene soporte de Mimo Viajero.")
        if self.is_chinese_region:
            raise NotImplementedError("El Mimo Viajero solo está disponible en región OS.")

    def _mimo_url(self, endpoint: str) -> str:
        if self.GAME_ID == 2:
            return f"{_MIMO_BASE_URL}/qiuqiu/{endpoint.replace('-', '_')}"
        return f"{_MIMO_BASE_URL}/{endpoint}"

    async def _get_mimo_version_id(self) -> int:
        if self._mimo_version_id is not None:
            return self._mimo_version_id

        data = await self._get(
            self._mimo_url("index"),
            params={"game_id": self.GAME_ID, "lang": "en"},
        )

        if self.GAME_ID == 2:
            items = [i["act_info"] for i in data.get("act_list", []) if "act_info" in i]
        else:
            items = data.get("list", [])

        if not items:
            raise HoyoAPIError(0, f"Mimo: respuesta vacía (game_id={self.GAME_ID})", data)

        entry = next(
            (i for i in items if i.get("game_id") == self.GAME_ID or i.get("game") == self.GAME_ID),
            items[0],
        )
        if entry is items[0] and len(items) > 1:
            logger.debug("Mimo: sin coincidencia exacta game_id=%d, usando primer item.", self.GAME_ID)

        version_id = entry.get("version_id") or entry.get("id")
        if not version_id:
            raise HoyoAPIError(0, f"Mimo: version_id no encontrado en: {entry}", data)

        self._mimo_version_id = int(version_id)
        logger.debug("[%s] Mimo version_id=%d.", self.GAME_NAME, self._mimo_version_id)
        return self._mimo_version_id

    async def get_mimo_tasks(self, *, lang: str = "en") -> list[dict]:
        """Tareas del mimo. Campos: task_id, name, point, status."""
        self._check_mimo_support()
        version_id = await self._get_mimo_version_id()
        data = await self._get(
            self._mimo_url("task-list"),
            params={"game_id": self.GAME_ID, "lang": lang, "version_id": version_id},
        )
        return data.get("task_list", [])

    async def claim_mimo_task_reward(self, task_id: int, *, lang: str = "en") -> None:
        """Reclama recompensa de tarea mimo. Genshin usa POST; resto usa GET."""
        self._check_mimo_support()
        version_id = await self._get_mimo_version_id()
        params = {"task_id": task_id, "game_id": self.GAME_ID,
                  "lang": lang, "version_id": version_id}
        if self.GAME_ID == 2:
            await self._post(self._mimo_url("receive-point"), params=params)
        else:
            await self._get(self._mimo_url("receive-point"), params=params)

    async def get_mimo_shop_items(self, *, lang: str = "en") -> list[dict]:
        """Items de la tienda mimo. Campos: award_id, name, cost."""
        self._check_mimo_support()
        version_id = await self._get_mimo_version_id()
        data = await self._get(
            self._mimo_url("exchange-list"),
            params={"game_id": self.GAME_ID, "lang": lang, "version_id": version_id},
        )
        return data.get("exchange_award_list", [])

    async def buy_mimo_currency(self, *, lang: str = "en") -> list[dict]:
        """
        Compra monedas premium en la tienda mimo.
        Incluye delay entre compras para evitar patrones robóticos.

        Devuelve: [{item_name, code}, ...]
        """
        self._check_mimo_support()
        keywords = CURRENCY_ITEM_KEYWORDS.get(self.game_biz, [])
        if not keywords:
            logger.warning("[%s] Sin keywords de moneda para %s.", self.GAME_NAME, self.game_biz)
            return []

        items = await self.get_mimo_shop_items(lang=lang)
        version_id = await self._get_mimo_version_id()
        results: list[dict] = []
        first = True

        for item in items:
            name_lower = item.get("name", "").lower()
            if not any(kw.lower() in name_lower for kw in keywords):
                continue
            award_id = item.get("award_id")
            if not award_id:
                continue

            if not first:
                await random_delay(1.5, 4.0)
            first = False

            try:
                data = await self._post(
                    self._mimo_url("exchange"),
                    data={"award_id": award_id, "game_id": self.GAME_ID,
                          "lang": lang, "version_id": version_id},
                )
                code = data.get("exchange_code", "")
                logger.info("[%s] Comprado '%s' → código: %s",
                            self.GAME_NAME, item.get("name"), code)
                results.append({"item_name": item.get("name"), "code": code})
            except HoyoAPIError as e:
                logger.warning("[%s] Error comprando '%s': %s",
                               self.GAME_NAME, item.get("name"), e)

        return results

    # ------------------------------------------------------------------
    # Canje de códigos
    # ------------------------------------------------------------------

    async def redeem_code(self, code: str, *, lang: str = "en") -> None:
        """
        Canjea un código regalo. OS únicamente.
        Genshin: GET. StarRail/ZZZ: POST. Honkai: no soportado.
        """
        base_url = _CODE_URLS.get(self.game_biz)
        if not base_url:
            raise NotImplementedError(f"{self.GAME_NAME} no soporta canje de códigos.")
        if self.is_chinese_region:
            raise NotImplementedError("El canje de códigos solo está disponible en OS.")

        lang_short = lang[:2] if len(lang) > 2 else lang
        origin = _CODE_ORIGINS.get(self.game_biz, "https://hoyoverse.com")
        headers = {"Origin": origin, "Referer": f"{origin}/"}

        if self.game_biz in _CODE_POST_GAMES:
            await self._post(
                base_url,
                data={
                    "cdkey":       code,
                    "device_uuid": self.session.device_id,
                    "game_biz":    self.game_biz,
                    "lang":        lang_short,
                    "platform":    "4",
                    "region":      self.server,
                    "t":           int(time.time()),
                    "uid":         int(self.uid),
                },
                headers=headers,
            )
        else:
            await self._get(
                base_url,
                params={"uid": self.uid, "region": self.server,
                        "lang": lang_short, "cdkey": code, "game_biz": self.game_biz},
                headers=headers,
            )

        logger.info("[%s] uid=%s Código '%s' canjeado.", self.GAME_NAME, self.uid, code)

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (f"<{self.__class__.__name__} uid={self.uid} "
                f"nickname={self.nickname!r} level={self.level} server={self.server!r}>")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_game(session: "HoyoSession", account: dict) -> "BaseGame":
    """Instancia la subclase correcta según game_biz. Imports locales = sin circulares."""
    from HoyoWrapper.genshin_game import GenshinGame    # noqa: PLC0415
    from HoyoWrapper.starrail_game import StarRailGame  # noqa: PLC0415
    from HoyoWrapper.zzz_game import ZZZGame            # noqa: PLC0415
    from HoyoWrapper.honkai_game import HonkaiGame      # noqa: PLC0415

    _REGISTRY: dict[str, type[BaseGame]] = {
        "hk4e_global":  GenshinGame,
        "hk4e_cn":      GenshinGame,
        "hkrpg_global": StarRailGame,
        "hkrpg_cn":     StarRailGame,
        "nap_global":   ZZZGame,
        "nap_cn":       ZZZGame,
        "bh3_global":   HonkaiGame,
        "bh3_cn":       HonkaiGame,
    }
    cls = _REGISTRY.get(account.get("game_biz", ""), BaseGame)
    return cls(session, account)
