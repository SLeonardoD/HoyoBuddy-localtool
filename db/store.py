"""
db/store.py
-----------
Capa de persistencia de HoYoWrapper. Reemplaza los antiguos
cookie_store.py + account_store.py (fusionados en una sola tabla) y
agrega el sistema de códigos de regalo.

Decisiones de diseño (acordadas con el usuario):
  - Una sola tabla `accounts` para cookies + alias. Antes eran dos
    tablas enlazadas por account_hash; ahora es una sola fila por
    (email, region).
  - El email se guarda en TEXTO PLANO, no hasheado. Esto es intencional
    — herramienta de uso local, sin necesidad de ese nivel de secreto —
    y permite reconstruir el flujo de "reingresar contraseña" (relogin)
    sin pedirle el email de nuevo al usuario, ya que ya lo tenemos.
  - Las cookies siguen en JSON plano, sin cifrar (misma logica: uso
    local, no vale la pena la complejidad de cifrado real por ahora).
  - "Eliminar cuenta" NO borra la fila fisicamente — solo pone
    alias=NULL. Esto preserva el comportamiento original de las dos
    tablas separadas: si el usuario vuelve a registrar la misma cuenta,
    las cookies siguen validas y evita un login+captcha innecesario.
  - Codigos de regalo en 2 tablas nuevas:
      gift_codes         codigo + juego + expiracion + si murio globalmente
      code_redemptions   que cuenta ya intento que codigo, y con que resultado
    Separadas porque la relacion es muchos-a-muchos: un codigo puede
    fallar para una cuenta puntual (nivel bajo, ya reclamado) sin que
    eso signifique que el codigo este muerto para las demas cuentas.

Exporta:
    AccountStore    Cuentas (cookies + alias unificados).
    GiftCodeStore   Codigos de regalo + tracking de canjes por cuenta.
"""

from __future__ import annotations

import json
import logging
import typing
from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, String, Text,
    UniqueConstraint, create_engine, select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

__all__ = ["AccountStore", "GiftCodeStore"]

logger = logging.getLogger(__name__)


# =============================================================================
# AccountStore — cuentas (cookies + alias fusionados)
# =============================================================================

class _AccountsBase(DeclarativeBase):
    pass


class _AccountRecord(_AccountsBase):
    """
    Fila de la tabla accounts.

    alias           Nombre elegido por el usuario. NULL = fila "huerfana":
                    existe (tiene cookies validas de un login) pero no
                    esta etiquetada como cuenta registrada. Pasa esto
                    cuando se elimina una cuenta (soft-delete) o si un
                    login tuvo exito pero el alias fallo al guardarse.
    email           Texto plano (ver decisiones de diseño arriba).
    region          'os' o 'cn'.
    cookies_json    JSON del dict de cookies de la sesion.
    device_id       UUID de dispositivo, persistido para reusar siempre
                    el mismo en cada request (anti-deteccion).
    """
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("email", "region", name="uq_email_region"),
        UniqueConstraint("alias", name="uq_alias"),
    )

    id           = Column(Integer, primary_key=True, autoincrement=True)
    alias        = Column(String(64), nullable=True)
    email        = Column(String(255), nullable=False)
    region       = Column(String(8), nullable=False)
    cookies_json = Column(Text, nullable=False, default="{}")
    device_id    = Column(String(36), nullable=False, default="")
    created_at   = Column(DateTime(timezone=True), nullable=False)
    updated_at   = Column(DateTime(timezone=True), nullable=False)


