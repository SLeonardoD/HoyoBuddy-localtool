"""
tests/conftest.py
-----------------
Fixtures compartidos entre todos los módulos de test.

Provee:
  - Instancias de CookieStore en memoria (:memory:) — sin archivos en disco.
  - HoyoSession de prueba con cookies y device_id fijos.
  - MockCookieStore para aislar AuthManager de SQLite.
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

from db.cookie_store import CookieStore
from HoyoWrapper.session import HoyoSession
from HoyoWrapper.genshin_game import GenshinGame
from HoyoWrapper.starrail_game import StarRailGame
from HoyoWrapper.zzz_game import ZZZGame
from HoyoWrapper.honkai_game import HonkaiGame


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
def cookie_store() -> CookieStore:
    """CookieStore respaldado por SQLite en memoria. Sin efectos en disco."""
    return CookieStore(":memory:")


@pytest.fixture
def cookie_store_file(tmp_path) -> CookieStore:
    """
    CookieStore respaldado por un archivo SQLite temporal real.

    Necesario para los tests que inspeccionan la DB abriendo una segunda
    conexion sqlite3 directa. SQLite ':memory:' crea una instancia nueva
    por cada conexion, por lo que una segunda conexion ve una DB vacia.
    tmp_path es un fixture de pytest que crea un directorio temporal
    unico por test y lo elimina automaticamente al terminar.
    """
    db_path = str(tmp_path / "test_hoyo.db")
    return CookieStore(db_path)


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
# MockCookieStore
# ---------------------------------------------------------------------------

class MockCookieStore:
    """
    CookieStore en memoria pura (sin SQLAlchemy) para aislar AuthManager.
    Registra todas las llamadas para poder verificarlas en los tests.
    """

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], tuple[dict, str]] = {}
        self.calls: list[tuple[str, tuple]] = []

    def get(self, email: str, region: str):
        self.calls.append(("get", (email, region)))
        return self._data.get((email, region), (None, None))

    def set(self, email: str, region: str, cookies: dict, device_id: str) -> None:
        self.calls.append(("set", (email, region)))
        self._data[(email, region)] = (cookies, device_id)

    def delete(self, email: str) -> None:
        self.calls.append(("delete", (email,)))
        keys_to_delete = [k for k in self._data if k[0] == email]
        for k in keys_to_delete:
            del self._data[k]

    def seed(self, email: str, region: str, cookies: dict, device_id: str) -> None:
        """Precarga datos sin registrar la llamada en self.calls."""
        self._data[(email, region)] = (cookies, device_id)


@pytest.fixture
def mock_store() -> MockCookieStore:
    return MockCookieStore()
