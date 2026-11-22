"""
HoyoWrapper/game_model.py
-------------------------
Clase base para todos los modulos de juego.

Los valores que HoYoverse cambia regularmente (act_id, URLs, salts, etc.)
viven en config.py — este archivo solo contiene logica. Ver config.py
para el detalle de cada valor y su fuente de verificacion.

Medidas anti-deteccion mantenidas:
  - device_id persistente de HoyoSession.
  - x-rpc-device_model en headers CN.
  - Retry con backoff exponencial + jitter (tenacity).
  - Delays aleatorios entre operaciones multiples (ver config.DELAY_*).

Tipos de tareas mimo completables via finish_mimo_task:
  VISIT(2), COMMENT(3), VIEW_TOPIC(4), TRAILER(10) y similares.
  NO completables: GI_GAME(6), HSR_GAME(8), ZZZ_GAME(16),
  GI_LOGIN(5), ZZZ_DAILY_LOGIN(12), ZZZ_CONSECUTIVE_LOGIN(13).
"""

from __future__ import annotations

import time
import typing

import tenacity
from loguru import logger

if typing.TYPE_CHECKING:
    from HoyoWrapper.session import HoyoSession

import config
from HoyoWrapper import ds_utils
from HoyoWrapper.timing import random_delay
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
# Re-exports desde config.py (mantiene compatibilidad con codigo/tests
# existentes que importan estos nombres directamente de game_model).
# ---------------------------------------------------------------------------

GAME_BIZ_MAP = config.GAME_BIZ_MAP
GAME_ID_MAP = config.GAME_ID_MAP
CURRENCY_ITEM_KEYWORDS = config.CURRENCY_ITEM_KEYWORDS
MIMO_FINISHABLE_TASK_TYPES = config.MIMO_FINISHABLE_TASK_TYPES

# ---------------------------------------------------------------------------
# Alias locales cortos de config.py (solo para no tener que escribir
# "config." delante de cada uso dentro de este archivo).
# ---------------------------------------------------------------------------

_REWARD_URLS = config.REWARD_URLS
_ACT_IDS = config.ACT_IDS
_SIGNGAME = config.SIGNGAME
_CODE_URLS = config.CODE_URLS
_CODE_POST_GAMES = config.CODE_POST_GAMES
_CODE_ORIGINS = config.CODE_ORIGINS
_MIMO_BASE_URL = config.MIMO_BASE_URL
_DS_SALT_CN_SIGNIN = config.DS_SALT_CN_SIGNIN

