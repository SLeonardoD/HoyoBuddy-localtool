"""
HoyoWrapper/ds_utils.py
-----------------------
Generacion del header 'ds' (dynamic secret) que HoYoverse exige en
ciertos endpoints como prueba de que el request viene de un cliente
"legitimo".

Verificado leyendo el codigo fuente instalado de genshin.py
(genshin/utility/ds.py -> generate_dynamic_secret()). Esta es la
variante SIMPLE: NO incluye body ni query params en el hash — solo
salt+t+r. Es la que usan tanto el intercambio stoken->cookie_token_v2
(auth_manager.py, salt="app_login") como el check-in diario CN
(game_model.py, salt="cn_signin"). Genshin.py tiene otras variantes
(generate_cn_dynamic_secret, generate_app_login_ds, generate_passport_ds)
para OTROS endpoints que si incluyen body/query en el hash — si algun
endpoint nuevo empieza a fallar con un retcode de firma invalida,
revisar primero si necesita una de esas variantes en vez de esta.

Los salts en si (que SI cambian con el tiempo) viven en config.py,
no aca — este modulo es logica pura, no datos.
"""

from __future__ import annotations

import hashlib
import json
import random
import string
import time

__all__ = ["generate_dynamic_secret", "generate_app_login_ds"]


def generate_dynamic_secret(salt: str) -> str:
    """
    Genera un dynamic secret con el formato "t,r,md5(salt=...&t=...&r=...)".

    Parametros:
        salt    Uno de los valores de config.DS_SALT_*.
    """
    t = int(time.time())
    r = "".join(random.choices(string.ascii_letters, k=6))
    h = hashlib.md5(f"salt={salt}&t={t}&r={r}".encode()).hexdigest()
    return f"{t},{r},{h}"


def generate_app_login_ds(body: dict, salt: str) -> str:
    """
    ds del login por app (appLoginByPassword). A diferencia de
    generate_dynamic_secret(), el hash INCLUYE el cuerpo del request:
        md5("salt=...&t=...&r=...&b=<json compacto del body>&q=")
    y el componente aleatorio usa letras Y digitos.

    Replica genshin/utility/ds.py::generate_app_login_ds (que a su vez
    copia RequestUtils.createSign() de la app oficial). El body se
    serializa con separadores compactos (",", ":") para calcular la
    firma, igual que hace genshin.py.

    Parametros:
        body    El mismo dict que se envia como JSON.
        salt    config.DS_SALT_APP_LOGIN.
    """
    t = int(time.time())
    r = "".join(random.choices(string.ascii_letters + string.digits, k=6))
    b = json.dumps(body, separators=(",", ":"))
    h = hashlib.md5(f"salt={salt}&t={t}&r={r}&b={b}&q=".encode()).hexdigest()
    return f"{t},{r},{h}"