class AccountStore:
    """
    Almacen unificado de cuentas: cookies de sesion + alias en una sola
    tabla/fila por (email, region).

    Expone DOS interfaces sobre la misma tabla:

      1. Interfaz de sesion (usada por AuthManager, no conoce alias):
         get(email, region), set(email, region, cookies, device_id),
         delete(email).

      2. Interfaz de cuentas (usada por app.py, orientada a alias):
         set_alias(), get_by_alias(), list_all(), delete_by_alias(),
         exists_alias().

    Uso:
        store = AccountStore("./hoyo_auth.db")

        # AuthManager la usa asi (no necesita saber que existe un alias):
        cookies, device_id = store.get("user@mail.com", "os")
        store.set("user@mail.com", "os", {"ltoken_v2": "..."}, "uuid-x")

        # app.py la usa asi:
        store.set_alias("user@mail.com", "os", "Cuenta Principal")
        accounts = store.list_all()
    """

    def __init__(self, db_path: str = "./hoyo_auth.db") -> None:
        self._db_path = db_path
        engine = create_engine(f"sqlite:///{db_path}", echo=False)
        _AccountsBase.metadata.create_all(engine)
        self._factory = sessionmaker(bind=engine)

    # ------------------------------------------------------------------
    # Interfaz de sesion (compatible con AuthManager — NO tocar sin
    # revisar HoyoWrapper/auth_manager.py, que llama estos 3 metodos
    # exactos: get(email, region), set(email, region, cookies, device_id),
    # delete(email)).
    # ------------------------------------------------------------------

    def get(
        self, email: str, region: str,
    ) -> tuple[typing.Optional[dict[str, str]], typing.Optional[str]]:
        """Recupera (cookies, device_id) para (email, region), o (None, None)."""
        with self._factory() as db:
            record = db.execute(
                select(_AccountRecord).where(
                    _AccountRecord.email == email,
                    _AccountRecord.region == region,
                )
            ).scalar_one_or_none()

        if record is None:
            return None, None
        return json.loads(record.cookies_json), record.device_id or None

    def set(
        self, email: str, region: str, cookies: dict[str, str], device_id: str,
    ) -> None:
        """
        Guarda o actualiza cookies (upsert por email+region).

        Si la fila ya existe y tiene un alias asignado, el alias se
        preserva intacto — este metodo NUNCA toca la columna alias.
        Esto es lo que permite que AuthManager persista cookies durante
        un relogin sin necesidad de saber (ni de romper) el alias.
        """
        now = datetime.now(timezone.utc)

        with self._factory() as db:
            record = db.execute(
                select(_AccountRecord).where(
                    _AccountRecord.email == email,
                    _AccountRecord.region == region,
                )
            ).scalar_one_or_none()

            if record:
                record.cookies_json = json.dumps(cookies)
                record.device_id    = device_id
                record.updated_at   = now
            else:
                record = _AccountRecord(
                    alias=None,
                    email=email,
                    region=region,
                    cookies_json=json.dumps(cookies),
                    device_id=device_id,
                    created_at=now,
                    updated_at=now,
                )
                db.add(record)

            db.commit()
            logger.debug(
                "AccountStore: cookies guardadas para email=%s region=%s.",
                email, region,
            )

    def delete(self, email: str) -> None:
        """
        Elimina FISICAMENTE todas las filas de este email (cualquier
        region). Usado por AuthManager.logout() — a diferencia de
        delete_by_alias(), esto es un borrado real, no un soft-delete,
        porque logout() es una operacion explicita de "olvida esta
        cuenta por completo", no "solo quitale la etiqueta".
        """
        with self._factory() as db:
            records = db.execute(
                select(_AccountRecord).where(_AccountRecord.email == email)
            ).scalars().all()
            for r in records:
                db.delete(r)
            db.commit()
            if records:
                logger.debug("AccountStore: %d fila(s) eliminadas para email=%s.",
                             len(records), email)

    # ------------------------------------------------------------------
    # Interfaz de cuentas (usada por app.py)
    # ------------------------------------------------------------------

    def set_alias(self, email: str, region: str, alias: str) -> None:
        """
        Asigna un alias a la fila (email, region) — la fila DEBE existir
        ya (creada por un set() previo, tipicamente tras un login exitoso).

        Lanza:
            ValueError  Si no existe fila para (email, region), o si el
                        alias ya esta en uso por otra fila.
        """
        alias = alias.strip()
        if not alias:
            raise ValueError("El alias no puede estar vacio.")
        if len(alias) > 64:
            raise ValueError("El alias no puede superar 64 caracteres.")

        with self._factory() as db:
            record = db.execute(
                select(_AccountRecord).where(
                    _AccountRecord.email == email,
                    _AccountRecord.region == region,
                )
            ).scalar_one_or_none()

            if record is None:
                raise ValueError(
                    "No hay una sesion guardada para esta cuenta todavia "
                    "— esto no deberia pasar si se llama despues de un "
                    "login exitoso."
                )

            existing_alias = db.execute(
                select(_AccountRecord).where(
                    _AccountRecord.alias == alias,
                    _AccountRecord.id != record.id,
                )
            ).scalar_one_or_none()
            if existing_alias is not None:
                raise ValueError(f"Ya existe una cuenta con el alias '{alias}'.")

            record.alias = alias
            db.commit()
            logger.debug("AccountStore: alias '%s' asignado a email=%s region=%s.",
                         alias, email, region)

    def exists_alias(self, alias: str) -> bool:
        """True si ya existe una cuenta activa (alias no NULL) con ese alias."""
        with self._factory() as db:
            return db.execute(
                select(_AccountRecord).where(_AccountRecord.alias == alias)
            ).scalar_one_or_none() is not None

    def get_by_alias(self, alias: str) -> typing.Optional[dict]:
        """Devuelve la cuenta con ese alias, o None si no existe."""
        with self._factory() as db:
            record = db.execute(
                select(_AccountRecord).where(_AccountRecord.alias == alias)
            ).scalar_one_or_none()

        if record is None:
            return None
        return _record_to_dict(record)

    def list_all(self) -> list[dict]:
        """
        Devuelve todas las cuentas CON alias asignado (registradas),
        ordenadas por fecha de creacion. Las filas huerfanas
        (alias=NULL) no aparecen aca.

        Cada elemento: {alias, email, region, cookies, device_id,
                        created_at, updated_at}
        """
        with self._factory() as db:
            records = db.execute(
                select(_AccountRecord)
                .where(_AccountRecord.alias.is_not(None))
                .order_by(_AccountRecord.created_at)
            ).scalars().all()

        return [_record_to_dict(r) for r in records]

    def delete_by_alias(self, alias: str) -> bool:
        """
        "Elimina" una cuenta quitandole el alias (soft-delete): las
        cookies quedan intactas en la fila, solo deja de aparecer en
        list_all(). Si el usuario vuelve a registrar la misma cuenta
        con add_account(), el login reutiliza estas cookies sin pedir
        captcha de nuevo (mismo comportamiento que la version anterior
        con 2 tablas separadas).

        Devuelve True si se encontro y desetiqueto, False si no existia.
        """
        with self._factory() as db:
            record = db.execute(
                select(_AccountRecord).where(_AccountRecord.alias == alias)
            ).scalar_one_or_none()

            if record is None:
                return False

            record.alias = None
            db.commit()
        logger.debug("AccountStore: cuenta '%s' desetiquetada (cookies preservadas).", alias)
        return True


