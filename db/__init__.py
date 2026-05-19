"""
db/
---
Paquete de persistencia local.

Exporta únicamente CookieStore: la interfaz para guardar y recuperar
cookies de sesión. No conoce nada de HoYoverse.

Uso desde main.py o desde cualquier capa superior:
    from db import CookieStore
    store = CookieStore("./hoyo_auth.db")
"""

from db.cookie_store import CookieStore

__all__ = ["CookieStore"]
