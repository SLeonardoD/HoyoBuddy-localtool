"""
db/
---
Paquete de persistencia local.

Exporta:
  CookieStore    Almacen de cookies de sesion por usuario y region.
  AccountStore   Almacen de cuentas registradas con alias legible.

Uso:
    from db import CookieStore, AccountStore

    cookie_store  = CookieStore("./hoyo_auth.db")
    account_store = AccountStore("./hoyo_auth.db")
"""

from db.cookie_store import CookieStore
from db.account_store import AccountStore

__all__ = ["CookieStore", "AccountStore"]
