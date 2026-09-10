"""
db/
---
Capa de persistencia de HoYoWrapper.

AccountStore    Cuentas registradas: cookies de sesion + alias, en una
                sola tabla (ver store.py para el detalle del diseño).
GiftCodeStore   Codigos de regalo pendientes de canjear + historial de
                que cuenta ya canjeo (o no pudo canjear) cada codigo.
"""

from db.store import AccountStore, GiftCodeStore

__all__ = ["AccountStore", "GiftCodeStore"]
