"""
game_model.py
-------------
Clase base para todos los módulos de juego.

Contiene la implementación real de check-in diario, mimo viajero y canje
de códigos. Las subclases solo necesitan declarar sus constantes de clase.

Arquitectura:
  - BaseGame implementa toda la lógica compartida.
  - Las subclases (GenshinGame, StarRailGame, etc.) son declarativas:
    definen GAME_BIZ, GAME_NAME, GAME_ID, SIGNGAME, REWARD_URL, etc.
  - Escalar a un juego nuevo = crear un archivo con ~30 líneas de constantes.

Identificadores usados por HoYoverse:
    game_biz        string usado en la API ('hk4e_global', 'hkrpg_global', ...)
    game_id         entero usado en el mimo viajero (2=Genshin, 6=HSR, 8=ZZZ)
    signgame        string usado en x-rpc-signgame para check-in

Notas de compatibilidad:
  - HoyoSession.request() usa 'body' (no 'json') como parámetro del cuerpo.
  - El mimo solo está disponible en región OS (HoYoLab). CN no lo soporta.
  - Honkai Impact 3rd no tiene mimo ni canje de códigos vía esta API.
"""

from __future__ import annotations

import logging
import typing
import uuid

if typing.TYPE_CHECKING:
    from auth_manager import HoyoSession

from auth_manager import HoyoAPIError

