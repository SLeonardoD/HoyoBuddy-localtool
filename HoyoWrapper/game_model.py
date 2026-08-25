"""
HoyoWrapper/game_model.py
-------------------------
Clase base para todos los modulos de juego.

Correcciones respecto a la version anterior (basadas en genshin.py actual):
  - URL base mimo: miyoubi -> e2023mimotravel
  - Prefijo Genshin mimo: /qiuqiu/ -> /nata/
  - URL check-in hk4e_cn corregida
  - URL check-in nap_global corregida (sg-act-nap-api)
  - URL check-in bh3_global corregida (event/mani)
  - act_id hk4e_cn, nap_cn, bh3_cn corregidos
  - redeem ZZZ: POST -> GET (igual que Genshin)
  - finish_mimo_task() implementado para tareas completables via web
  - Logging exhaustivo en todas las operaciones

Medidas anti-deteccion mantenidas:
  - device_id persistente de HoyoSession.
  - x-rpc-device_model en headers CN.
  - Retry con backoff exponencial + jitter (tenacity).
  - Delays aleatorios entre operaciones multiples.

Tipos de tareas mimo completables via finish_mimo_task:
  VISIT(2), COMMENT(3), VIEW_TOPIC(4), TRAILER(10) y similares.
  NO completables: GI_GAME(6), HSR_GAME(8), ZZZ_GAME(16),
  GI_LOGIN(5), ZZZ_DAILY_LOGIN(12), ZZZ_CONSECUTIVE_LOGIN(13).
"""

from __future__ import annotations

import asyncio
import random
import time
import typing

import tenacity
from loguru import logger

if typing.TYPE_CHECKING:
    from HoyoWrapper.session import HoyoSession

from HoyoWrapper.session import HoyoAPIError

__all__ = [
    "BaseGame",
    "GAME_BIZ_MAP",
    "GAME_ID_MAP",
    "CURRENCY_ITEM_KEYWORDS",
    "MIMO_FINISHABLE_TASK_TYPES",
    "create_game",
    "random_delay",
]

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

# Tipos de tareas mimo que se pueden completar via finish_mimo_task()
# Basado en MimoTaskType de genshin.py/models/hoyolab/mimo.py
# NO incluir: GI_LOGIN(5), GI_GAME(6), HSR_GAME(8),
#             ZZZ_DAILY_LOGIN(12), ZZZ_CONSECUTIVE_LOGIN(13), ZZZ_GAME(16)
# Estos requieren accion real dentro del juego.
MIMO_FINISHABLE_TASK_TYPES: frozenset[int] = frozenset({
    1,   # FINISHABLE — tutoriales y similares
    2,   # VISIT — visitar grupo de interes
    3,   # COMMENT — participar en interacciones
    4,   # VIEW_TOPIC — ver un tema
    10,  # TRAILER — ver trailer
})

# ---------------------------------------------------------------------------
# URLs base de los endpoints
# ---------------------------------------------------------------------------

# Check-in diario. Fuente: genshin.py/routes.py (REWARD_URL).
# Correcciones: hk4e_cn, nap_global, bh3_global actualizados.
_REWARD_URLS: dict[str, str] = {
    "hk4e_global":  "https://sg-hk4e-api.hoyolab.com/event/sol",
    "hk4e_cn":      "https://api-takumi.mihoyo.com/event/luna/",
    "hkrpg_global": "https://sg-public-api.hoyolab.com/event/luna/os",
    "hkrpg_cn":     "https://api-takumi.mihoyo.com/event/luna/",
    "nap_global":   "https://sg-act-nap-api.hoyolab.com/event/luna/zzz/os",
    "nap_cn":       "https://api-takumi.mihoyo.com/event/luna/zzz",
    "bh3_global":   "https://sg-public-api.hoyolab.com/event/mani",
    "bh3_cn":       "https://api-takumi.mihoyo.com/event/luna/",
}

