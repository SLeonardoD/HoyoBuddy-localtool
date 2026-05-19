"""
game_model.py
-------------
Clase base para todos los módulos de juego.

Contiene la implementación real de check-in diario, mimo viajero y canje
de códigos. Las subclases solo necesitan declarar sus constantes de clase.

Arquitectura:
  - BaseGame implementa toda la lógica compartida.
  - Las subclases (GenshinGame, StarRailGame, etc.) son declarativas:
    solo definen GAME_BIZ, GAME_NAME, GAME_ID.
  - Escalar a un juego nuevo = crear un archivo con ~15 líneas de constantes.

Medidas anti-detección implementadas:
  - device_id tomado de HoyoSession (generado una vez en el login y
    persistido en DB). Un dispositivo real siempre tiene el mismo ID.
  - x-rpc-device_model en headers CN (igual que genshin.py).
  - Retry automático con backoff exponencial + jitter ante rate limits
    (tenacity). Retcodes -429 y HTTP 429 están cubiertos.
  - Delays con jitter aleatorio entre operaciones múltiples para no
    generar patrones de timing robótico.

Notas de compatibilidad:
  - HoyoSession.request() usa 'body' (no 'json') como parámetro del cuerpo.
  - El mimo solo está disponible en región OS (HoYoLab). CN no lo soporta.
  - Honkai Impact 3rd no tiene mimo ni canje de códigos vía esta API.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import typing
import uuid

import tenacity

if typing.TYPE_CHECKING:
    from auth_manager import HoyoSession

from auth_manager import HoyoAPIError

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

# game_biz → mimo game_id. Honkai Impact 3rd no tiene mimo.
GAME_ID_MAP: dict[str, int] = {
    "hk4e_global":  2,
    "hk4e_cn":      2,
    "hkrpg_global": 6,
    "hkrpg_cn":     6,
    "nap_global":   8,
    "nap_cn":       8,
}

# Palabras clave para identificar monedas premium en la tienda mimo.
# Búsqueda case-insensitive de subcadena.
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
# URLs base de los endpoints
# ---------------------------------------------------------------------------

# Check-in diario. Fuente: genshin.py/genshin/client/routes.py (REWARD_URL).
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

# act_id requerido en check-in. Fuente: genshin.py/genshin/client/routes.py.
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

# URLs de canje de códigos. Fuente: genshin.py/genshin/client/routes.py.
_CODE_URLS: dict[str, str] = {
    "hk4e_global":  "https://sg-hk4e-api.hoyoverse.com/common/apicdkey/api/webExchangeCdkey",
    "hkrpg_global": "https://sg-public-api.hoyoverse.com/common/apicdkey/api/webExchangeCdkeyRisk",
    "nap_global":   "https://public-operation-nap.hoyoverse.com/common/apicdkey/api/webExchangeCdkeyRisk",
}

# StarRail y ZZZ usan POST para redeem; Genshin usa GET.
_CODE_POST_GAMES: frozenset[str] = frozenset({"hkrpg_global", "nap_global"})

# Origin/Referer para redeem por juego.
_CODE_ORIGINS: dict[str, str] = {
    "hk4e_global":  "https://genshin.hoyoverse.com",
    "hkrpg_global": "https://hsr.hoyoverse.com",
    "nap_global":   "https://zenless.hoyoverse.com",
}

# URL base del mimo viajero (solo OS).
_MIMO_BASE_URL = "https://sg-public-api.hoyolab.com/event/miyoubi"

# DS salt para CN. Fuente: genshin.py/genshin/constants.py.
_DS_SALT_CN_SIGNIN = "xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs"

# ---------------------------------------------------------------------------
# Utilidades anti-detección
# ---------------------------------------------------------------------------

async def random_delay(min_s: float = 1.0, max_s: float = 3.0) -> None:
    """
    Espera un tiempo aleatorio entre min_s y max_s segundos.

    Uso entre operaciones secuenciales (check-in de varios juegos, compras
    en mimo) para evitar patrones de timing robótico que disparen los
    sistemas anti-bot de HoYoverse.
    """
    delay = random.uniform(min_s, max_s)
    logger.debug("Anti-detección: esperando %.2fs antes del siguiente request.", delay)
    await asyncio.sleep(delay)


def _is_rate_limit(exc: BaseException) -> bool:
    """
    Devuelve True si la excepción corresponde a un rate limit de HoYoverse.

    Cubre retcode -429 (HoYoAPIError) y HTTP 429 (aiohttp.ClientResponseError).
    """
    if isinstance(exc, HoyoAPIError) and exc.retcode in (-429, 429):
        return True
    try:
        import aiohttp
        if isinstance(exc, aiohttp.ClientResponseError) and exc.status == 429:
            return True
    except ImportError:
        pass
    return False


# Decorador de retry reutilizable: backoff exponencial con jitter completo.
# Cubre rate limits y errores de red transitorios.
# Se aplica en _get/_post mediante tenacity.retry().
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

    Implementa la lógica completa de check-in, mimo y redeem.
    Las subclases solo declaran: GAME_BIZ, GAME_NAME, GAME_ID.

    Parámetros:
        session     HoyoSession autenticada (incluye device_id persistente).
        account     Dict devuelto por HoyoSession.get_game_accounts():
                        game_biz, game_uid, nickname, level, region, region_name
    """

    GAME_BIZ:  str = ""
    GAME_NAME: str = ""
    GAME_ID:   int = 0    # 0 = sin soporte de mimo

    def __init__(self, session: "HoyoSession", account: dict) -> None:
        self.session = session
        self.account = account
        self._mimo_version_id: typing.Optional[int] = None

    # ------------------------------------------------------------------
    # Propiedades de conveniencia
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
    # Métodos HTTP internos con retry automático
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

        Usa 'body' en lugar de 'json' porque HoyoSession.request() así lo
        requiere por diseño (evita shadowing del módulo json).
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
        Headers para los endpoints de check-in según región.

        OS:  x-rpc-signgame + Referer.
        CN:  Además, headers de device info que reducen captchas Geetest.
             device_id tomado de HoyoSession (persistente, generado en login)
             para simular siempre el mismo dispositivo.
        """
        headers: dict[str, str] = {"x-rpc-signgame": self._signgame}

        if not self.is_chinese_region:
            headers["Referer"] = "https://act.hoyolab.com/"
        else:
            # device_id consistente con el del login → mismo "dispositivo"
            headers.update({
                "x-rpc-app_version": "2.70.1",
                "x-rpc-client_type": "5",
                "x-rpc-device_id":   self.session.device_id,
                "x-rpc-sys_version": "12",
                "x-rpc-platform":    "android",
                "x-rpc-channel":     "miyousheluodi",
                # account_id como device_model, igual que genshin.py
                "x-rpc-device_model": str(self.session.account_id or ""),
            })
            # DS dinámico opcional para CN.
            try:
                from _ds_utility import generate_dynamic_secret  # type: ignore[import]
                headers["ds"] = generate_dynamic_secret(_DS_SALT_CN_SIGNIN)
            except ImportError:
                pass

        return headers

    def _checkin_params(self, lang: str = "en") -> dict:
        """Query params para /info y /sign del check-in."""
        params: dict[str, typing.Any] = {"act_id": self._act_id}
        if not self.is_chinese_region:
            params["lang"] = lang
        else:
            params["uid"] = self.uid
            params["region"] = self.server
        return params

    async def get_daily_reward_info(self, *, lang: str = "en") -> dict:
        """
        Estado del check-in diario del usuario.

        Devuelve:
            is_sign         bool    True si ya se hizo check-in hoy.
            total_sign_day  int     Días totales firmados este mes.
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
        Realiza el check-in diario si no se ha hecho aún hoy.

        Devuelve:
            message         str     Mensaje de resultado legible.
            already_done    bool    True si el check-in ya estaba hecho.
            total_sign_day  int     Días totales firmados este mes.

        Lanza:
            HoyoAPIError    Si la API devuelve un error inesperado.
        """
        info = await self.get_daily_reward_info(lang=lang)

        if info["is_sign"]:
            logger.info(
                "[%s] uid=%s ya hizo check-in hoy (día %d del mes).",
                self.GAME_NAME, self.uid, info["total_sign_day"],
            )
            return {
                "message":        "Check-in ya realizado hoy.",
                "already_done":   True,
                "total_sign_day": info["total_sign_day"],
            }

        await self._post(
            f"{self._reward_base_url}/sign",
            params=self._checkin_params(lang),
            headers=self._checkin_headers(),
        )

        info_after = await self.get_daily_reward_info(lang=lang)
        logger.info(
            "[%s] uid=%s Check-in realizado (día %d del mes).",
            self.GAME_NAME, self.uid, info_after["total_sign_day"],
        )
        return {
            "message":        "Check-in realizado.",
            "already_done":   False,
            "total_sign_day": info_after["total_sign_day"],
        }

    # ------------------------------------------------------------------
    # Mimo viajero (solo OS, solo juegos con GAME_ID > 0)
    # ------------------------------------------------------------------

    def _check_mimo_support(self) -> None:
        """Lanza NotImplementedError si este juego/región no soporta mimo."""
        if self.GAME_ID == 0:
            raise NotImplementedError(
                f"{self.GAME_NAME} no tiene soporte de Mimo Viajero."
            )
        if self.is_chinese_region:
            raise NotImplementedError(
                "El Mimo Viajero solo está disponible en región OS (HoYoLab)."
            )

    def _mimo_url(self, endpoint: str) -> str:
        """
        URL del endpoint mimo según el juego.

        Genshin (GAME_ID=2): /qiuqiu/<endpoint> con guiones→guiones_bajos.
        Resto: /<endpoint> directamente.
        Documentado en genshin.py/hoyolab.py/_request_mimo.
        """
        if self.GAME_ID == 2:
            return f"{_MIMO_BASE_URL}/qiuqiu/{endpoint.replace('-', '_')}"
        return f"{_MIMO_BASE_URL}/{endpoint}"

    async def _get_mimo_version_id(self) -> int:
        """
        Obtiene el version_id activo del mimo para este juego.

        Dinámico (cambia cada temporada). Cacheado en la instancia.

        Estrategia de parsing:
          Genshin:  data["act_list"][N]["act_info"]
          HSR/ZZZ:  data["list"][N]
          Identificador del juego puede ser "game" o "game_id"; se prueban ambos.
        """
        if self._mimo_version_id is not None:
            return self._mimo_version_id

        data = await self._get(
            self._mimo_url("index"),
            params={"game_id": self.GAME_ID, "lang": "en"},
        )

        if self.GAME_ID == 2:
            items = [
                item["act_info"]
                for item in data.get("act_list", [])
                if "act_info" in item
            ]
        else:
            items = data.get("list", [])

        if not items:
            raise HoyoAPIError(
                0, f"Mimo: respuesta vacía al obtener index (game_id={self.GAME_ID})", data
            )

        entry = next(
            (
                i for i in items
                if i.get("game_id") == self.GAME_ID or i.get("game") == self.GAME_ID
            ),
            items[0],
        )

        if entry is items[0] and len(items) > 1:
            logger.debug(
                "Mimo: sin coincidencia exacta de game_id=%d en %d items, usando el primero.",
                self.GAME_ID, len(items),
            )

        version_id = entry.get("version_id") or entry.get("id")
        if not version_id:
            raise HoyoAPIError(
                0,
                f"Mimo: version_id no encontrado en la entrada del índice: {entry}",
                data,
            )

        self._mimo_version_id = int(version_id)
        logger.debug("[%s] Mimo version_id=%d.", self.GAME_NAME, self._mimo_version_id)
        return self._mimo_version_id

    async def get_mimo_tasks(self, *, lang: str = "en") -> list[dict]:
        """
        Lista de tareas del mimo viajero.

        Campos relevantes:
            task_id, name, point, status (0=pendiente, 1=completada, 2=reclamada)
        """
        self._check_mimo_support()
        version_id = await self._get_mimo_version_id()
        data = await self._get(
            self._mimo_url("task-list"),
            params={"game_id": self.GAME_ID, "lang": lang, "version_id": version_id},
        )
        return data.get("task_list", [])

    async def claim_mimo_task_reward(self, task_id: int, *, lang: str = "en") -> None:
        """
        Reclama la recompensa de una tarea del mimo completada.

        Genshin usa POST; el resto usa GET.
        Documentado en genshin.py/hoyolab.py/claim_mimo_task_reward.
        """
        self._check_mimo_support()
        version_id = await self._get_mimo_version_id()
        params = {
            "task_id":    task_id,
            "game_id":    self.GAME_ID,
            "lang":       lang,
            "version_id": version_id,
        }
        if self.GAME_ID == 2:
            await self._post(self._mimo_url("receive-point"), params=params)
        else:
            await self._get(self._mimo_url("receive-point"), params=params)

    async def get_mimo_shop_items(self, *, lang: str = "en") -> list[dict]:
        """
        Items disponibles en la tienda del mimo viajero.

        Campos relevantes: award_id, name, cost.
        """
        self._check_mimo_support()
        version_id = await self._get_mimo_version_id()
        data = await self._get(
            self._mimo_url("exchange-list"),
            params={"game_id": self.GAME_ID, "lang": lang, "version_id": version_id},
        )
        return data.get("exchange_award_list", [])

    async def buy_mimo_currency(self, *, lang: str = "en") -> list[dict]:
        """
        Compra automáticamente las monedas premium del juego en la tienda mimo.

        Filtra por CURRENCY_ITEM_KEYWORDS[game_biz] (case-insensitive, subcadena).
        Incluye delay aleatorio entre compras para evitar patrones robóticos.

        Devuelve lista de compras realizadas:
            item_name   str     Nombre del item comprado
            code        str     Código regalo resultante para canjear
        """
        self._check_mimo_support()
        keywords = CURRENCY_ITEM_KEYWORDS.get(self.game_biz, [])
        if not keywords:
            logger.warning(
                "[%s] No hay keywords de moneda definidas para %s.",
                self.GAME_NAME, self.game_biz,
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

            # Delay entre compras (no antes de la primera)
            if not first:
                await random_delay(1.5, 4.0)
            first = False

            try:
                data = await self._post(
                    self._mimo_url("exchange"),
                    data={
                        "award_id":   award_id,
                        "game_id":    self.GAME_ID,
                        "lang":       lang,
                        "version_id": version_id,
                    },
                )
                code = data.get("exchange_code", "")
                logger.info(
                    "[%s] Comprado '%s' en mimo → código: %s",
                    self.GAME_NAME, item.get("name"), code,
                )
                results.append({"item_name": item.get("name"), "code": code})
            except HoyoAPIError as e:
                logger.warning(
                    "[%s] Error al comprar '%s' en mimo: %s",
                    self.GAME_NAME, item.get("name"), e,
                )

        return results

    # ------------------------------------------------------------------
    # Canje de códigos regalo
    # ------------------------------------------------------------------

    async def redeem_code(self, code: str, *, lang: str = "en") -> None:
        """
        Canjea un código regalo para este juego.

        Disponible solo en OS: Genshin (GET), StarRail (POST), ZZZ (POST).
        Honkai Impact 3rd no tiene este endpoint en la API pública.

        Lanza:
            NotImplementedError  Si el juego o región no soportan redeem.
            HoyoAPIError         Si el código es inválido, ya fue usado, etc.
        """
        base_url = _CODE_URLS.get(self.game_biz)
        if not base_url:
            raise NotImplementedError(
                f"{self.GAME_NAME} no soporta canje de códigos vía esta API."
            )
        if self.is_chinese_region:
            raise NotImplementedError(
                "El canje de códigos vía API solo está disponible en región OS."
            )

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
                params={
                    "uid":      self.uid,
                    "region":   self.server,
                    "lang":     lang_short,
                    "cdkey":    code,
                    "game_biz": self.game_biz,
                },
                headers=headers,
            )

        logger.info("[%s] uid=%s Código '%s' canjeado.", self.GAME_NAME, self.uid, code)

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"uid={self.uid} "
            f"nickname={self.nickname!r} "
            f"level={self.level} "
            f"server={self.server!r}>"
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_game(session: "HoyoSession", account: dict) -> "BaseGame":
    """
    Instancia la subclase correcta de BaseGame según el game_biz de la cuenta.

    Imports locales para evitar circulares (todas las subclases importan game_model).
    Si el game_biz no está en el registro, devuelve un BaseGame genérico.
    """
    from genshin_game import GenshinGame    # noqa: PLC0415
    from starrail_game import StarRailGame  # noqa: PLC0415
    from zzz_game import ZZZGame            # noqa: PLC0415
    from honkai_game import HonkaiGame      # noqa: PLC0415

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
