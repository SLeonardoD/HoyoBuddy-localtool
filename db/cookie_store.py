"""
db/cookie_store.py
------------------
Capa de persistencia de cookies de sesión.

Responsabilidad única: guardar y recuperar cookies + device_id por usuario
y región. No conoce nada de HoYoverse ni del wrapper.

La interfaz pública es CookieStore, que expone tres métodos:
    get(email, region)                          → (cookies, device_id) | (None, None)
    set(email, region, cookies, device_id)      → None
    delete(email)                               → None

El email nunca se almacena en claro: solo su SHA-256 (account_hash).

Diseño para intercambiabilidad:
    AuthManager recibe un CookieStore en __init__ (inyección de dependencia).
    En el futuro se puede crear MemoryCookieStore, RedisCookieStore, etc.
    implementando la misma interfaz sin tocar HoyoWrapper.
"""

from __future__ import annotations

import hashlib
import json
import logging
import typing
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, sessionmaker

__all__ = ["CookieStore"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Modelo SQLAlchemy
# ---------------------------------------------------------------------------

class _Base(DeclarativeBase):
    pass


class _CookieRecord(_Base):
    """
    Fila de la tabla hoyo_cookies.

    Columnas:
        account_hash    SHA-256 del email normalizado (email nunca en claro).
        region          'os' o 'cn'.
        cookies_json    JSON del dict {nombre_cookie: valor}.
        device_id       UUID del dispositivo usado en el login. Persistido
                        para que todos los requests del usuario usen el mismo
                        device_id y simulen un dispositivo real.
        updated_at      Timestamp UTC de la última actualización.
    """
    __tablename__ = "hoyo_cookies"

    id           = Column(Integer,  primary_key=True, autoincrement=True)
    account_hash = Column(String(64), nullable=False, unique=True, index=True)
    region       = Column(String(8),  nullable=False)
    cookies_json = Column(Text,       nullable=False)
    device_id    = Column(String(36), nullable=False, default="")
    updated_at   = Column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# CookieStore
# ---------------------------------------------------------------------------

class CookieStore:
    """
    Almacén de cookies de sesión respaldado por SQLite + SQLAlchemy.

    Parámetros:
        db_path     Ruta al archivo SQLite. Por defecto './hoyo_auth.db'.
                    Pasa ':memory:' para un almacén en memoria (tests).

    Uso:
        store = CookieStore("./hoyo_auth.db")
        store.set("user@mail.com", "os", {"cookie_a": "v1"}, "uuid-xxx")
        cookies, device_id = store.get("user@mail.com", "os")
        store.delete("user@mail.com")
    """

    def __init__(self, db_path: str = "./hoyo_auth.db") -> None:
        self._db_path = db_path
        engine = create_engine(f"sqlite:///{db_path}", echo=False)
        _Base.metadata.create_all(engine)
        self._factory = sessionmaker(bind=engine)

    # ------------------------------------------------------------------
    # Interfaz pública
    # ------------------------------------------------------------------

    def get(
        self,
        email: str,
        region: str,
    ) -> tuple[typing.Optional[dict[str, str]], typing.Optional[str]]:
        """
        Recupera cookies y device_id guardados para un usuario y región.

        Devuelve (cookies_dict, device_id) si existen, (None, None) si no.
        """
        account_hash = _hash_email(email)

        with self._factory() as db:
            record = db.execute(
                select(_CookieRecord).where(
                    _CookieRecord.account_hash == account_hash,
                    _CookieRecord.region == region,
                )
            ).scalar_one_or_none()

        if record is None:
            return None, None

        cookies = json.loads(record.cookies_json)
        device_id = record.device_id or None
        return cookies, device_id

    def set(
        self,
        email: str,
        region: str,
        cookies: dict[str, str],
        device_id: str,
    ) -> None:
        """
        Guarda o actualiza cookies y device_id de un usuario (upsert).

        El email se hashea antes de almacenarse: nunca se guarda en claro.
        """
        account_hash = _hash_email(email)
        now = datetime.now(timezone.utc)

        with self._factory() as db:
            record = db.execute(
                select(_CookieRecord).where(
                    _CookieRecord.account_hash == account_hash,
                )
            ).scalar_one_or_none()

            if record:
                record.cookies_json = json.dumps(cookies)
                record.region       = region
                record.device_id    = device_id
                record.updated_at   = now
            else:
                record = _CookieRecord(
                    account_hash=account_hash,
                    region=region,
                    cookies_json=json.dumps(cookies),
                    device_id=device_id,
                    updated_at=now,
                )
                db.add(record)

            db.commit()
            logger.debug("CookieStore: cookies guardadas para hash=%s (%s).", account_hash[:8], region)

    def delete(self, email: str) -> None:
        """
        Elimina las cookies guardadas de un usuario.

        No lanza error si el usuario no existe.
        """
        account_hash = _hash_email(email)

        with self._factory() as db:
            record = db.execute(
                select(_CookieRecord).where(
                    _CookieRecord.account_hash == account_hash,
                )
            ).scalar_one_or_none()

            if record:
                db.delete(record)
                db.commit()
                logger.debug("CookieStore: cookies eliminadas para hash=%s.", account_hash[:8])


# ---------------------------------------------------------------------------
# Utilidad interna
# ---------------------------------------------------------------------------

def _hash_email(email: str) -> str:
    """
    SHA-256 del email normalizado (lowercase, strip).
    El email nunca se almacena en claro en ninguna tabla.
    """
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()