# act_id requerido en check-in. Fuente: genshin.py/routes.py.
# Correcciones: hk4e_cn, nap_cn, bh3_cn actualizados.
_ACT_IDS: dict[str, str] = {
    "hk4e_global":  "e202102251931481",
    "hk4e_cn":      "e202311201442471",
    "hkrpg_global": "e202303301540311",
    "hkrpg_cn":     "e202304121516551",
    "nap_global":   "e202406031448091",
    "nap_cn":       "e202406242138391",
    "bh3_global":   "e202110291205111",
    "bh3_cn":       "e202306201626331",
}

# x-rpc-signgame por juego. Fuente: genshin.py/daily.py.
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

# URLs de canje de codigos. Fuente: genshin.py/routes.py (CODE_URL).
# Correccion: ZZZ usa GET (igual que Genshin), no POST.
_CODE_URLS: dict[str, str] = {
    "hk4e_global":  "https://sg-hk4e-api.hoyoverse.com/common/apicdkey/api/webExchangeCdkey",
    "hkrpg_global": "https://sg-hkrpg-api.hoyoverse.com/common/apicdkey/api/webExchangeCdkeyRisk",
    "nap_global":   "https://public-operation-nap.hoyoverse.com/common/apicdkey/api/webExchangeCdkey",
}

# Solo StarRail usa POST para redeem. Genshin y ZZZ usan GET.
# Fuente: genshin.py hoyolab.py redeem_code():
#   method="POST" if game is types.Game.STARRAIL else "GET"
_CODE_POST_GAMES: frozenset[str] = frozenset({"hkrpg_global"})

_CODE_ORIGINS: dict[str, str] = {
    "hk4e_global":  "https://genshin.hoyoverse.com",
    "hkrpg_global": "https://hsr.hoyoverse.com",
    "nap_global":   "https://zenless.hoyoverse.com",
}

# URL base mimo. Fuente: genshin.py/routes.py (MIMO_URL).
# Correccion: miyoubi -> e2023mimotravel
_MIMO_BASE_URL = "https://sg-public-api.hoyolab.com/event/e2023mimotravel"

_DS_SALT_CN_SIGNIN = "xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs"

# ---------------------------------------------------------------------------
# Utilidades anti-deteccion
# ---------------------------------------------------------------------------

async def random_delay(min_s: float = 1.0, max_s: float = 3.0) -> None:
    """
    Espera aleatoria con decimales entre operaciones para evitar patrones
    de timing robotico. Valores tipicos: 1.3s, 2.7s, etc.
    """
    delay = round(random.uniform(min_s, max_s), 2)
    logger.debug("[GAME] Anti-deteccion: delay de {}s.", delay)
    await asyncio.sleep(delay)


def _is_rate_limit(exc: BaseException) -> bool:
    """True si la excepcion es un rate limit de HoYoverse."""
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
    before_sleep=tenacity.before_sleep_log(logger, "WARNING"),
    reraise=True,
)

# ---------------------------------------------------------------------------
# Clase base
# ---------------------------------------------------------------------------