__all__ = [
    "BaseGame",
    "GAME_BIZ_MAP",
    "GAME_ID_MAP",
    "CURRENCY_ITEM_KEYWORDS",
    "create_game",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mapeos de identificadores de juego
# ---------------------------------------------------------------------------

# game_biz → nombre legible
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

# game_biz → mimo game_id (entero que usa el endpoint del mimo viajero)
# Honkai Impact 3rd no tiene mimo, por eso no aparece.
GAME_ID_MAP: dict[str, int] = {
    "hk4e_global":  2,
    "hk4e_cn":      2,
    "hkrpg_global": 6,
    "hkrpg_cn":     6,
    "nap_global":   8,
    "nap_cn":       8,
}

# Palabras clave para identificar la moneda premium de cada juego en la
# tienda del mimo. La búsqueda es case-insensitive (ver buy_mimo_currency).
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

# Check-in diario — base URL por juego y región.
# Extraídas de genshin.py/genshin/client/routes.py (REWARD_URL).
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

# act_id requerido como query param en check-in, varía por juego.
# Extraídos de genshin.py/genshin/client/routes.py.
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

# x-rpc-signgame por juego (extraído de genshin.py/daily.py)
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

# URLs base de canje de códigos, solo para juegos que lo soportan.
# Extraídas de genshin.py/genshin/client/routes.py (CODE_URL).
# ZZZ y StarRail usan POST; Genshin usa GET.
_CODE_URLS: dict[str, str] = {
    "hk4e_global":  "https://sg-hk4e-api.hoyoverse.com/common/apicdkey/api/webExchangeCdkey",
    "hkrpg_global": "https://sg-public-api.hoyoverse.com/common/apicdkey/api/webExchangeCdkeyRisk",
    "nap_global":   "https://public-operation-nap.hoyoverse.com/common/apicdkey/api/webExchangeCdkeyRisk",
}

# Juegos que usan POST para canjear código (el resto usa GET).
_CODE_POST_GAMES = {"hkrpg_global", "nap_global"}

# URL base del mimo viajero.
_MIMO_BASE_URL = "https://sg-public-api.hoyolab.com/event/miyoubi"

# DS salt para CN (extraído de genshin.py/genshin/constants.py)
_DS_SALT_CN_SIGNIN = "xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs"


# ---------------------------------------------------------------------------
# Clase base
# ---------------------------------------------------------------------------

class BaseGame:
    """
    Clase base para los módulos de juego.

    Implementa la lógica completa de check-in, mimo y redeem.
    Las subclases solo declaran constantes de clase.

    Parámetros:
        session     HoyoSession autenticada.
        account     Dict con los datos de la cuenta tal como los devuelve
                    HoyoSession.get_game_accounts():
                        game_biz, game_uid, nickname, level, region, region_name
    """

    # Subclases deben definir estos atributos de clase
    GAME_BIZ: str = ""          # e.g. 'hk4e_global'
    GAME_NAME: str = ""         # e.g. 'Genshin Impact'
    GAME_ID: int = 0            # ID para el mimo viajero (0 = sin mimo)

    def __init__(self, session: "HoyoSession", account: dict) -> None:
        self.session = session
        self.account = account
        # Caché del version_id del mimo para evitar llamadas extra
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
    # Métodos HTTP de conveniencia (delegan en la sesión)
    # ------------------------------------------------------------------

    async def _get(
        self,
        url: str,
        params: typing.Optional[dict] = None,
        headers: typing.Optional[dict] = None,
    ) -> typing.Any:
        """GET autenticado. Devuelve data{} de la respuesta."""
        return await self.session.request("GET", url, params=params, headers=headers)

    async def _post(
        self,
        url: str,
        data: typing.Optional[dict] = None,
        params: typing.Optional[dict] = None,
        headers: typing.Optional[dict] = None,
    ) -> typing.Any:
        """POST autenticado. Devuelve data{} de la respuesta."""
        # Nota: HoyoSession.request() usa 'body', no 'json'
        return await self.session.request(
            "POST", url, params=params, body=data, headers=headers
        )

    # ------------------------------------------------------------------
    # Check-in diario
    # ------------------------------------------------------------------

    def _checkin_headers(self) -> dict:
        """
        Construye los headers necesarios para el check-in según región.

        OS: solo x-rpc-signgame + referer.
        CN: headers adicionales para DS y device info (reducen captchas).
        """
        headers: dict[str, str] = {
            "x-rpc-signgame": self._signgame,
        }

        if not self.is_chinese_region:
            headers["Referer"] = "https://act.hoyolab.com/"
        else:
            # Headers opcionales pero recomendados para CN (reducen geetest)
            # Extraídos de genshin.py/daily.py (región CHINESE)
            headers.update({
                "x-rpc-app_version": "2.70.1",
                "x-rpc-client_type": "5",
                "x-rpc-device_id":   str(uuid.uuid4()),
                "x-rpc-sys_version": "12",
                "x-rpc-platform":    "android",
                "x-rpc-channel":     "miyousheluodi",
            })
            # DS dinámico para CN — requiere la librería ds_utility de genshin.py
            # o una implementación propia. Incluido como campo vacío si no está
            # disponible, ya que es opcional.
            try:
                from _ds_utility import generate_dynamic_secret
                headers["ds"] = generate_dynamic_secret(_DS_SALT_CN_SIGNIN)
            except ImportError:
                pass  # No crítico; el check-in puede funcionar sin él

        return headers

    def _checkin_params(self, lang: str = "en") -> dict:
        """Query params comunes para los endpoints de check-in."""
        params: dict[str, str] = {"act_id": self._act_id}
        if not self.is_chinese_region:
            params["lang"] = lang
        else:
            params["uid"] = self.uid
            params["region"] = self.server
        return params

    async def get_daily_reward_info(self, *, lang: str = "en") -> dict:
        """
        Obtiene el estado del check-in diario del usuario.

        Devuelve dict con al menos:
            is_sign         bool    Si ya se hizo check-in hoy.
            total_sign_day  int     Total de días que se ha firmado este mes.
        """
        url = f"{self._reward_base_url}/info"
        data = await self._get(
            url,
            params=self._checkin_params(lang),
            headers=self._checkin_headers(),
        )
        return {
            "is_sign": data.get("is_sign", False),
            "total_sign_day": data.get("total_sign_day", 0),
        }

    async def claim_daily_reward(self, *, lang: str = "en") -> dict:
        """
        Realiza el check-in diario si no se ha hecho aún hoy.

        Devuelve dict con:
            message     str     Mensaje de resultado.
            already_done bool   True si el check-in ya estaba hecho.
        """
        info = await self.get_daily_reward_info(lang=lang)

        if info["is_sign"]:
            logger.info(
                "[%s] uid=%s ya hizo check-in hoy (día %d del mes).",
                self.GAME_NAME, self.uid, info["total_sign_day"],
            )
            return {
                "message": "Check-in ya realizado hoy.",
                "already_done": True,
                "total_sign_day": info["total_sign_day"],
            }

        url = f"{self._reward_base_url}/sign"
        await self._post(
            url,
            params=self._checkin_params(lang),
            headers=self._checkin_headers(),
        )

        # Volver a consultar para saber qué recompensa cayó
        info_after = await self.get_daily_reward_info(lang=lang)
        logger.info(
            "[%s] uid=%s Check-in realizado (día %d del mes).",
            self.GAME_NAME, self.uid, info_after["total_sign_day"],
        )
        return {
            "message": "Check-in realizado.",
            "already_done": False,
            "total_sign_day": info_after["total_sign_day"],
        }

    # ------------------------------------------------------------------
    # Mimo viajero (solo OS, solo juegos con GAME_ID > 0)
    # ------------------------------------------------------------------

    def _check_mimo_support(self) -> None:
        """Lanza NotImplementedError si el juego o región no soporta mimo."""
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
        Construye la URL del endpoint mimo.

        Genshin usa /qiuqiu/<endpoint> en lugar de /<endpoint>.
        Además los guiones del endpoint se convierten a guiones bajos
        solo para Genshin (comportamiento de genshin.py/_request_mimo).
        """
        if self.GAME_ID == 2:  # Genshin
            endpoint_clean = endpoint.replace("-", "_")
            return f"{_MIMO_BASE_URL}/qiuqiu/{endpoint_clean}"
        return f"{_MIMO_BASE_URL}/{endpoint}"

    async def _get_mimo_version_id(self) -> int:
        """
        Obtiene el version_id activo del mimo para este juego.

        El version_id cambia con cada temporada del mimo y debe consultarse
        dinámicamente. Se cachea en la instancia para evitar llamadas extra.
        """
        if self._mimo_version_id is not None:
            return self._mimo_version_id

        url = self._mimo_url("index")
        data = await self._get(url, params={"game_id": self.GAME_ID, "lang": "en"})

        # Genshin devuelve act_list[].act_info; el resto devuelve list[]
        if self.GAME_ID == 2:
            items = [item["act_info"] for item in data.get("act_list", [])]
        else:
            items = data.get("list", [])

        # Encontrar la entrada de este juego por game_id
        entry = next(
            (i for i in items if i.get("game_id") == self.GAME_ID or i.get("game") == self.GAME_ID),
            items[0] if items else None,
        )
        if entry is None:
            raise HoyoAPIError(0, f"No se encontró version_id para game_id={self.GAME_ID}", {})

        version_id = entry.get("version_id") or entry.get("id")
        if not version_id:
            raise HoyoAPIError(0, "version_id no encontrado en la respuesta del mimo", {})

        self._mimo_version_id = int(version_id)
        return self._mimo_version_id

    async def get_mimo_tasks(self, *, lang: str = "en") -> list[dict]:
        """
        Obtiene la lista de tareas del mimo viajero.

        Devuelve lista de dicts con los campos de cada tarea:
            task_id, name, point, status (0=pendiente, 1=completada, 2=reclamada)
        """
        self._check_mimo_support()
        version_id = await self._get_mimo_version_id()
        url = self._mimo_url("task-list")
        data = await self._get(
            url,
            params={"game_id": self.GAME_ID, "lang": lang, "version_id": version_id},
        )
        return data.get("task_list", [])

    async def claim_mimo_task_reward(self, task_id: int, *, lang: str = "en") -> None:
        """
        Reclama la recompensa de una tarea del mimo viajero completada.

        Nota: Genshin usa POST; el resto de juegos usa GET.
        Esto es una particularidad de la API de HoYoverse documentada
        en genshin.py/hoyolab.py/_request_mimo.
        """
        self._check_mimo_support()
        version_id = await self._get_mimo_version_id()
        url = self._mimo_url("receive-point")
        params = {
            "task_id": task_id,
            "game_id": self.GAME_ID,
            "lang": lang,
            "version_id": version_id,
        }

        if self.GAME_ID == 2:  # Genshin usa POST
            await self._post(url, params=params)
        else:
            await self._get(url, params=params)

    async def get_mimo_shop_items(self, *, lang: str = "en") -> list[dict]:
        """
        Obtiene los items disponibles en la tienda del mimo viajero.

        Devuelve lista de dicts con los campos de cada item:
            award_id, name, cost, stock, ...
        """
        self._check_mimo_support()
        version_id = await self._get_mimo_version_id()
        url = self._mimo_url("exchange-list")
        data = await self._get(
            url,
            params={"game_id": self.GAME_ID, "lang": lang, "version_id": version_id},
        )
        return data.get("exchange_award_list", [])

    async def buy_mimo_currency(self, *, lang: str = "en") -> list[dict]:
        """
        Compra automáticamente las monedas premium del juego en la tienda mimo.

        Solo compra items cuyo nombre coincida con alguna de las palabras clave
        definidas en CURRENCY_ITEM_KEYWORDS para este game_biz. La búsqueda
        es case-insensitive.

        Devuelve lista de resultados de compra, uno por item comprado:
            item_name   str
            code        str     Código regalo a canjear
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
        results = []

        for item in items:
            name: str = item.get("name", "").lower()
            if not any(kw.lower() in name for kw in keywords):
                continue

            award_id = item.get("award_id")
            if not award_id:
                continue

            try:
                url = self._mimo_url("exchange")
                data = await self._post(
                    url,
                    data={
                        "award_id": award_id,
                        "game_id": self.GAME_ID,
                        "lang": lang,
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
                    "[%s] Error comprando '%s': %s",
                    self.GAME_NAME, item.get("name"), e,
                )

        return results

    # ------------------------------------------------------------------
    # Canje de códigos regalo
    # ------------------------------------------------------------------

    async def redeem_code(self, code: str, *, lang: str = "en") -> None:
        """
        Canjea un código regalo para este juego.

        Solo disponible en OS y para los juegos que lo soportan:
        Genshin (GET), StarRail (POST), ZZZ (POST).
        Honkai Impact 3rd no tiene este endpoint.

        Lanza:
            NotImplementedError  Si el juego no soporta canje de códigos.
            HoyoAPIError         Si el código es inválido, ya fue usado, etc.
        """
        base_url = _CODE_URLS.get(self.game_biz)
        if not base_url:
            raise NotImplementedError(
                f"{self.GAME_NAME} no soporta canje de códigos vía esta API."
            )
        if self.is_chinese_region:
            raise NotImplementedError(
                "El canje de códigos via API solo está disponible en región OS."
            )

        game_biz_prod = self.game_biz  # Ya es prod para OS
        lang_short = lang[:2] if len(lang) > 2 else lang

        origin_map = {
            "hk4e_global":  "https://genshin.hoyoverse.com",
            "hkrpg_global": "https://hsr.hoyoverse.com",
            "nap_global":   "https://zenless.hoyoverse.com",
        }
        origin = origin_map.get(self.game_biz, "https://hoyoverse.com")
        headers = {
            "Origin":  origin,
            "Referer": f"{origin}/",
        }

        if self.game_biz in _CODE_POST_GAMES:
            # StarRail y ZZZ usan POST
            import time
            await self._post(
                base_url,
                data={
                    "cdkey":       code,
                    "device_uuid": str(uuid.uuid4()),
                    "game_biz":    game_biz_prod,
                    "lang":        lang_short,
                    "platform":    "4",
                    "region":      self.server,
                    "t":           int(time.time()),
                    "uid":         int(self.uid),
                },
                headers=headers,
            )
        else:
            # Genshin usa GET
            await self._get(
                base_url,
                params={
                    "uid":      self.uid,
                    "region":   self.server,
                    "lang":     lang_short,
                    "cdkey":    code,
                    "game_biz": game_biz_prod,
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
# Factory: crea el objeto de juego correcto según game_biz
# ---------------------------------------------------------------------------

def create_game(session: "HoyoSession", account: dict) -> "BaseGame":
    """
    Instancia la subclase correcta de BaseGame según el game_biz de la cuenta.

    Importa las subclases aquí para evitar imports circulares.
    Si el game_biz no está soportado, devuelve un BaseGame genérico
    (que solo tendrá las propiedades básicas disponibles).
    """
    from genshin_game import GenshinGame
    from starrail_game import StarRailGame
    from zzz_game import ZZZGame
    from honkai_game import HonkaiGame

    biz = account.get("game_biz", "")

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

    cls = _REGISTRY.get(biz, BaseGame)
    return cls(session, account)
