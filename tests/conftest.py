"""
tests/conftest.py
-----------------
Fixtures compartidos entre todos los módulos de test.

===============================================================================
PRINCIPIOS DE TESTING DE ESTE PROYECTO (aplicables a cualquier lenguaje/
framework en herramientas futuras basadas en este wrapper)
===============================================================================

1. Un test = una regla de negocio, no un detalle de implementación.
   El nombre del test describe el COMPORTAMIENTO esperado ("no reintenta
   un codigo ya reclamado por esa cuenta"), no el método que se llama
   ("test_record_redemption"). Si cambia la implementación pero la regla
   de negocio sigue siendo la misma, el test no debería tener que cambiar.

2. Los mensajes de assert siempre explican QUÉ se esperaba, QUÉ se obtuvo
   y (cuando aplica) POR QUÉ importa. Un test que falla sin contexto
   obliga a releer el código fuente para entender qué se rompió; un buen
   mensaje de assert es autoexplicativo incluso fuera de contexto.

3. Se mockea en el LÍMITE de confianza, no el objeto bajo prueba:
     - Red (aioresponses)               -> siempre mockeada.
     - Motor de DB real (SQLite)        -> se usa de verdad (":memory:"),
       nunca se mockea — la lógica de queries/constraints ES lo que se
       prueba.
     - Reloj/delays (random_delay)      -> se mockea para que los tests
       de anti-detección (que SÍ verifican que hay un delay) no tarden
       segundos reales.
   Regla general: si mockear algo hace que el test deje de probar la
   regla que le da su nombre, no se debe mockear eso.

4. Clasificación de errores por ALCANCE, no por mensaje de texto. Cuando
   una operación falla, la primera pregunta de diseño es "¿este fallo es
   definitivo para todo el sistema, definitivo solo para esta entidad
   puntual, o temporal?" (ver GiftCodeStore: global / account / transient).
   Tratar todo fallo como "reintentar por siempre" desperdicia recursos;
   tratar todo fallo como "no reintentar nunca" pierde resultados válidos.
   Cada categoría tiene su propio test explícito.

5. Idempotencia se prueba explícitamente cuando una operación puede
   ejecutarse más de una vez con los mismos datos (agregar el mismo
   código dos veces, reintentar un registro tras un fallo de red). La
   pregunta del test es "¿el segundo intento corrompe o duplica el
   primero?", no solo "¿el primer intento funciona?".

6. Anti-detección (o cualquier propiedad de "parecer un humano"/"no
   generar un patrón detectable") se prueba con números, no con
   confianza ciega: se verifica que el delay ocurre, que ocurre EXACTAMENTE
   el número de veces esperado (N-1 para N operaciones secuenciales, nunca
   antes de la primera), y que valores repetidos (como un device_id)
   permanecen estables entre llamadas.

7. Los tests nunca duplican valores desde la implementación — importan
   la MISMA constante que usa el código bajo prueba (config.ACT_IDS,
   config.GAME_ID_MAP, etc.) en vez de copiar el valor literal. Si el
   valor cambia en config.py, el test sigue siendo válido sin tocarlo;
   si en cambio el test tuviera el valor hardcodeado por separado, un
   cambio legítimo en config.py haría que el test fallara sin que hubiera
   ningún bug real.

===============================================================================

Provee:
  - AccountStore / GiftCodeStore en memoria (:memory:) — sin archivos en disco.
  - HoyoSession de prueba con cookies y device_id fijos.
  - MockAccountStore para aislar AuthManager de SQLite real.
  - Cuentas de juego de prueba (una por juego soportado).
  - Objetos de juego instanciados (GenshinGame, etc.) listos para testear.
"""

from __future__ import annotations

import sys
import os

# Asegura que los paquetes del proyecto sean importables desde tests/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio

import config
from db.store import AccountStore, GiftCodeStore
from HoyoWrapper.session import HoyoSession
from HoyoWrapper.genshin_game import GenshinGame
from HoyoWrapper.starrail_game import StarRailGame
from HoyoWrapper.zzz_game import ZZZGame
from HoyoWrapper.honkai_game import HonkaiGame


