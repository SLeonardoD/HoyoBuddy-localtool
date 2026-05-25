"""
db/account_store.py
-------------------
Almacen de cuentas registradas por el usuario.

Responsabilidad: guardar un alias legible (e.g. "Cuenta Principal") junto
al account_hash y region que permiten recuperar las cookies de CookieStore.

El email NUNCA se almacena — solo su SHA-256 (account_hash), que es la
misma clave que usa CookieStore. Ambas tablas se enlazan por ese hash.

Tabla: accounts(id, alias, account_hash, region, created_at)

Metodos publicos:
    add(alias, account_hash, region)  Registrar cuenta
    list_all()                        Listar todas las cuentas
    get_by_alias(alias)               Buscar por alias
    delete_by_alias(alias)            Eliminar por alias
    exists_alias(alias)               Comprobar si el alias ya existe
    exists_hash(account_hash)         Comprobar si el hash ya esta registrado
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint, create_engine, select
from sqlalchemy.orm import DeclarativeBase, sessionmaker

__all__ = ["AccountStore"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Modelo SQLAlchemy
# ---------------------------------------------------------------------------

class _AccountBase(DeclarativeBase):
    pass


class _AccountRecord(_AccountBase):
    """
    Fila de la tabla accounts.

    alias           Nombre legible elegido por el usuario.
    account_hash    SHA-256 del email — enlaza con hoyo_cookies.account_hash.
    region          'os' o 'cn'.
    created_at      Timestamp UTC de registro.
    """
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("alias",        name="uq_alias"),
        UniqueConstraint("account_hash", name="uq_account_hash"),
    )

    id           = Column(Integer,  primary_key=True, autoincrement=True)
    alias        = Column(String(64),  nullable=False)
    account_hash = Column(String(64),  nullable=False)
    region       = Column(String(8),   nullable=False)
    created_at   = Column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# AccountStore
# ---------------------------------------------------------------------------

class AccountStore:
    """
    Almacen de cuentas registradas.

    Comparte el mismo archivo SQLite que CookieStore pero usa su
    propia tabla y su propio engine — sin acoplamiento entre ellos.

    Parametros:
        db_path     Ruta al archivo SQLite. Debe ser la misma que CookieStore.
                    Pasa ':memory:' para tests.

    Uso:
        store = AccountStore("./hoyo_auth.db")
        store.add("Cuenta Principal", account_hash, "os")
        accounts = store.list_all()
        store.delete_by_alias("Cuenta Principal")
    """

    def __init__(self, db_path: str = "./hoyo_auth.db") -> None:
        self._db_path = db_path
        engine = create_engine(f"sqlite:///{db_path}", echo=False)
        _AccountBase.metadata.create_all(engine)
        self._factory = sessionmaker(bind=engine)

    # ------------------------------------------------------------------
    # Escritura
    # ------------------------------------------------------------------

    def add(self, alias: str, account_hash: str, region: str) -> None:
        """
        Registra una cuenta nueva.

        Lanza:
            ValueError  Si el alias o el account_hash ya existen.
        """
        alias = alias.strip()
        if not alias:
            raise ValueError("El alias no puede estar vacio.")
        if len(alias) > 64:
            raise ValueError("El alias no puede superar 64 caracteres.")

        if self.exists_alias(alias):
            raise ValueError(f"Ya existe una cuenta con el alias '{alias}'.")
        if self.exists_hash(account_hash):
            raise ValueError(
                "Esta cuenta ya esta registrada con otro alias. "
                "Usa 'Eliminar cuenta' primero si quieres cambiar el alias."
            )

        now = datetime.now(timezone.utc)
        with self._factory() as db:
            db.add(_AccountRecord(
                alias=alias,
                account_hash=account_hash,
                region=region,
                created_at=now,
            ))
            db.commit()
        logger.debug("AccountStore: cuenta '%s' registrada (%s).", alias, region)

    def delete_by_alias(self, alias: str) -> bool:
        """
        Elimina la cuenta con el alias dado.

        Devuelve True si se elimino, False si no existia.
        """
        with self._factory() as db:
            record = db.execute(
                select(_AccountRecord).where(_AccountRecord.alias == alias)
            ).scalar_one_or_none()

            if record is None:
                return False

            db.delete(record)
            db.commit()
        logger.debug("AccountStore: cuenta '%s' eliminada.", alias)
        return True

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------

    def list_all(self) -> list[dict]:
        """
        Devuelve todas las cuentas registradas ordenadas por fecha de creacion.

        Cada elemento: {alias, account_hash, region, created_at}
        """
        with self._factory() as db:
            records = db.execute(
                select(_AccountRecord).order_by(_AccountRecord.created_at)
            ).scalars().all()

        return [
            {
                "alias":        r.alias,
                "account_hash": r.account_hash,
                "region":       r.region,
                "created_at":   r.created_at,
            }
            for r in records
        ]

    def get_by_alias(self, alias: str) -> dict | None:
        """
        Devuelve la cuenta con el alias dado, o None si no existe.
        """
        with self._factory() as db:
            record = db.execute(
                select(_AccountRecord).where(_AccountRecord.alias == alias)
            ).scalar_one_or_none()

        if record is None:
            return None
        return {
            "alias":        record.alias,
            "account_hash": record.account_hash,
            "region":       record.region,
            "created_at":   record.created_at,
        }

    def exists_alias(self, alias: str) -> bool:
        """True si ya existe una cuenta con ese alias."""
        with self._factory() as db:
            return db.execute(
                select(_AccountRecord).where(_AccountRecord.alias == alias)
            ).scalar_one_or_none() is not None

    def exists_hash(self, account_hash: str) -> bool:
        """True si ya existe una cuenta con ese account_hash."""
        with self._factory() as db:
            return db.execute(
                select(_AccountRecord).where(
                    _AccountRecord.account_hash == account_hash
                )
            ).scalar_one_or_none() is not None
