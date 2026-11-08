"""
tests/test_store.py
--------------------
Tests de db/store.py: AccountStore (cuentas fusionadas) + GiftCodeStore
(codigos de regalo + tracking de canjes por cuenta).

Reemplaza los antiguos test_cookie_store.py + test_account_store.py,
que probaban dos tablas separadas enlazadas por account_hash. Ahora es
una sola tabla, el email va en texto plano (decision de diseño: uso
local, sin necesidad de ese nivel de secreto) y hay dos tablas nuevas
para el sistema de codigos.

Todos los tests usan SQLite en memoria salvo los que necesitan abrir una
segunda conexion directa (marcados _file), que usan un archivo temporal.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import TEST_EMAIL, TEST_COOKIES_OS, TEST_COOKIES_CN, TEST_DEVICE_ID
from db.store import AccountStore, GiftCodeStore

OTHER_EMAIL     = "other_user@example.com"
OTHER_DEVICE_ID = "11111111-2222-3333-4444-555555555555"


# =============================================================================
# AccountStore — interfaz de sesion (usada por AuthManager)
# =============================================================================

class TestAccountStoreSessionInterface:
    """
    Estos tests verifican el CONTRATO que AuthManager espera de su store:
    get(email, region), set(email, region, cookies, device_id), delete(email).
    AuthManager no sabe (ni deberia saber) que existe un alias — si
    alguno de estos tests falla, romper AuthManager es casi seguro.
    """

    def test_set_and_get_returns_saved_data(self, account_store: AccountStore) -> None:
        account_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        cookies, device_id = account_store.get(TEST_EMAIL, "os")

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

    def test_get_nonexistent_returns_none_tuple(self, account_store: AccountStore) -> None:
        """get() sobre un email no registrado devuelve (None, None), no lanza excepcion."""
        cookies, device_id = account_store.get("nobody@example.com", "os")
        assert cookies is None and device_id is None, (
            f"Se esperaba (None, None), obtenido: ({cookies}, {device_id})"
        )

    def test_upsert_updates_existing_record(self, account_store: AccountStore) -> None:
        """Llamar set() dos veces con el mismo email actualiza el registro (no duplica)."""
        account_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)

        new_cookies   = {"ltuid_v2": "999", "ltoken_v2": "new_token"}
        new_device_id = "99999999-9999-9999-9999-999999999999"
        account_store.set(TEST_EMAIL, "os", new_cookies, new_device_id)

        cookies, device_id = account_store.get(TEST_EMAIL, "os")
        assert cookies == new_cookies, f"Upsert fallido, cookies: {cookies}"
        assert device_id == new_device_id, f"Upsert fallido, device_id: {device_id!r}"

    def test_upsert_does_not_create_duplicate_rows(self, account_store_file: AccountStore) -> None:
        """Multiples set() sobre el mismo email no generan filas duplicadas en la DB."""
        for i in range(3):
            account_store_file.set(TEST_EMAIL, "os", {"token": str(i)}, TEST_DEVICE_ID)

        con = sqlite3.connect(account_store_file._db_path)
        count = con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        con.close()

        assert count == 1, f"Se esperaba 1 fila despues de 3 upserts, encontradas: {count}"

    def test_os_and_cn_are_independent(self, account_store: AccountStore) -> None:
        """Mismo email, distinta region -> filas independientes, sin pisarse."""
        account_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        account_store.set(TEST_EMAIL, "cn", TEST_COOKIES_CN, OTHER_DEVICE_ID)

        cookies_os, did_os = account_store.get(TEST_EMAIL, "os")
        cookies_cn, did_cn = account_store.get(TEST_EMAIL, "cn")

        assert cookies_os == TEST_COOKIES_OS, "Cookies OS sobrescritas por las de CN."
        assert cookies_cn == TEST_COOKIES_CN, "Cookies CN incorrectas."
        assert did_os != did_cn, "OS y CN no deberian compartir device_id."

    def test_get_wrong_region_returns_none(self, account_store: AccountStore) -> None:
        account_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        cookies, _ = account_store.get(TEST_EMAIL, "cn")
        assert cookies is None, f"get() con region incorrecta deberia dar None, dio: {cookies}"

    def test_delete_removes_record(self, account_store: AccountStore) -> None:
        account_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        account_store.delete(TEST_EMAIL)
        cookies, device_id = account_store.get(TEST_EMAIL, "os")
        assert cookies is None and device_id is None

    def test_delete_nonexistent_does_not_raise(self, account_store: AccountStore) -> None:
        try:
            account_store.delete("nobody@example.com")
        except Exception as e:
            pytest.fail(f"delete() sobre email inexistente lanzo excepcion: {e}")

    def test_delete_only_removes_target_user(self, account_store: AccountStore) -> None:
        account_store.set(TEST_EMAIL,  "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        account_store.set(OTHER_EMAIL, "os", TEST_COOKIES_CN, OTHER_DEVICE_ID)
        account_store.delete(TEST_EMAIL)

        cookies, _ = account_store.get(OTHER_EMAIL, "os")
        assert cookies == TEST_COOKIES_CN, "delete() afecto a un usuario que no era el objetivo."

    def test_delete_removes_all_regions_of_that_email(self, account_store: AccountStore) -> None:
        """
        delete(email) (usado por AuthManager.logout) es un borrado FISICO
        y total: quita todas las regiones de ese email, a diferencia de
        delete_by_alias() que es un soft-delete de una sola fila.
        """
        account_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        account_store.set(TEST_EMAIL, "cn", TEST_COOKIES_CN, OTHER_DEVICE_ID)
        account_store.delete(TEST_EMAIL)

        assert account_store.get(TEST_EMAIL, "os") == (None, None)
        assert account_store.get(TEST_EMAIL, "cn") == (None, None)

    def test_cookies_stored_as_valid_json(self, account_store_file: AccountStore) -> None:
        """Verifica que no hay corrupcion al serializar/deserializar cookies."""
        account_store_file.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)

        con = sqlite3.connect(account_store_file._db_path)
        row = con.execute("SELECT cookies_json FROM accounts").fetchone()
        con.close()

        parsed = json.loads(row[0])
        assert parsed == TEST_COOKIES_OS, f"JSON deserializado no coincide: {parsed}"

    def test_email_stored_in_plain_text(self, account_store_file: AccountStore) -> None:
        """
        DECISION DE DISEÑO (explicita, no un descuido): el email se guarda
        en TEXTO PLANO, no hasheado. Esta herramienta es de uso local; el
        beneficio de poder reconstruir el flujo de "reingresar contrasena"
        (relogin sin re-pedir el email) supera el valor de ese hash para
        este caso de uso. Este test documenta la decision para que no se
        interprete como un bug en una revision futura.
        """
        account_store_file.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)

        con = sqlite3.connect(account_store_file._db_path)
        row = con.execute("SELECT email FROM accounts").fetchone()
        con.close()

        assert row[0] == TEST_EMAIL, (
            f"El email deberia estar en texto plano (decision de diseño).\n"
            f"  Esperado: {TEST_EMAIL!r}\n"
            f"  Obtenido: {row[0]!r}"
        )


# =============================================================================
# AccountStore — interfaz de alias (usada por app.py)
# =============================================================================

class TestAccountStoreAliasInterface:

    def test_set_alias_requires_existing_session_row(self, account_store: AccountStore) -> None:
        """
        set_alias() NO puede crear una cuenta de la nada — la fila debe
        existir ya (creada por un set() previo, tipicamente tras login).
        Esto refleja el flujo real: login primero, etiqueta con alias despues.
        """
        with pytest.raises(ValueError):
            account_store.set_alias(TEST_EMAIL, "os", "Alias Huerfano")

    def test_set_alias_after_session_exists(self, account_store: AccountStore) -> None:
        account_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        account_store.set_alias(TEST_EMAIL, "os", "Mi Cuenta")

        assert account_store.exists_alias("Mi Cuenta") is True
        record = account_store.get_by_alias("Mi Cuenta")
        assert record["email"] == TEST_EMAIL
        assert record["cookies"] == TEST_COOKIES_OS, (
            "get_by_alias() deberia traer las cookies en el mismo dict "
            "(ya no hace falta una segunda consulta como con las 2 tablas viejas)."
        )

    def test_duplicate_alias_rejected(self, account_store: AccountStore) -> None:
        account_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        account_store.set(OTHER_EMAIL, "os", TEST_COOKIES_CN, OTHER_DEVICE_ID)
        account_store.set_alias(TEST_EMAIL, "os", "Alias Repetido")

        with pytest.raises(ValueError, match="alias"):
            account_store.set_alias(OTHER_EMAIL, "os", "Alias Repetido")

    def test_empty_alias_rejected(self, account_store: AccountStore) -> None:
        account_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        with pytest.raises(ValueError, match="vacio"):
            account_store.set_alias(TEST_EMAIL, "os", "   ")

    def test_alias_too_long_rejected(self, account_store: AccountStore) -> None:
        account_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        with pytest.raises(ValueError, match="64"):
            account_store.set_alias(TEST_EMAIL, "os", "X" * 65)

    def test_list_all_excludes_orphan_rows(self, account_store: AccountStore) -> None:
        """
        Una fila con sesion pero SIN alias (login exitoso pero set_alias
        nunca se llamo) no debe aparecer como "cuenta registrada".
        """
        account_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        # Adrede: no llamamos set_alias.
        assert account_store.list_all() == [], (
            "Una fila sin alias no deberia listarse como cuenta registrada."
        )

    def test_list_all_returns_registered_ordered_by_creation(self, account_store: AccountStore) -> None:
        account_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        account_store.set_alias(TEST_EMAIL, "os", "Primera")
        account_store.set(OTHER_EMAIL, "cn", TEST_COOKIES_CN, OTHER_DEVICE_ID)
        account_store.set_alias(OTHER_EMAIL, "cn", "Segunda")

        result = account_store.list_all()
        assert [r["alias"] for r in result] == ["Primera", "Segunda"], (
            f"Orden incorrecto: {[r['alias'] for r in result]}"
        )

    def test_delete_by_alias_is_soft_delete(self, account_store: AccountStore) -> None:
        """
        REGLA DE NEGOCIO CLAVE: "eliminar cuenta" NO borra las cookies.
        Solo quita la etiqueta (alias=NULL). Si se vuelve a registrar la
        misma cuenta, el login reutiliza la sesion existente sin pedir
        captcha de nuevo. Esto preserva el comportamiento de las 2 tablas
        originales (alias y cookies vivian por separado).
        """
        account_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        account_store.set_alias(TEST_EMAIL, "os", "A Eliminar")

        deleted = account_store.delete_by_alias("A Eliminar")
        assert deleted is True

        assert account_store.exists_alias("A Eliminar") is False, (
            "El alias deberia haber desaparecido de la vista publica."
        )
        cookies_after, _ = account_store.get(TEST_EMAIL, "os")
        assert cookies_after == TEST_COOKIES_OS, (
            "Las cookies deberian seguir intactas tras un soft-delete — "
            "esa es la diferencia con AuthManager.logout()/delete()."
        )

    def test_reregistering_after_soft_delete_reuses_session(self, account_store: AccountStore) -> None:
        """Volver a poner alias a una cuenta soft-eliminada reusa la MISMA sesion, sin nuevo login."""
        account_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        account_store.set_alias(TEST_EMAIL, "os", "Original")
        account_store.delete_by_alias("Original")

        account_store.set_alias(TEST_EMAIL, "os", "Nuevo Alias")
        record = account_store.get_by_alias("Nuevo Alias")
        assert record["cookies"] == TEST_COOKIES_OS, (
            "El re-registro deberia reusar las cookies existentes sin login nuevo."
        )

    def test_delete_by_alias_nonexistent_returns_false(self, account_store: AccountStore) -> None:
        assert account_store.delete_by_alias("No Existe") is False

    def test_delete_by_alias_does_not_affect_others(self, account_store: AccountStore) -> None:
        account_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        account_store.set_alias(TEST_EMAIL, "os", "Cuenta A")
        account_store.set(OTHER_EMAIL, "cn", TEST_COOKIES_CN, OTHER_DEVICE_ID)
        account_store.set_alias(OTHER_EMAIL, "cn", "Cuenta B")

        account_store.delete_by_alias("Cuenta A")

        remaining = account_store.list_all()
        assert len(remaining) == 1 and remaining[0]["alias"] == "Cuenta B"

    def test_get_by_alias_nonexistent_returns_none(self, account_store: AccountStore) -> None:
        assert account_store.get_by_alias("No Existe") is None

    def test_exists_alias_false_after_soft_delete(self, account_store: AccountStore) -> None:
        account_store.set(TEST_EMAIL, "os", TEST_COOKIES_OS, TEST_DEVICE_ID)
        account_store.set_alias(TEST_EMAIL, "os", "Temporal")
        account_store.delete_by_alias("Temporal")
        assert account_store.exists_alias("Temporal") is False


# =============================================================================
# GiftCodeStore
# =============================================================================

class TestGiftCodeStoreAddCode:

    def test_normalizes_case_and_whitespace(self, code_store: GiftCodeStore) -> None:
        """El codigo se guarda en MAYUSCULAS y sin espacios, sin importar como se ingreso."""
        code_store.add_code("hk4e_global", "  genshinGift123  ")
        codes = code_store.list_codes("hk4e_global")
        assert codes[0]["code"] == "GENSHINGIFT123", f"Normalizacion fallida: {codes[0]['code']!r}"

    def test_adding_same_code_twice_is_idempotent(self, code_store: GiftCodeStore) -> None:
        """
        Agregar el mismo (codigo, juego) dos veces no duplica la fila —
        sea porque el usuario lo agrego manualmente dos veces, o porque
        add_code() es idempotente por diseño para permitir llamadas
        repetidas sin verificacion previa por parte del caller.
        """
        first  = code_store.add_code("hk4e_global", "SAMECODE")
        second = code_store.add_code("hk4e_global", "samecode")  # mismo codigo, distinto case

        assert first is True and second is False, (
            f"Se esperaba (True, False), obtenido: ({first}, {second})"
        )
        assert len(code_store.list_codes("hk4e_global")) == 1, (
            "No deberia haber 2 filas para el mismo codigo normalizado."
        )

    def test_same_code_different_games_are_independent(self, code_store: GiftCodeStore) -> None:
        """El mismo string de codigo para 2 juegos distintos son entidades separadas."""
        code_store.add_code("hk4e_global", "SHARED")
        added_again = code_store.add_code("hkrpg_global", "SHARED")
        assert added_again is True, "El mismo codigo en otro juego deberia poder agregarse."


class TestGiftCodeStorePendingLogic:
    """
    Este es el nucleo del sistema de canje automatico: que codigos le
    quedan pendientes a una cuenta puntual. Las reglas probadas aca son
    las que definen si el sistema reintenta de mas, de menos, o justo lo
    necesario.
    """

    def test_new_code_is_pending_for_any_account(self, code_store: GiftCodeStore) -> None:
        code_store.add_code("hk4e_global", "NEWCODE")
        pending = code_store.get_pending_for_account("Cualquier Cuenta", "hk4e_global")
        assert len(pending) == 1 and pending[0]["code"] == "NEWCODE"

    def test_pending_filtered_by_game(self, code_store: GiftCodeStore) -> None:
        """Un codigo de Genshin no debe aparecer como pendiente para una cuenta consultando StarRail."""
        code_store.add_code("hk4e_global", "GENSHINCODE")
        pending = code_store.get_pending_for_account("Cuenta", "hkrpg_global")
        assert pending == [], "Un codigo de otro juego no deberia aparecer como pendiente."

    def test_successful_redemption_removes_from_pending_for_that_account(
        self, code_store: GiftCodeStore
    ) -> None:
        code_store.add_code("hk4e_global", "GOODCODE")
        pending = code_store.get_pending_for_account("Alice", "hk4e_global")
        code_id = pending[0]["id"]

        code_store.record_redemption(code_id, "Alice", "success", "Canjeado.")

        assert code_store.get_pending_for_account("Alice", "hk4e_global") == [], (
            "Tras un canje exitoso, ese codigo no deberia seguir pendiente para esa cuenta."
        )

    def test_claimed_by_one_account_still_pending_for_others(self, code_store: GiftCodeStore) -> None:
        """
        REGLA CLAVE: un resultado 'account-scoped' (ya reclamado, no
        elegible) es definitivo SOLO para esa cuenta puntual. El codigo
        sigue activo y disponible para las demas.
        """
        code_store.add_code("hk4e_global", "SHAREDCODE")
        code_id = code_store.get_pending_for_account("Alice", "hk4e_global")[0]["id"]
        code_store.record_redemption(code_id, "Alice", "claimed", "[-2017] Ya reclamado.")

        pending_bob = code_store.get_pending_for_account("Bob", "hk4e_global")
        assert len(pending_bob) == 1, (
            "El codigo deberia seguir pendiente para Bob aunque Alice ya lo haya reclamado."
        )

    def test_dead_code_disappears_for_every_account(self, code_store: GiftCodeStore) -> None:
        """
        REGLA CLAVE: un resultado 'global-scoped' (codigo expirado/invalido/
        agotado) mata el codigo para TODAS las cuentas, incluso las que
        nunca lo intentaron.
        """
        code_store.add_code("hkrpg_global", "DEADCODE")
        code_id = code_store.get_pending_for_account("Alice", "hkrpg_global")[0]["id"]
        code_store.mark_dead(code_id, "[-2001] Redemption code has expired.")

        assert code_store.get_pending_for_account("Alice", "hkrpg_global") == []
        assert code_store.get_pending_for_account("Bob", "hkrpg_global") == [], (
            "Un codigo muerto globalmente no deberia aparecer pendiente "
            "ni siquiera para cuentas que nunca lo intentaron."
        )

    def test_expired_by_date_excluded_from_pending(self, code_store: GiftCodeStore) -> None:
        """Un codigo con expires_at en el pasado no debe ofrecerse, sin necesidad de marcarlo 'dead'."""
        past = datetime.now(timezone.utc) - timedelta(days=1)
        code_store.add_code("hk4e_global", "EXPIREDBYDATE", expires_at=past)
        assert code_store.get_pending_for_account("Alice", "hk4e_global") == []

    def test_future_expiry_still_pending(self, code_store: GiftCodeStore) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=30)
        code_store.add_code("hk4e_global", "FUTURECODE", expires_at=future)
        assert len(code_store.get_pending_for_account("Alice", "hk4e_global")) == 1

    def test_null_expiry_never_excluded_by_date(self, code_store: GiftCodeStore) -> None:
        """Sin fecha de expiracion conocida, el codigo se sigue ofreciendo indefinidamente."""
        code_store.add_code("hk4e_global", "NOEXPIRY", expires_at=None)
        assert len(code_store.get_pending_for_account("Alice", "hk4e_global")) == 1

    def test_transient_failure_leaves_code_pending_for_retry(self, code_store: GiftCodeStore) -> None:
        """
        REGLA CLAVE: un fallo transitorio (cooldown, red, retcode
        desconocido) NO debe registrarse en absoluto — ni como exito, ni
        como claimed/ineligible, ni como dead. La ausencia de registro ES
        el mecanismo de reintento: la proxima consulta a
        get_pending_for_account() lo va a devolver de nuevo, sin logica
        adicional en el llamador.
        """
        code_store.add_code("hk4e_global", "COOLDOWNCODE")
        pending_before = code_store.get_pending_for_account("Alice", "hk4e_global")
        assert len(pending_before) == 1

        # Simplemente NO se llama a record_redemption ni a mark_dead —
        # eso es lo que hace app.py cuando clasifica el error como transient.

        pending_after = code_store.get_pending_for_account("Alice", "hk4e_global")
        assert len(pending_after) == 1, (
            "Un codigo sin resultado registrado debe seguir pendiente — "
            "asi es como se implementa 'reintentar despues' sin estado extra."
        )

    def test_record_redemption_is_idempotent_per_account_and_code(
        self, code_store: GiftCodeStore
    ) -> None:
        """Registrar el mismo (codigo, cuenta) dos veces actualiza, no duplica ni lanza error."""
        code_store.add_code("hk4e_global", "IDEMPOTENTCODE")
        code_id = code_store.get_pending_for_account("Alice", "hk4e_global")[0]["id"]

        code_store.record_redemption(code_id, "Alice", "claimed", "primer intento")
        code_store.record_redemption(code_id, "Alice", "claimed", "segundo intento")  # no debe lanzar

        assert code_store.has_redeemed(code_id, "Alice") is True

    def test_has_redeemed_false_when_never_attempted(self, code_store: GiftCodeStore) -> None:
        code_store.add_code("hk4e_global", "UNTRIEDCODE")
        code_id = code_store.list_codes("hk4e_global")[0]["id"]
        assert code_store.has_redeemed(code_id, "Alice") is False

    def test_get_or_create_code_id_reuses_existing(self, code_store: GiftCodeStore) -> None:
        """
        get_or_create_code_id() (usado por el canje manual) no duplica un
        codigo que ya fue agregado — el canje manual y el sistema
        automatico comparten la misma fila.
        """
        id1 = code_store.get_or_create_code_id("hk4e_global", "MANUALCODE")
        id2 = code_store.get_or_create_code_id("hk4e_global", "manualcode")
        assert id1 == id2, "get_or_create_code_id deberia devolver el mismo id para el mismo codigo normalizado."


# =============================================================================
# Medicion de sesiones (session_events)
#
# Responde: cuanto dura un cookie_token_v2? Ni genshin.py ni HoYoverse lo
# documentan. Si algun dia un canje falla por sesion, el evento
# 'token_use_fail' guarda la edad exacta del token: ese numero es el
# intervalo maximo que NO se debe exceder entre dos corridas.
# =============================================================================

class TestSessionEvents:

    def test_log_and_read_back_all_fields(self, account_store: AccountStore) -> None:
        account_store.log_session_event(
            TEST_EMAIL, "os", "token_issued", detail="renovacion", age_hours=23.5,
        )
        last = account_store.last_session_event(TEST_EMAIL, "os", "token_issued")

        assert last["detail"] == "renovacion"
        assert last["age_hours"] == 23.5
        assert last["email"] == TEST_EMAIL and last["region"] == "os"

    def test_last_returns_the_most_recent_of_that_type(self, account_store: AccountStore) -> None:
        old = datetime.now(timezone.utc) - timedelta(hours=10)
        account_store.log_session_event(TEST_EMAIL, "os", "token_issued", detail="viejo", created_at=old)
        account_store.log_session_event(TEST_EMAIL, "os", "token_issued", detail="nuevo")

        assert account_store.last_session_event(TEST_EMAIL, "os", "token_issued")["detail"] == "nuevo"

    def test_events_are_independent_per_email_region_and_type(self, account_store: AccountStore) -> None:
        account_store.log_session_event(TEST_EMAIL, "os", "token_issued")

        assert account_store.last_session_event(OTHER_EMAIL, "os", "token_issued") is None
        assert account_store.last_session_event(TEST_EMAIL, "cn", "token_issued") is None
        assert account_store.last_session_event(TEST_EMAIL, "os", "token_use_fail") is None

    def test_last_returns_none_without_events(self, account_store: AccountStore) -> None:
        assert account_store.last_session_event(TEST_EMAIL, "os", "token_issued") is None

    def test_hours_since_last_event_measures_elapsed_time(self, account_store: AccountStore) -> None:
        five_hours_ago = datetime.now(timezone.utc) - timedelta(hours=5)
        account_store.log_session_event(TEST_EMAIL, "os", "token_issued", created_at=five_hours_ago)

        hours = account_store.hours_since_last_event(TEST_EMAIL, "os", "token_issued")
        assert hours == pytest.approx(5.0, abs=0.01), f"Se esperaban ~5h, se obtuvo {hours}"

    def test_hours_since_last_event_is_none_without_events(self, account_store: AccountStore) -> None:
        assert account_store.hours_since_last_event(TEST_EMAIL, "os", "token_issued") is None

    def test_hours_since_works_with_timezone_aware_and_naive_values(self, account_store_file: AccountStore) -> None:
        """
        REGRESION (mismo origen que el bug de get_pending_for_account):
        SQLite devuelve datetimes sin zona horaria; restarlos de "ahora con
        zona" lanza TypeError. Se prueba contra un archivo real, que es
        donde ocurre el round-trip.
        """
        aware = datetime.now(timezone.utc) - timedelta(hours=2)
        account_store_file.log_session_event(TEST_EMAIL, "os", "token_issued", created_at=aware)

        hours = account_store_file.hours_since_last_event(TEST_EMAIL, "os", "token_issued")
        assert hours == pytest.approx(2.0, abs=0.01)

    def test_list_is_chronological_and_filterable(self, account_store: AccountStore) -> None:
        base = datetime.now(timezone.utc)
        account_store.log_session_event(TEST_EMAIL, "os", "token_use_ok", detail="3", created_at=base)
        account_store.log_session_event(TEST_EMAIL, "os", "token_issued", detail="1", created_at=base - timedelta(hours=2))
        account_store.log_session_event(OTHER_EMAIL, "os", "token_issued", detail="x", created_at=base - timedelta(hours=1))

        mine = account_store.list_session_events(email=TEST_EMAIL)
        assert [e["detail"] for e in mine] == ["1", "3"], "Debe estar en orden cronologico."
        assert len(account_store.list_session_events(event="token_issued")) == 2
        assert len(account_store.list_session_events()) == 3

    def test_existing_database_gets_the_new_table_without_manual_migration(self, tmp_path) -> None:
        """
        Una base creada ANTES de este historial (solo tabla accounts) debe
        seguir funcionando: la tabla nueva se crea sola al abrirla y las
        cuentas existentes siguen legibles.
        """
        db_path = str(tmp_path / "legacy.db")
        con = sqlite3.connect(db_path)
        con.execute(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, alias VARCHAR(64), "
            "email VARCHAR(255) NOT NULL, region VARCHAR(8) NOT NULL, cookies_json TEXT NOT NULL, "
            "device_id VARCHAR(36) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
            "UNIQUE (email, region), UNIQUE (alias))"
        )
        con.execute(
            "INSERT INTO accounts (alias, email, region, cookies_json, device_id, created_at, updated_at) "
            "VALUES ('Vieja', 'old@x.com', 'os', '{\"ltuid_v2\": \"1\"}', 'dev', '2026-01-01', '2026-01-01')"
        )
        con.commit()
        con.close()

        store = AccountStore(db_path)

        assert [a["alias"] for a in store.list_all()] == ["Vieja"], "Las cuentas existentes deben seguir ahi."
        store.log_session_event("old@x.com", "os", "token_issued")
        assert store.last_session_event("old@x.com", "os", "token_issued") is not None