def _record_to_dict(r: _AccountRecord) -> dict:
    return {
        "alias":      r.alias,
        "email":      r.email,
        "region":     r.region,
        "cookies":    json.loads(r.cookies_json),
        "device_id":  r.device_id or None,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    }


# =============================================================================
# GiftCodeStore — códigos de regalo + tracking de canjes por cuenta
# =============================================================================

class _CodesBase(DeclarativeBase):
    pass


class _GiftCodeRecord(_CodesBase):
    """
    Fila de la tabla gift_codes.

    status          'active' | 'dead'. 'dead' = un retcode GLOBAL
                    (ver config.REDEEM_GLOBAL_DEAD_RETCODES) confirmo
                    que el codigo ya no sirve para NADIE — se deja de
                    intentar para todas las cuentas.
    dead_reason     Mensaje/retcode que lo mato, para diagnostico.
    expires_at      NULL si no se conoce fecha de expiracion (se sigue
                    intentando indefinidamente hasta que un retcode
                    global lo marque 'dead', o hasta que expire).
    """
    __tablename__ = "gift_codes"
    __table_args__ = (
        UniqueConstraint("code", "game_biz", name="uq_code_game"),
    )

    id          = Column(Integer, primary_key=True, autoincrement=True)
    code        = Column(String(64), nullable=False)
    game_biz    = Column(String(32), nullable=False)
    expires_at  = Column(DateTime(timezone=True), nullable=True)
    status      = Column(String(8), nullable=False, default="active")
    dead_reason = Column(Text, nullable=True)
    added_at    = Column(DateTime(timezone=True), nullable=False)