# ---------------------------------------------------------------------------
# Compatibilidad aioresponses <-> aiohttp >= 3.14
# ---------------------------------------------------------------------------
# aiohttp 3.14 exige el argumento keyword-only `stream_writer` en
# ClientResponse.__init__, y aioresponses (0.7.9, la ultima publicada; su master
# tampoco lo trata) no lo pasa: con aiohttp >= 3.14 fallaban 24 tests con
#   TypeError: ClientResponse.__init__() missing 1 required keyword-only
#   argument: 'stream_writer'
# (aiohttp <= 3.13.5 funciona sin esto). Es un problema del mock de red de los
# tests, no de la aplicacion. En vez de fijar una version vieja de aiohttp
# (que en Python recientes puede no tener ruedas), se le da a la respuesta
# simulada un `stream_writer` de mentira: aioresponses ya le pasa writer=None
# ("la request ya se envio") y en ese caso aiohttp solo lee
# `stream_writer.output_size`, asi que basta un objeto con output_size = 0.
#
# Solo actua cuando hace falta: no hace nada con un aiohttp que no exige el
# argumento, ni con un aioresponses que ya lo pase. Si aioresponses publica
# una version compatible, este bloque entero se puede borrar.

def _patch_aioresponses_for_new_aiohttp() -> None:
    import inspect
    import types

    import aioresponses.core as ar_core

    stream_writer = inspect.signature(ar_core.ClientResponse.__init__).parameters.get("stream_writer")
    if stream_writer is None or stream_writer.default is not inspect.Parameter.empty:
        return

    class _ClientResponseCompat(ar_core.ClientResponse):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("stream_writer", types.SimpleNamespace(output_size=0))
            super().__init__(*args, **kwargs)

    ar_core.ClientResponse = _ClientResponseCompat


_patch_aioresponses_for_new_aiohttp()


# ---------------------------------------------------------------------------
# Constantes de prueba
# ---------------------------------------------------------------------------

TEST_EMAIL     = "test_user@example.com"
TEST_PASSWORD  = "test_password_123"
TEST_DEVICE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

TEST_COOKIES_OS = {
    "ltuid_v2":    "123456789",
    "ltoken_v2":   "test_ltoken_os",
    "cookie_token_v2": "test_cookie_token_os",
}

# Variante que incluye stoken + ltmid_v2 — necesaria para probar el
# intercambio stoken->cookie_token_v2 (_fetch_cookie_token en
# auth_manager.py). TEST_COOKIES_OS a secas ya trae cookie_token_v2 y
# no sirve para probar el CAMINO que lo obtiene.
TEST_COOKIES_OS_PRE_EXCHANGE = {
    "ltuid_v2":  "123456789",
    "ltoken_v2": "test_ltoken_os",
    "ltmid_v2":  "test_mid_os",
    "stoken":    "test_stoken_os",
    # Nota: sin cookie_token_v2 a proposito.
}

TEST_COOKIES_CN = {
    "ltuid_v2":    "987654321",
    "ltoken_v2":   "test_ltoken_cn",
    "cookie_token_v2": "test_cookie_token_cn",
}

# Cuentas de juego simuladas (formato de get_game_accounts())
TEST_ACCOUNTS = [
    {
        "game_biz":   "hk4e_global",
        "game_uid":   "700000001",
        "nickname":   "TravelerOS",
        "level":      60,
        "region":     "os_asia",
        "region_name": "Asia",
    },
    {
        "game_biz":   "hkrpg_global",
        "game_uid":   "700000002",
        "nickname":   "TrailblazerOS",
        "level":      70,
        "region":     "prod_official_asia",
        "region_name": "Asia",
    },
    {
        "game_biz":   "nap_global",
        "game_uid":   "700000003",
        "nickname":   "ProxyOS",
        "level":      50,
        "region":     "prod_gf_jp",
        "region_name": "Asia",
    },
    {
        "game_biz":   "bh3_global",
        "game_uid":   "700000004",
        "nickname":   "CaptainOS",
        "level":      80,
        "region":     "overseas01",
        "region_name": "Europe",
    },
]

TEST_ACCOUNT_CN = {
    "game_biz":   "hk4e_cn",
    "game_uid":   "100000001",
    "nickname":   "TravelerCN",
    "level":      60,
    "region":     "cn_gf01",
    "region_name": "天空岛",
}


# ---------------------------------------------------------------------------
# Fixtures de DB
# ---------------------------------------------------------------------------

@pytest.fixture
def account_store() -> AccountStore:
    """AccountStore respaldado por SQLite en memoria. Sin efectos en disco."""
    return AccountStore(":memory:")


