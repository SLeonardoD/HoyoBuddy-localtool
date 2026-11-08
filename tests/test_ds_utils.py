"""
tests/test_ds_utils.py
----------------------
Firmas 'ds' (dynamic secret). Se verifican RECALCULANDO el hash a partir
de las partes del propio header: si la formula cambia, el test lo detecta;
si solo cambian los valores aleatorios, no.
"""

from __future__ import annotations

import hashlib
import json

import config
from HoyoWrapper import ds_utils


class TestGenerateAppLoginDs:

    def _parts(self, ds: str) -> tuple[str, str, str]:
        t, r, h = ds.split(",")
        return t, r, h

    def test_has_three_parts(self) -> None:
        assert len(ds_utils.generate_app_login_ds({"a": "b"}, "SALT").split(",")) == 3

    def test_hash_covers_the_request_body(self) -> None:
        """
        REGLA CLAVE: a diferencia del ds simple, este hash incluye el cuerpo
        (json compacto). Se recalcula con las partes del header y debe coincidir.
        """
        body = {"account": "AAA", "password": "BBB"}
        t, r, h = self._parts(ds_utils.generate_app_login_ds(body, config.DS_SALT_APP_LOGIN))

        compact = json.dumps(body, separators=(",", ":"))
        expected = hashlib.md5(
            f"salt={config.DS_SALT_APP_LOGIN}&t={t}&r={r}&b={compact}&q=".encode()
        ).hexdigest()
        assert h == expected, "El hash no coincide con la formula de genshin.py (incluye body)."

    def test_different_body_gives_different_hash_for_same_t_and_r(self) -> None:
        """Si el body cambia, la firma tiene que cambiar (si no, no estaria firmando el body)."""
        body_a, body_b = {"account": "A"}, {"account": "B"}
        _, _, h_a = self._parts(ds_utils.generate_app_login_ds(body_a, "S"))
        t, r = "1700000000", "abc123"
        ha = hashlib.md5(f"salt=S&t={t}&r={r}&b={json.dumps(body_a, separators=(',', ':'))}&q=".encode()).hexdigest()
        hb = hashlib.md5(f"salt=S&t={t}&r={r}&b={json.dumps(body_b, separators=(',', ':'))}&q=".encode()).hexdigest()
        assert ha != hb
        assert len(h_a) == 32

    def test_random_part_is_six_alphanumeric_chars(self) -> None:
        _, r, _ = self._parts(ds_utils.generate_app_login_ds({}, "S"))
        assert len(r) == 6 and r.isalnum()

    def test_changes_between_calls(self) -> None:
        values = {ds_utils.generate_app_login_ds({"a": 1}, "S") for _ in range(20)}
        assert len(values) > 1, "El ds deberia variar entre llamadas."


class TestGenerateDynamicSecretUnchanged:

    def test_simple_ds_does_not_depend_on_a_body(self) -> None:
        t, r, h = ds_utils.generate_dynamic_secret("SALT").split(",")
        assert h == hashlib.md5(f"salt=SALT&t={t}&r={r}".encode()).hexdigest()