class _CodeRedemptionRecord(_CodesBase):
    """
    Fila de la tabla code_redemptions: que cuenta ya intento que codigo.

    account_alias   Referencia "blanda" al alias de AccountStore (sin
                    FK real: AccountStore y GiftCodeStore usan Base de
                    SQLAlchemy distintas aunque comparten archivo
                    SQLite — una FK cruzada complicaria el setup sin
                    aportar mucho en una DB local de un solo usuario).
    status          'success' | 'claimed' | 'ineligible'.
                    Solo se crea fila para resultados DEFINITIVOS por
                    cuenta — un resultado transitorio (cooldown, error
                    de red) no crea fila, para que se reintente solo.
    """
    __tablename__ = "code_redemptions"
    __table_args__ = (
        UniqueConstraint("code_id", "account_alias", name="uq_code_account"),
    )

    id            = Column(Integer, primary_key=True, autoincrement=True)
    code_id       = Column(Integer, ForeignKey("gift_codes.id"), nullable=False)
    account_alias = Column(String(64), nullable=False)
    status        = Column(String(16), nullable=False)
    message       = Column(Text, nullable=True)
    redeemed_at   = Column(DateTime(timezone=True), nullable=False)


class GiftCodeStore:
    """
    Almacen de codigos de regalo y su estado de canje por cuenta.

    Uso:
        codes = GiftCodeStore("./hoyo_auth.db")
        codes.add_code("hk4e_global", "GENSHINGIFT123")
        pending = codes.get_pending_for_account("Cuenta Principal", "hk4e_global")
        codes.record_redemption(pending[0]["id"], "Cuenta Principal", "success", "OK")
    """

    def __init__(self, db_path: str = "./hoyo_auth.db") -> None:
        self._db_path = db_path
        engine = create_engine(f"sqlite:///{db_path}", echo=False)
        _CodesBase.metadata.create_all(engine)
        self._factory = sessionmaker(bind=engine)

    # ------------------------------------------------------------------
    # Alta de codigos
    # ------------------------------------------------------------------

    def add_code(
        self,
        game_biz: str,
        code: str,
        expires_at: typing.Optional[datetime] = None,
    ) -> bool:
        """
        Agrega un codigo nuevo (normalizado a MAYUSCULAS + sin espacios).

        Devuelve True si se agrego, False si ya existia ese (code, game_biz)
        — no es un error, simplemente no hace nada (idempotente).
        """
        code = code.strip().upper()
        now = datetime.now(timezone.utc)

        with self._factory() as db:
            existing = db.execute(
                select(_GiftCodeRecord).where(
                    _GiftCodeRecord.code == code,
                    _GiftCodeRecord.game_biz == game_biz,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return False

            db.add(_GiftCodeRecord(
                code=code,
                game_biz=game_biz,
                expires_at=expires_at,
                status="active",
                dead_reason=None,
                added_at=now,
            ))
            db.commit()
        logger.debug("GiftCodeStore: codigo '%s' agregado para %s.", code, game_biz)
        return True

    # ------------------------------------------------------------------
    # Consulta de codigos activos
    # ------------------------------------------------------------------

    def get_pending_for_account(
        self, account_alias: str, game_biz: str,
    ) -> list[dict]:
        """
        Codigos activos y no expirados de un juego que esta cuenta
        TODAVIA no intento (no tiene fila en code_redemptions).

        Cada elemento: {id, code, game_biz, expires_at}
        """
        now = datetime.now(timezone.utc)

        with self._factory() as db:
            already_tried_ids = {
                row[0] for row in db.execute(
                    select(_CodeRedemptionRecord.code_id).where(
                        _CodeRedemptionRecord.account_alias == account_alias,
                    )
                ).all()
            }

            records = db.execute(
                select(_GiftCodeRecord).where(
                    _GiftCodeRecord.game_biz == game_biz,
                    _GiftCodeRecord.status == "active",
                )
            ).scalars().all()

        pending = []
        for r in records:
            if r.id in already_tried_ids:
                continue
            if r.expires_at is not None and r.expires_at <= now:
                continue
            pending.append({
                "id": r.id, "code": r.code, "game_biz": r.game_biz,
                "expires_at": r.expires_at,
            })
        return pending

    def get_or_create_code_id(
        self, game_biz: str, code: str,
    ) -> int:
        """
        Devuelve el id del codigo (creandolo como 'active' si no existia).
        Usado por el canje manual: al canjear un codigo suelto, tambien
        se registra en la DB para que otras cuentas lo aprovechen
        automaticamente en la proxima corrida de canje pendiente.
        """
        code = code.strip().upper()
        with self._factory() as db:
            record = db.execute(
                select(_GiftCodeRecord).where(
                    _GiftCodeRecord.code == code,
                    _GiftCodeRecord.game_biz == game_biz,
                )
            ).scalar_one_or_none()

            if record is not None:
                return record.id

            record = _GiftCodeRecord(
                code=code, game_biz=game_biz, expires_at=None,
                status="active", dead_reason=None,
                added_at=datetime.now(timezone.utc),
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            return record.id

    def list_codes(self, game_biz: typing.Optional[str] = None) -> list[dict]:
        """Lista todos los codigos (para diagnostico/debug), opcionalmente filtrados por juego."""
        with self._factory() as db:
            stmt = select(_GiftCodeRecord)
            if game_biz:
                stmt = stmt.where(_GiftCodeRecord.game_biz == game_biz)
            records = db.execute(stmt.order_by(_GiftCodeRecord.added_at)).scalars().all()

        return [
            {
                "id": r.id, "code": r.code, "game_biz": r.game_biz,
                "status": r.status, "dead_reason": r.dead_reason,
                "expires_at": r.expires_at, "added_at": r.added_at,
            }
            for r in records
        ]

    # ------------------------------------------------------------------
    # Registro de resultados de canje
    # ------------------------------------------------------------------

    def mark_dead(self, code_id: int, reason: str) -> None:
        """Marca un codigo como 'dead' — no se vuelve a intentar para NADIE."""
        with self._factory() as db:
            record = db.get(_GiftCodeRecord, code_id)
            if record is None:
                return
            record.status = "dead"
            record.dead_reason = reason
            db.commit()
        logger.debug("GiftCodeStore: codigo id=%s marcado como muerto: %s", code_id, reason)

    def record_redemption(
        self, code_id: int, account_alias: str, status: str, message: str,
    ) -> None:
        """
        Registra el resultado DEFINITIVO de un intento de canje para
        una cuenta puntual ('success' | 'claimed' | 'ineligible').

        Idempotente: si ya existia una fila para (code_id, account_alias)
        la actualiza en vez de fallar por la UniqueConstraint.
        """
        now = datetime.now(timezone.utc)
        with self._factory() as db:
            existing = db.execute(
                select(_CodeRedemptionRecord).where(
                    _CodeRedemptionRecord.code_id == code_id,
                    _CodeRedemptionRecord.account_alias == account_alias,
                )
            ).scalar_one_or_none()

            if existing:
                existing.status = status
                existing.message = message
                existing.redeemed_at = now
            else:
                db.add(_CodeRedemptionRecord(
                    code_id=code_id, account_alias=account_alias,
                    status=status, message=message, redeemed_at=now,
                ))

            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                logger.debug(
                    "GiftCodeStore: redemption (code_id=%s, alias=%s) "
                    "ya existia (race benigna).", code_id, account_alias,
                )

    def has_redeemed(self, code_id: int, account_alias: str) -> bool:
        """True si esta cuenta ya tiene un resultado definitivo para este codigo."""
        with self._factory() as db:
            return db.execute(
                select(_CodeRedemptionRecord).where(
                    _CodeRedemptionRecord.code_id == code_id,
                    _CodeRedemptionRecord.account_alias == account_alias,
                )
            ).scalar_one_or_none() is not None