# ---------------------------------------------------------------------------
# Utilidades anti-deteccion
#
# random_delay() vive en HoyoWrapper/timing.py (compartido con
# auth_manager.py) — se re-importa y re-exporta aca para no romper el
# import existente `from HoyoWrapper.game_model import random_delay`
# usado en app.py, session_manager.py y los tests.
# ---------------------------------------------------------------------------

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
    wait=tenacity.wait_random_exponential(
        multiplier=config.RETRY_WAIT_MULTIPLIER,
        min=config.RETRY_WAIT_MIN,
        max=config.RETRY_WAIT_MAX,
    ),
    stop=tenacity.stop_after_attempt(config.RETRY_STOP_ATTEMPTS),
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
                "x-rpc-app_version":  config.CN_CHECKIN_APP_VERSION,
                "x-rpc-client_type":  config.CN_CHECKIN_CLIENT_TYPE,
                "x-rpc-device_id":    self.session.device_id,
                "x-rpc-sys_version":  config.CN_CHECKIN_SYS_VERSION,
                "x-rpc-platform":     config.CN_CHECKIN_PLATFORM,
                "x-rpc-channel":      config.CN_CHECKIN_CHANNEL,
                "x-rpc-device_model": str(self.session.account_id or ""),
                # BUG FIX: la version anterior intentaba
                # "from _ds_utility import generate_dynamic_secret", un
                # modulo que NO existe en el proyecto — el import siempre
                # fallaba, el ImportError se tragaba silenciosamente, y
                # el check-in CN se enviaba SIN header 'ds'. Verificado
                # contra genshin.py client/components/daily.py: el check-in
                # CN requiere ds_utility.generate_dynamic_secret(
                # DS_SALT["cn_signin"]) — la variante SIMPLE (sin body/query
                # en el hash), no generate_cn_dynamic_secret().
                "ds": ds_utils.generate_dynamic_secret(_DS_SALT_CN_SIGNIN),
            })

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

        # ANTI-BANEO: un usuario real lee la pagina de check-in (que ya
        # cargo con la info de arriba) antes de hacer click en "firmar".
        # Antes esto disparaba /sign inmediatamente despues de /info.
        await random_delay(*config.DELAY_CHECKIN_INFO_TO_SIGN)

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

        # ANTI-BANEO: espera antes de refrescar el estado, como haria
        # una pagina real esperando la confirmacion visual del check-in.
        await random_delay(*config.DELAY_CHECKIN_SIGN_TO_INFO)

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
        Genshin (GAME_ID=2): /<config.MIMO_GENSHIN_PATH_PREFIX>/<endpoint> con
        guiones->guiones_bajos. El prefijo se lee de config en cada llamada
        porque HoYoverse lo rota (ver el comentario en config.py).
        Resto: /<endpoint> directamente.
        Fuente: genshin.py/hoyolab.py/_request_mimo()
        """
        if self.GAME_ID == 2:
            prefix = config.MIMO_GENSHIN_PATH_PREFIX
            return f"{_MIMO_BASE_URL}/{prefix}/{endpoint.replace('-', '_')}"
        return f"{_MIMO_BASE_URL}/{endpoint}"

    def _mimo_lang(self, lang: typing.Optional[str] = None) -> str:
        """
        Idioma de las requests del Mimo. Un `lang` explicito manda; si no,
        Genshin (GAME_ID=2) usa config.MIMO_GENSHIN_LANG ("en-us", como
        genshin.py) y el resto config.MIMO_LANG ("en", validado en vivo).
        """
        if lang is not None:
            return lang
        return config.MIMO_GENSHIN_LANG if self.GAME_ID == 2 else config.MIMO_LANG

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
                params={"game_id": self.GAME_ID, "lang": self._mimo_lang()},
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
            None,
        )
        if entry is None:
            # Antes se avisaba tambien cuando la coincidencia ERA el primer item
            # (ej. ZZZ con 2 items en el index), lo que confundia al leer el log.
            entry = items[0]
            logger.debug(
                "[MIMO] Sin coincidencia exacta game_id={}, usando primer item: {}",
                self.GAME_ID, entry
            )
        logger.debug(
            "[MIMO] entrada del index elegida | game={} entry={}",
            self.GAME_NAME, entry
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
        # ANTI-BANEO: simula cargar la pagina/pestaña del mimo antes de
        # interactuar con ella. Solo se paga esta vez por instancia —
        # las siguientes llamadas usan el version_id cacheado y no
        # vuelven a pasar por aca.
        await random_delay(*config.DELAY_MIMO_VERSION_TO_ACTION)
        return self._mimo_version_id

    async def get_mimo_tasks(self, *, lang: typing.Optional[str] = None) -> list[dict]:
        """Lista de tareas del mimo viajero."""
        self._check_mimo_support()
        lang = self._mimo_lang(lang)
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
                "[MIMO] Error en task-list | game={} retcode={} message={!r} "
                "raw={} params={}",
                self.GAME_NAME, e.retcode, e.message, e.raw, params
            )
            raise

    async def finish_mimo_task(self, task_id: int, *, lang: typing.Optional[str] = None) -> None:
        """
        Completa una tarea mimo via API (tareas tipo VISIT, VIEW_TOPIC, TRAILER, etc.).

        Solo funciona con tareas que no requieren accion real en el juego.
        Tipos completables: 1(FINISHABLE), 2(VISIT), 3(COMMENT), 4(VIEW_TOPIC), 10(TRAILER).
        NO usar con: GI_LOGIN(5), GI_GAME(6), HSR_GAME(8),
                     ZZZ_DAILY_LOGIN(12), ZZZ_CONSECUTIVE_LOGIN(13), ZZZ_GAME(16).

        Fuente: genshin.py/hoyolab.py/finish_mimo_task() — usa POST con body.
        """
        self._check_mimo_support()
        lang = self._mimo_lang(lang)
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

    async def claim_mimo_task_reward(self, task_id: int, *, lang: typing.Optional[str] = None) -> None:
        """
        Reclama la recompensa de una tarea mimo completada.
        Genshin (GAME_ID=2) usa POST; resto usa GET.
        Fuente: genshin.py/hoyolab.py/claim_mimo_task_reward()
        """
        self._check_mimo_support()
        lang = self._mimo_lang(lang)
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

    async def get_mimo_shop_items(self, *, lang: typing.Optional[str] = None) -> list[dict]:
        """Items disponibles en la tienda mimo."""
        self._check_mimo_support()
        lang = self._mimo_lang(lang)
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
                "retcode={} message={!r} raw={} params={}",
                self.GAME_NAME, e.retcode, e.message, e.raw, params
            )
            raise

    async def buy_mimo_currency(self, *, lang: typing.Optional[str] = None) -> list[dict]:
        """
        Compra monedas premium en la tienda mimo.
        Incluye delay entre compras para evitar patrones roboticos.
        """
        self._check_mimo_support()
        lang = self._mimo_lang(lang)
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

        for item in items:
            name_lower = item.get("name", "").lower()
            if not any(kw.lower() in name_lower for kw in keywords):
                continue
            award_id = item.get("award_id")
            if not award_id:
                continue

            # ANTI-BANEO: no se manda un exchange que ya se sabe que va a
            # fallar. La propia lista indica que no se puede comprar: status
            # distinto de "canjeable" (2 sin stock, 3 limite alcanzado, 4
            # agotado) o stock 0. Antes se intentaba igual: el log del
            # 21/11/2026 muestra 4 exchange de ZZZ (Polychrome, status=2
            # stock=0) que respondieron -502006 "Out of stock" una a una.
            # Si la lista no trae el dato, no se descarta (se conserva el
            # comportamiento anterior).
            status = item.get("status")
            stock = item.get("stock")
            if (
                (status is not None and status != config.MIMO_SHOP_STATUS_EXCHANGEABLE)
                or (stock is not None and stock <= 0)
            ):
                logger.debug(
                    "[MIMO] Se omite articulo no comprable | game={} item={!r} "
                    "status={} stock={}",
                    self.GAME_NAME, item.get("name"), status, stock
                )
                continue

            # ANTI-BANEO: antes este delay se saltaba para la primera
            # compra de la lista (solo se aplicaba "entre" compras). Un
            # usuario real tambien tarda en decidirse a comprar el
            # PRIMER item, no solo entre el 2do y 3ro — asi que ahora se
            # aplica siempre, sin excepcion para el primero.
            await random_delay(*config.DELAY_BETWEEN_MIMO_PURCHASES)

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
        StarRail y ZZZ: POST. Genshin: GET.
        Fuente: genshin.py/hoyolab.py/redeem_code()
          post_request_games = {types.Game.ZZZ, types.Game.STARRAIL}
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