class BaseGame:
    """
    Clase base para los modulos de juego.
    Las subclases declaran solo: GAME_BIZ, GAME_NAME, GAME_ID.
    """

    GAME_BIZ:  str = ""
    GAME_NAME: str = ""
    GAME_ID:   int = 0

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
        async for attempt in tenacity.AsyncRetrying(**_RETRY_KWARGS):
            with attempt:
                return await self.session.request(
                    "GET", url, params=params, headers=headers
                )

    async def _post(
        self,
        url: str,
        data: typing.Optional[dict] = None,
        params: typing.Optional[dict] = None,
        headers: typing.Optional[dict] = None,
    ) -> typing.Any:
        """POST autenticado con retry ante rate limits."""
        async for attempt in tenacity.AsyncRetrying(**_RETRY_KWARGS):
            with attempt:
                return await self.session.request(
                    "POST", url, params=params, body=data, headers=headers
                )

    # ------------------------------------------------------------------
    # Check-in diario
    # ------------------------------------------------------------------

    def _checkin_headers(self) -> dict:
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
        """Estado del check-in diario. Devuelve is_sign y total_sign_day."""
        url = f"{self._reward_base_url}/info"
        logger.debug(
            "[CHECKIN] get_daily_reward_info | game={} uid={} url={} params={}",
            self.GAME_NAME, self.uid, url, self._checkin_params(lang)
        )
        try:
            data = await self._get(
                url,
                params=self._checkin_params(lang),
                headers=self._checkin_headers(),
            )
            logger.debug(
                "[CHECKIN] info response | game={} uid={} is_sign={} total_sign_day={}",
                self.GAME_NAME, self.uid,
                data.get("is_sign"), data.get("total_sign_day")
            )
            return {
                "is_sign":        data.get("is_sign", False),
                "total_sign_day": data.get("total_sign_day", 0),
            }
        except HoyoAPIError as e:
            logger.error(
                "[CHECKIN] Error en get_daily_reward_info | game={} uid={} "
                "retcode={} message={!r} raw={}",
                self.GAME_NAME, self.uid, e.retcode, e.message, e.raw
            )
            raise

    async def claim_daily_reward(self, *, lang: str = "en") -> dict:
        """Realiza el check-in diario si no se ha hecho aun hoy."""
        logger.debug(
            "[CHECKIN] claim_daily_reward iniciado | game={} uid={}",
            self.GAME_NAME, self.uid
        )
        info = await self.get_daily_reward_info(lang=lang)

        if info["is_sign"]:
            logger.info(
                "[CHECKIN] Ya realizado hoy | game={} uid={} dia={}",
                self.GAME_NAME, self.uid, info["total_sign_day"]
            )
            return {
                "message":        "Check-in ya realizado hoy.",
                "already_done":   True,
                "total_sign_day": info["total_sign_day"],
            }

        sign_url = f"{self._reward_base_url}/sign"
        logger.debug(
            "[CHECKIN] Enviando POST a /sign | game={} uid={} url={}",
            self.GAME_NAME, self.uid, sign_url
        )

        try:
            resp = await self._post(
                sign_url,
                params=self._checkin_params(lang),
                headers=self._checkin_headers(),
            )
            logger.debug(
                "[CHECKIN] Respuesta /sign | game={} uid={} resp={}",
                self.GAME_NAME, self.uid, resp
            )
        except HoyoAPIError as e:
            logger.error(
                "[CHECKIN] Error en /sign | game={} uid={} "
                "retcode={} message={!r} raw={}",
                self.GAME_NAME, self.uid, e.retcode, e.message, e.raw
            )
            raise

        info_after = await self.get_daily_reward_info(lang=lang)
        logger.info(
            "[CHECKIN] Completado | game={} uid={} dia={}",
            self.GAME_NAME, self.uid, info_after["total_sign_day"]
        )
        return {
            "message":        "Check-in realizado.",
            "already_done":   False,
            "total_sign_day": info_after["total_sign_day"],
        }

    # ------------------------------------------------------------------
    # Mimo viajero
    # ------------------------------------------------------------------

    def _check_mimo_support(self) -> None:
        if self.GAME_ID == 0:
            raise NotImplementedError(
                f"{self.GAME_NAME} no tiene soporte de Mimo Viajero."
            )
        if self.is_chinese_region:
            raise NotImplementedError(
                "El Mimo Viajero solo esta disponible en region OS (HoYoLab)."
            )

    def _mimo_url(self, endpoint: str) -> str:
        """
        URL del endpoint mimo.
        Genshin (GAME_ID=2): /nata/<endpoint> con guiones->guiones_bajos.
        Resto: /<endpoint> directamente.
        Fuente: genshin.py/hoyolab.py/_request_mimo()
        """
        if self.GAME_ID == 2:
            return f"{_MIMO_BASE_URL}/nata/{endpoint.replace('-', '_')}"
        return f"{_MIMO_BASE_URL}/{endpoint}"

    async def _get_mimo_version_id(self) -> int:
        """Obtiene el version_id activo del mimo. Cacheado por instancia."""
        if self._mimo_version_id is not None:
            return self._mimo_version_id

        url = self._mimo_url("index")
        logger.debug(
            "[MIMO] _get_mimo_version_id | game={} game_id={} url={}",
            self.GAME_NAME, self.GAME_ID, url
        )

        try:
            data = await self._get(
                url,
                params={"game_id": self.GAME_ID, "lang": "en"},
            )
            logger.debug(
                "[MIMO] index response | game={} data_keys={}",
                self.GAME_NAME, list(data.keys()) if isinstance(data, dict) else type(data)
            )
        except HoyoAPIError as e:
            logger.error(
                "[MIMO] Error en index | game={} retcode={} message={!r} raw={}",
                self.GAME_NAME, e.retcode, e.message, e.raw
            )
            raise

        if self.GAME_ID == 2:
            items = [i["act_info"] for i in data.get("act_list", []) if "act_info" in i]
        else:
            items = data.get("list", [])

        logger.debug(
            "[MIMO] items encontrados en index | game={} count={}",
            self.GAME_NAME, len(items)
        )

        if not items:
            logger.error(
                "[MIMO] Sin items en index | game={} game_id={} raw_data={}",
                self.GAME_NAME, self.GAME_ID, data
            )
            raise HoyoAPIError(
                0, f"Mimo: respuesta vacia (game_id={self.GAME_ID})", data
            )

        entry = next(
            (i for i in items
             if i.get("game_id") == self.GAME_ID or i.get("game") == self.GAME_ID),
            items[0],
        )
        if entry is items[0] and len(items) > 1:
            logger.debug(
                "[MIMO] Sin coincidencia exacta game_id={}, usando primer item: {}",
                self.GAME_ID, entry
            )

        version_id = entry.get("version_id") or entry.get("id")
        if not version_id:
            logger.error(
                "[MIMO] version_id no encontrado | entry={} data={}",
                entry, data
            )
            raise HoyoAPIError(
                0, f"Mimo: version_id no encontrado en: {entry}", data
            )

        self._mimo_version_id = int(version_id)
        logger.info(
            "[MIMO] version_id obtenido | game={} version_id={}",
            self.GAME_NAME, self._mimo_version_id
        )
        return self._mimo_version_id

    async def get_mimo_tasks(self, *, lang: str = "en") -> list[dict]:
        """Lista de tareas del mimo viajero."""
        self._check_mimo_support()
        version_id = await self._get_mimo_version_id()
        url = self._mimo_url("task-list")
        params = {"game_id": self.GAME_ID, "lang": lang, "version_id": version_id}

        logger.debug(
            "[MIMO] get_mimo_tasks | game={} url={} params={}",
            self.GAME_NAME, url, params
        )

        try:
            data = await self._get(url, params=params)
            tasks = data.get("task_list", [])
            logger.debug(
                "[MIMO] task-list response | game={} total={} "
                "finished={} ongoing={} claimed={}",
                self.GAME_NAME,
                len(tasks),
                sum(1 for t in tasks if t.get("status") == 1),
                sum(1 for t in tasks if t.get("status") == 2),
                sum(1 for t in tasks if t.get("status") == 3),
            )
            for t in tasks:
                logger.debug(
                    "[MIMO] tarea | id={} name={!r} type={} status={} point={}",
                    t.get("task_id"), t.get("task_name") or t.get("name"),
                    t.get("task_type") or t.get("type"),
                    t.get("status"), t.get("point")
                )
            return tasks
        except HoyoAPIError as e:
            logger.error(
                "[MIMO] Error en task-list | game={} retcode={} message={!r} raw={}",
                self.GAME_NAME, e.retcode, e.message, e.raw
            )
            raise

    async def finish_mimo_task(self, task_id: int, *, lang: str = "en") -> None:
        """
        Completa una tarea mimo via API (tareas tipo VISIT, VIEW_TOPIC, TRAILER, etc.).

        Solo funciona con tareas que no requieren accion real en el juego.
        Tipos completables: 1(FINISHABLE), 2(VISIT), 3(COMMENT), 4(VIEW_TOPIC), 10(TRAILER).
        NO usar con: GI_LOGIN(5), GI_GAME(6), HSR_GAME(8),
                     ZZZ_DAILY_LOGIN(12), ZZZ_CONSECUTIVE_LOGIN(13), ZZZ_GAME(16).

        Fuente: genshin.py/hoyolab.py/finish_mimo_task() — usa POST con body.
        """
        self._check_mimo_support()
        version_id = await self._get_mimo_version_id()
        url = self._mimo_url("finish-task")
        body = {
            "task_id":    task_id,
            "game_id":    self.GAME_ID,
            "lang":       lang,
            "version_id": version_id,
        }

        logger.debug(
            "[MIMO] finish_mimo_task | game={} task_id={} url={} body={}",
            self.GAME_NAME, task_id, url, body
        )

        try:
            resp = await self._post(url, data=body)
            logger.info(
                "[MIMO] finish_mimo_task completado | game={} task_id={} resp={}",
                self.GAME_NAME, task_id, resp
            )
        except HoyoAPIError as e:
            logger.error(
                "[MIMO] Error en finish-task | game={} task_id={} "
                "retcode={} message={!r} raw={}",
                self.GAME_NAME, task_id, e.retcode, e.message, e.raw
            )
            raise

    async def claim_mimo_task_reward(self, task_id: int, *, lang: str = "en") -> None:
        """
        Reclama la recompensa de una tarea mimo completada.
        Genshin (GAME_ID=2) usa POST; resto usa GET.
        Fuente: genshin.py/hoyolab.py/claim_mimo_task_reward()
        """
        self._check_mimo_support()
        version_id = await self._get_mimo_version_id()
        url = self._mimo_url("receive-point")
        params = {
            "task_id":    task_id,
            "game_id":    self.GAME_ID,
            "lang":       lang,
            "version_id": version_id,
        }

        logger.debug(
            "[MIMO] claim_mimo_task_reward | game={} task_id={} "
            "method={} url={} params={}",
            self.GAME_NAME, task_id,
            "POST" if self.GAME_ID == 2 else "GET",
            url, params
        )

        try:
            if self.GAME_ID == 2:
                resp = await self._post(url, params=params)
            else:
                resp = await self._get(url, params=params)
            logger.info(
                "[MIMO] recompensa reclamada | game={} task_id={} resp={}",
                self.GAME_NAME, task_id, resp
            )
        except HoyoAPIError as e:
            logger.error(
                "[MIMO] Error en receive-point | game={} task_id={} "
                "retcode={} message={!r} raw={}",
                self.GAME_NAME, task_id, e.retcode, e.message, e.raw
            )
            raise

    async def get_mimo_shop_items(self, *, lang: str = "en") -> list[dict]:
        """Items disponibles en la tienda mimo."""
        self._check_mimo_support()
        version_id = await self._get_mimo_version_id()
        url = self._mimo_url("exchange-list")
        params = {"game_id": self.GAME_ID, "lang": lang, "version_id": version_id}

        logger.debug(
            "[MIMO] get_mimo_shop_items | game={} url={} params={}",
            self.GAME_NAME, url, params
        )

        try:
            data = await self._get(url, params=params)
            items = data.get("exchange_award_list", [])
            logger.debug(
                "[MIMO] exchange-list response | game={} total_items={}",
                self.GAME_NAME, len(items)
            )
            for item in items:
                logger.debug(
                    "[MIMO] item tienda | id={} name={!r} cost={} "
                    "stock={} status={}",
                    item.get("award_id"), item.get("name"),
                    item.get("cost"), item.get("stock"),
                    item.get("status")
                )
            return items
        except HoyoAPIError as e:
            logger.error(
                "[MIMO] Error en exchange-list | game={} "
                "retcode={} message={!r} raw={}",
                self.GAME_NAME, e.retcode, e.message, e.raw
            )
            raise

    async def buy_mimo_currency(self, *, lang: str = "en") -> list[dict]:
        """
        Compra monedas premium en la tienda mimo.
        Incluye delay entre compras para evitar patrones roboticos.
        """
        self._check_mimo_support()
        keywords = CURRENCY_ITEM_KEYWORDS.get(self.game_biz, [])
        if not keywords:
            logger.warning(
                "[MIMO] Sin keywords de moneda para {} ({})",
                self.GAME_NAME, self.game_biz
            )
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

            url = self._mimo_url("exchange")
            body = {
                "award_id":   award_id,
                "game_id":    self.GAME_ID,
                "lang":       lang,
                "version_id": version_id,
            }
            logger.debug(
                "[MIMO] buy_mimo_currency | game={} item={!r} "
                "award_id={} url={} body={}",
                self.GAME_NAME, item.get("name"), award_id, url, body
            )

            try:
                data = await self._post(url, data=body)
                code = data.get("exchange_code", "")
                logger.info(
                    "[MIMO] Compra exitosa | game={} item={!r} "
                    "code={} resp={}",
                    self.GAME_NAME, item.get("name"), code, data
                )
                results.append({"item_name": item.get("name"), "code": code})
            except HoyoAPIError as e:
                logger.error(
                    "[MIMO] Error en exchange | game={} item={!r} "
                    "retcode={} message={!r} raw={}",
                    self.GAME_NAME, item.get("name"),
                    e.retcode, e.message, e.raw
                )

        return results

    # ------------------------------------------------------------------
    # Canje de codigos regalo
    # ------------------------------------------------------------------

    async def redeem_code(self, code: str, *, lang: str = "en") -> None:
        """
        Canjea un codigo regalo.
        StarRail: POST. Genshin y ZZZ: GET.
        Fuente: genshin.py/hoyolab.py/redeem_code()
        """
        base_url = _CODE_URLS.get(self.game_biz)
        if not base_url:
            raise NotImplementedError(
                f"{self.GAME_NAME} no soporta canje de codigos via esta API."
            )
        if self.is_chinese_region:
            raise NotImplementedError(
                "El canje de codigos via API solo esta disponible en OS."
            )

        lang_short = lang[:2] if len(lang) > 2 else lang
        origin = _CODE_ORIGINS.get(self.game_biz, "https://hoyoverse.com")
        headers = {"Origin": origin, "Referer": f"{origin}/"}
        method = "POST" if self.game_biz in _CODE_POST_GAMES else "GET"

        logger.debug(
            "[REDEEM] redeem_code | game={} uid={} code={} "
            "method={} url={} lang={}",
            self.GAME_NAME, self.uid, code, method, base_url, lang_short
        )

        try:
            if method == "POST":
                resp = await self._post(
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
                resp = await self._get(
                    base_url,
                    params={
                        "uid":      self.uid,
                        "region":   self.server,
                        "lang":     lang_short,
                        "cdkey":    code,
                        "game_biz": self.game_biz,
                    },
                    headers=headers,
                )
            logger.info(
                "[REDEEM] Codigo canjeado | game={} uid={} code={} resp={}",
                self.GAME_NAME, self.uid, code, resp
            )
        except HoyoAPIError as e:
            logger.error(
                "[REDEEM] Error al canjear | game={} uid={} code={} "
                "retcode={} message={!r} raw={}",
                self.GAME_NAME, self.uid, code, e.retcode, e.message, e.raw
            )
            raise

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} uid={self.uid} "
            f"nickname={self.nickname!r} level={self.level} "
            f"server={self.server!r}>"
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_game(session: "HoyoSession", account: dict) -> "BaseGame":
    """Instancia la subclase correcta segun game_biz."""
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