@pytest.fixture
def account_store_file(tmp_path) -> AccountStore:
    """
    AccountStore respaldado por un archivo SQLite temporal real.

    Necesario para los tests que inspeccionan la DB abriendo una segunda
    conexion sqlite3 directa. SQLite ':memory:' crea una instancia nueva
    por cada conexion, por lo que una segunda conexion ve una DB vacia.
    tmp_path es un fixture de pytest que crea un directorio temporal
    unico por test y lo elimina automaticamente al terminar.
    """
    db_path = str(tmp_path / "test_hoyo.db")
    return AccountStore(db_path)


@pytest.fixture
def code_store() -> GiftCodeStore:
    """GiftCodeStore respaldado por SQLite en memoria."""
    return GiftCodeStore(":memory:")


@pytest.fixture
def code_store_file(tmp_path) -> GiftCodeStore:
    """GiftCodeStore respaldado por un archivo SQLite temporal real."""
    db_path = str(tmp_path / "test_hoyo_codes.db")
    return GiftCodeStore(db_path)


# ---------------------------------------------------------------------------
# Fixtures de sesión
# ---------------------------------------------------------------------------

@pytest.fixture
def session_os() -> HoyoSession:
    """HoyoSession OS con cookies y device_id fijos para tests."""
    return HoyoSession(TEST_COOKIES_OS, "os", TEST_DEVICE_ID)


@pytest.fixture
def session_cn() -> HoyoSession:
    """HoyoSession CN con cookies y device_id fijos para tests."""
    return HoyoSession(TEST_COOKIES_CN, "cn", TEST_DEVICE_ID)


# ---------------------------------------------------------------------------
# Fixtures de juegos
# ---------------------------------------------------------------------------

@pytest.fixture
def genshin(session_os) -> GenshinGame:
    return GenshinGame(session_os, TEST_ACCOUNTS[0])


@pytest.fixture
def starrail(session_os) -> StarRailGame:
    return StarRailGame(session_os, TEST_ACCOUNTS[1])


@pytest.fixture
def zzz(session_os) -> ZZZGame:
    return ZZZGame(session_os, TEST_ACCOUNTS[2])


@pytest.fixture
def honkai(session_os) -> HonkaiGame:
    return HonkaiGame(session_os, TEST_ACCOUNTS[3])


@pytest.fixture
def genshin_cn(session_cn) -> GenshinGame:
    return GenshinGame(session_cn, TEST_ACCOUNT_CN)


# ---------------------------------------------------------------------------
# MockAccountStore
# ---------------------------------------------------------------------------

class MockAccountStore:
    """
    Store en memoria pura (sin SQLAlchemy) para aislar AuthManager de
    SQLite real. Implementa solo la interfaz de SESION que AuthManager
    necesita (get/set/delete por email+region) — NO implementa la
    interfaz de alias (set_alias/list_all/etc.), a proposito: AuthManager
    nunca deberia depender de esa parte de la interfaz, y si algun dia
    lo hiciera, este mock lo haria fallar de inmediato en vez de pasar
    por casualidad.

    Registra todas las llamadas para poder verificarlas en los tests.
    """

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], tuple[dict, str]] = {}
        self.calls: list[tuple[str, tuple]] = []

    # El store real serializa a JSON: get() devuelve SIEMPRE un dict nuevo y
    # set() no conserva una referencia al dict recibido. El mock hace lo
    # mismo (copias), porque renew_cookie_token() muta el dict de cookies
    # in place — con un mock que compartiera referencias, mutar cookies
    # "ya guardadas" pasaria desapercibido y hasta contaminaria las
    # constantes de test (TEST_COOKIES_*) entre tests.
    def get(self, email: str, region: str):
        self.calls.append(("get", (email, region)))
        cookies, device_id = self._data.get((email, region), (None, None))
        return (dict(cookies) if cookies is not None else None), device_id

    def set(self, email: str, region: str, cookies: dict, device_id: str) -> None:
        self.calls.append(("set", (email, region)))
        self._data[(email, region)] = (dict(cookies), device_id)

    def delete(self, email: str) -> None:
        self.calls.append(("delete", (email,)))
        keys_to_delete = [k for k in self._data if k[0] == email]
        for k in keys_to_delete:
            del self._data[k]

    def seed(self, email: str, region: str, cookies: dict, device_id: str) -> None:
        """Precarga datos sin registrar la llamada en self.calls."""
        self._data[(email, region)] = (dict(cookies), device_id)


@pytest.fixture
def mock_store() -> MockAccountStore:
    return MockAccountStore()
