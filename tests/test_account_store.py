"""
tests/test_account_store.py
---------------------------
Tests unitarios de db/account_store.py.

Todos usan SQLite en memoria — sin archivos en disco.
"""

from __future__ import annotations

import pytest
from db.account_store import AccountStore

HASH_1 = "a" * 64
HASH_2 = "b" * 64
HASH_3 = "c" * 64


@pytest.fixture
def store() -> AccountStore:
    return AccountStore(":memory:")


# ---------------------------------------------------------------------------
# Tests de add / get
# ---------------------------------------------------------------------------

class TestAccountStoreAdd:

    def test_add_and_get_by_alias(self, store: AccountStore) -> None:
        """Despues de add(), get_by_alias() devuelve el registro correcto."""
        store.add("Mi Cuenta", HASH_1, "os")
        record = store.get_by_alias("Mi Cuenta")

        assert record is not None, "get_by_alias() devolvio None."
        assert record["alias"]        == "Mi Cuenta", f"alias: {record['alias']!r}"
        assert record["account_hash"] == HASH_1,      f"hash: {record['account_hash']!r}"
        assert record["region"]       == "os",         f"region: {record['region']!r}"

    def test_get_nonexistent_returns_none(self, store: AccountStore) -> None:
        """get_by_alias() devuelve None para un alias no registrado."""
        result = store.get_by_alias("No Existe")
        assert result is None, f"Se esperaba None, obtenido: {result}"

    def test_duplicate_alias_raises(self, store: AccountStore) -> None:
        """Registrar dos cuentas con el mismo alias lanza ValueError."""
        store.add("Alias Repetido", HASH_1, "os")
        with pytest.raises(ValueError, match="alias"):
            store.add("Alias Repetido", HASH_2, "os")

    def test_duplicate_hash_raises(self, store: AccountStore) -> None:
        """Registrar el mismo account_hash con alias diferente lanza ValueError."""
        store.add("Cuenta A", HASH_1, "os")
        with pytest.raises(ValueError):
            store.add("Cuenta B", HASH_1, "os")

    def test_empty_alias_raises(self, store: AccountStore) -> None:
        """Alias vacio lanza ValueError."""
        with pytest.raises(ValueError, match="vacio"):
            store.add("", HASH_1, "os")

    def test_alias_too_long_raises(self, store: AccountStore) -> None:
        """Alias de mas de 64 caracteres lanza ValueError."""
        with pytest.raises(ValueError, match="64"):
            store.add("X" * 65, HASH_1, "os")

    def test_whitespace_alias_is_stripped_and_rejected(self, store: AccountStore) -> None:
        """Alias de solo espacios se rechaza tras hacer strip."""
        with pytest.raises(ValueError, match="vacio"):
            store.add("   ", HASH_1, "os")


# ---------------------------------------------------------------------------
# Tests de list_all
# ---------------------------------------------------------------------------

class TestAccountStoreList:

    def test_list_empty(self, store: AccountStore) -> None:
        """list_all() devuelve lista vacia si no hay cuentas."""
        result = store.list_all()
        assert result == [], f"Se esperaba [], obtenido: {result}"

    def test_list_returns_all(self, store: AccountStore) -> None:
        """list_all() devuelve todas las cuentas registradas."""
        store.add("Cuenta 1", HASH_1, "os")
        store.add("Cuenta 2", HASH_2, "cn")
        result = store.list_all()

        assert len(result) == 2, f"Se esperaban 2 cuentas, obtenidas: {len(result)}"
        aliases = [r["alias"] for r in result]
        assert "Cuenta 1" in aliases, f"'Cuenta 1' no encontrada en: {aliases}"
        assert "Cuenta 2" in aliases, f"'Cuenta 2' no encontrada en: {aliases}"

    def test_list_ordered_by_creation(self, store: AccountStore) -> None:
        """list_all() devuelve cuentas ordenadas por fecha de creacion."""
        store.add("Primera", HASH_1, "os")
        store.add("Segunda", HASH_2, "os")
        store.add("Tercera", HASH_3, "cn")
        result = store.list_all()

        aliases = [r["alias"] for r in result]
        assert aliases[0] == "Primera", f"Primer elemento esperado 'Primera', obtenido: {aliases[0]!r}"


# ---------------------------------------------------------------------------
# Tests de delete
# ---------------------------------------------------------------------------

class TestAccountStoreDelete:

    def test_delete_existing(self, store: AccountStore) -> None:
        """delete_by_alias() elimina la cuenta y devuelve True."""
        store.add("A Eliminar", HASH_1, "os")
        deleted = store.delete_by_alias("A Eliminar")

        assert deleted is True, "delete_by_alias() deberia devolver True."
        assert store.get_by_alias("A Eliminar") is None, (
            "La cuenta deberia haber sido eliminada."
        )

    def test_delete_nonexistent_returns_false(self, store: AccountStore) -> None:
        """delete_by_alias() devuelve False para alias no existente."""
        deleted = store.delete_by_alias("No Existe")
        assert deleted is False, (
            f"delete_by_alias() deberia devolver False para alias inexistente."
        )

    def test_delete_does_not_affect_others(self, store: AccountStore) -> None:
        """Eliminar una cuenta no afecta a las demas."""
        store.add("Cuenta A", HASH_1, "os")
        store.add("Cuenta B", HASH_2, "cn")
        store.delete_by_alias("Cuenta A")

        remaining = store.list_all()
        assert len(remaining) == 1, f"Deberia quedar 1 cuenta, quedan: {len(remaining)}"
        assert remaining[0]["alias"] == "Cuenta B", (
            f"La cuenta restante deberia ser 'Cuenta B', es: {remaining[0]['alias']!r}"
        )


# ---------------------------------------------------------------------------
# Tests de exists
# ---------------------------------------------------------------------------

class TestAccountStoreExists:

    def test_exists_alias_true(self, store: AccountStore) -> None:
        store.add("Existe", HASH_1, "os")
        assert store.exists_alias("Existe") is True

    def test_exists_alias_false(self, store: AccountStore) -> None:
        assert store.exists_alias("No Existe") is False

    def test_exists_hash_true(self, store: AccountStore) -> None:
        store.add("Cuenta", HASH_1, "os")
        assert store.exists_hash(HASH_1) is True

    def test_exists_hash_false(self, store: AccountStore) -> None:
        assert store.exists_hash(HASH_2) is False

    def test_exists_hash_false_after_delete(self, store: AccountStore) -> None:
        """Tras eliminar, exists_hash devuelve False para ese hash."""
        store.add("Cuenta", HASH_1, "os")
        store.delete_by_alias("Cuenta")
        assert store.exists_hash(HASH_1) is False, (
            "exists_hash() deberia devolver False tras eliminar la cuenta."
        )
