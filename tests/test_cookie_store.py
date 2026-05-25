"""
tests/test_cookie_store.py
--------------------------
Tests unitarios de db/cookie_store.py.

Todos usan SQLite en memoria — sin archivos en disco, sin efectos secundarios.
Verifican: persistencia, upsert, borrado, aislamiento por región y que el
email nunca se almacena en claro.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from tests.conftest import TEST_EMAIL, TEST_COOKIES_OS, TEST_COOKIES_CN, TEST_DEVICE_ID
from db.cookie_store import CookieStore, _hash_email


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OTHER_EMAIL     = "other_user@example.com"
OTHER_DEVICE_ID = "11111111-2222-3333-4444-555555555555"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCookieStoreGetSet:

    def test_set_and_get_returns_saved_data(self, cookie_store: CookieStore) -> None:
        """
        Después de set(), get() devuelve exactamente las cookies y device_id guardados.
        """
        cookie_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        cookies, device_id = cookie_store.get(TEST_EMAIL, "os")

        assert cookies == TEST_COOKIES_OS, (
            f"Cookies recuperadas no coinciden.\n"
            f"  Esperado: {TEST_COOKIES_OS}\n"
            f"  Obtenido: {cookies}"
        )
        assert device_id == TEST_DEVICE_ID, (
            f"device_id recuperado no coincide.\n"
            f"  Esperado: {TEST_DEVICE_ID!r}\n"
            f"  Obtenido: {device_id!r}"
        )

    def test_get_nonexistent_returns_none_tuple(self, cookie_store: CookieStore) -> None:
        """
        get() sobre un email no registrado devuelve (None, None), no lanza excepción.
        """
        cookies, device_id = cookie_store.get("nobody@example.com", "os")

        assert cookies is None, (
            f"Se esperaba cookies=None para email no registrado, obtenido: {cookies}"
        )
        assert device_id is None, (
            f"Se esperaba device_id=None para email no registrado, obtenido: {device_id!r}"
        )

    def test_upsert_updates_existing_record(self, cookie_store: CookieStore) -> None:
        """
        Llamar set() dos veces con el mismo email actualiza el registro (no duplica).
        """
        cookie_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)

        new_cookies   = {"ltuid_v2": "999", "ltoken_v2": "new_token"}
        new_device_id = "99999999-9999-9999-9999-999999999999"
        cookie_store.set(TEST_EMAIL, "os", new_cookies, new_device_id)

        cookies, device_id = cookie_store.get(TEST_EMAIL, "os")

        assert cookies == new_cookies, (
            f"Upsert fallido: las cookies no se actualizaron.\n"
            f"  Esperado: {new_cookies}\n"
            f"  Obtenido: {cookies}"
        )
        assert device_id == new_device_id, (
            f"Upsert fallido: device_id no se actualizó.\n"
            f"  Esperado: {new_device_id!r}\n"
            f"  Obtenido: {device_id!r}"
        )

    def test_upsert_does_not_create_duplicate_rows(self, cookie_store_file: CookieStore) -> None:
        """
        Multiples set() sobre el mismo email no generan filas duplicadas en la DB.
        Usa cookie_store_file (archivo real) para poder abrir una segunda
        conexion sqlite3 directa y contar las filas.
        """
        for i in range(3):
            cookie_store_file.set(TEST_EMAIL, "os", {"token": str(i)}, TEST_DEVICE_ID)

        db_path = cookie_store_file._db_path
        con = sqlite3.connect(db_path)
        count = con.execute("SELECT COUNT(*) FROM hoyo_cookies").fetchone()[0]
        con.close()

        assert count == 1, (
            f"Se esperaba 1 fila despues de 3 upserts, encontradas: {count}"
        )


class TestCookieStoreDelete:

    def test_delete_removes_record(self, cookie_store: CookieStore) -> None:
        """
        Después de delete(), get() devuelve (None, None).
        """
        cookie_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        cookie_store.delete(TEST_EMAIL)
        cookies, device_id = cookie_store.get(TEST_EMAIL, "os")

        assert cookies is None, (
            f"Después de delete(), se esperaba cookies=None, obtenido: {cookies}"
        )
        assert device_id is None, (
            f"Después de delete(), se esperaba device_id=None, obtenido: {device_id!r}"
        )

    def test_delete_nonexistent_does_not_raise(self, cookie_store: CookieStore) -> None:
        """
        delete() sobre un email que no existe no lanza ninguna excepción.
        """
        try:
            cookie_store.delete("nobody@example.com")
        except Exception as e:
            pytest.fail(
                f"delete() sobre email no existente lanzó excepción inesperada: {e}"
            )

    def test_delete_only_removes_target_user(self, cookie_store: CookieStore) -> None:
        """
        delete() solo elimina al usuario indicado; los demás usuarios no se ven afectados.
        """
        cookie_store.set(TEST_EMAIL,  "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        cookie_store.set(OTHER_EMAIL, "os", TEST_COOKIES_CN, OTHER_DEVICE_ID)

        cookie_store.delete(TEST_EMAIL)

        cookies, _ = cookie_store.get(OTHER_EMAIL, "os")
        assert cookies == TEST_COOKIES_CN, (
            f"delete() eliminó registros de otro usuario.\n"
            f"  Esperado: {TEST_COOKIES_CN}\n"
            f"  Obtenido: {cookies}"
        )


class TestCookieStoreSecurity:

    def test_email_not_stored_in_plain_text(self, cookie_store_file: CookieStore) -> None:
        """
        El email no aparece en claro en la base de datos.
        Solo debe estar su SHA-256.
        Usa cookie_store_file (archivo real) para poder abrir segunda conexion directa.
        """
        cookie_store_file.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)

        db_path = cookie_store_file._db_path
        con = sqlite3.connect(db_path)
        rows = con.execute("SELECT * FROM hoyo_cookies").fetchall()
        raw_dump = str(rows)
        con.close()

        assert TEST_EMAIL not in raw_dump, (
            f"El email '{TEST_EMAIL}' se encontro en claro en la DB.\n"
            f"  Contenido de la tabla: {raw_dump}"
        )

        expected_hash = _hash_email(TEST_EMAIL)
        assert expected_hash in raw_dump, (
            f"El hash SHA-256 del email no se encontro en la DB.\n"
            f"  Hash esperado: {expected_hash}"
        )

    def test_hash_is_deterministic(self) -> None:
        """
        _hash_email() siempre produce el mismo resultado para el mismo email.
        """
        h1 = _hash_email(TEST_EMAIL)
        h2 = _hash_email(TEST_EMAIL)
        assert h1 == h2, "El hash del email no es determinista."

    def test_hash_is_case_insensitive(self) -> None:
        """
        _hash_email() normaliza el email: mayúsculas y minúsculas producen el mismo hash.
        Garantiza que el mismo usuario no cree dos registros por diferencia de case.
        """
        h_lower = _hash_email("User@Example.COM")
        h_upper = _hash_email("user@example.com")
        assert h_lower == h_upper, (
            f"Hash diferente según capitalización del email.\n"
            f"  'User@Example.COM' → {h_lower}\n"
            f"  'user@example.com' → {h_upper}"
        )

    def test_cookies_stored_as_valid_json(self, cookie_store_file: CookieStore) -> None:
        """
        Las cookies se almacenan como JSON valido. Verifica que no hay
        corrupcion de datos al serializar/deserializar.
        Usa cookie_store_file (archivo real) para poder abrir segunda conexion directa.
        """
        cookie_store_file.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)

        db_path = cookie_store_file._db_path
        con = sqlite3.connect(db_path)
        row = con.execute("SELECT cookies_json FROM hoyo_cookies").fetchone()
        con.close()

        assert row is not None, "No se encontro ningun registro en la DB."
        try:
            parsed = json.loads(row[0])
        except json.JSONDecodeError as e:
            pytest.fail(f"cookies_json no es JSON valido: {e}\n  Valor: {row[0]!r}")

        assert parsed == TEST_COOKIES_OS, (
            f"JSON deserializado no coincide con las cookies originales.\n"
            f"  Esperado: {TEST_COOKIES_OS}\n"
            f"  Obtenido: {parsed}"
        )


class TestCookieStoreRegionIsolation:

    def test_os_and_cn_are_independent(self, cookie_store: CookieStore) -> None:
        """
        OS y CN se almacenan como registros separados e independientes.
        Guardar CN no sobreescribe OS.
        """
        cookie_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        cookie_store.set(TEST_EMAIL, "cn", TEST_COOKIES_CN, OTHER_DEVICE_ID)

        cookies_os, did_os = cookie_store.get(TEST_EMAIL, "os")
        cookies_cn, did_cn = cookie_store.get(TEST_EMAIL, "cn")

        assert cookies_os == TEST_COOKIES_OS, (
            f"Cookies OS sobrescritas por las de CN.\n"
            f"  Esperado: {TEST_COOKIES_OS}\n"
            f"  Obtenido: {cookies_os}"
        )
        assert cookies_cn == TEST_COOKIES_CN, (
            f"Cookies CN incorrectas.\n"
            f"  Esperado: {TEST_COOKIES_CN}\n"
            f"  Obtenido: {cookies_cn}"
        )
        assert did_os != did_cn, (
            f"OS y CN comparten el mismo device_id, deberían ser diferentes.\n"
            f"  OS: {did_os!r} / CN: {did_cn!r}"
        )

    def test_get_wrong_region_returns_none(self, cookie_store: CookieStore) -> None:
        """
        get() con región incorrecta devuelve (None, None) aunque el email exista.
        """
        cookie_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        cookies, _ = cookie_store.get(TEST_EMAIL, "cn")

        assert cookies is None, (
            f"get() con región 'cn' devolvió datos de 'os'.\n"
            f"  Obtenido: {cookies}"
        )
