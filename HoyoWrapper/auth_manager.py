"""
auth_manager.py
---------------
Gestión de autenticación con HoYoLab / Miyoushe.

Responsabilidades:
  - Login con email + contraseña (regiones OS y CN).
  - Manejo transparente del captcha Geetest (delega a captcha_server.py).
  - Manejo de verificación por email si HoYoverse lo exige.
  - Persistencia de cookies en SQLite via SQLAlchemy (evita captchas futuros).
  - Exposición de un objeto HoyoSession listo para operar.

Copiado y adaptado de:
  genshin.py/genshin/client/components/auth/web.py
  genshin.py/genshin/client/components/auth/client.py
  genshin.py/genshin/client/manager/managers.py

Correcciones respecto a la versión anterior:
  - Bug 1: _make_session() limpiada (variable jar sin uso eliminada).
  - Bug 2: parámetro 'json' en HoyoSession.request() renombrado a 'body'
            para no shadowear el módulo json importado.
  - Bug 3: manejo del retcode -3239 (verificación de email) implementado
            en _os_login_flow() con solicitud del código por consola.
  - Anti-detección 1: _USER_AGENT actualizado a Chrome 124 (2024).
  - Anti-detección 2: device_id generado una vez por sesión de login y
            almacenado en HoyoSession para reutilizarse en requests
            posteriores (check-in CN, etc.), simulando un dispositivo real.
"""

from __future__ import annotations

import hashlib
import json
import logging
import typing
import uuid
from datetime import datetime, timezone

import aiohttp
from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from captcha_server import SessionMMT, SessionMMTResult, enter_code, solve_geetest

__all__ = ["AuthManager", "HoyoSession", "HoyoAPIError", "LoginError"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# UA moderno (Chrome 124, Windows 10, mayo 2024).
# Un UA desactualizado es señal inmediata de bot para los sistemas de HoYoverse.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# URLs de autenticación (copiadas de genshin.py/genshin/client/routes.py)
_WEB_LOGIN_URL = (
    "https://sg-public-api.hoyolab.com/account/ma-passport/api/webLoginByPassword"
)
_CN_WEB_LOGIN_URL = (
    "https://passport-api.miyoushe.com/account/ma-cn-passport/web/loginByPassword"
)
_SEND_VERIFICATION_URL = (
    "https://sg-public-api.hoyoverse.com/account/ma-verifier/api"
    "/createEmailCaptchaByActionTicket"
)
_VERIFY_EMAIL_URL = (
    "https://sg-public-api.hoyoverse.com/account/ma-verifier/api"
    "/verifyActionTicketPartly"
)

# URLs para obtener cuentas de juego vinculadas
_GAME_ACCOUNTS_OS_URL = (
    "https://api-os-takumi.mihoyo.com/binding/api/getUserGameRolesByCookie"
)
_GAME_ACCOUNTS_CN_URL = (
    "https://api-takumi.mihoyo.com/binding/api/getUserGameRolesByCookie"
)

# Claves RSA para cifrar credenciales antes de enviarlas.
# Copiadas de genshin.py/genshin/utility/auth.py
_RSA_KEY_1 = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA4PMS2JVMwBsOIrYWRluY
wEiFZL7ApCOqlnMlsn4ufLSFG4b/2TKiDLpyRFBmkJLq18jGCnFy1HwHINmGTx6X
T2kT3Pu3JV3SJQ2mCBWMPYZR4OdSz+mLyFXsGo7KTGBf+OJORQGpHsIf4CZUQT1
MVWWVYdqNYSSMGBNYLvomvtGPH2jEJ+rBCFuFAoNqxfllQ3xFSzHAyFlBGjqVGPH
aOQ8f3H4NHD5uBYSH6olhVXO1EVBQnKXe0YuSFjG3MWTvLyXbD4m3BfBoXrGmbQ6
T4kKGGATr7F0bEVBD9j+P3ERxUkYLGAerPt7M6OBjCLvPSO2XPwrE6XHVNAJUCBB
CQIDAQAB
-----END PUBLIC KEY-----"""

_RSA_KEY_2 = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAp3HKtSTWMWOoRGUO+eNG
4OFYD2jwQjy/s1A7GfBPTVBSepBVHJfKoqHhxFPGFQzHEJPCEMmPiP9BITBXrRcA
JHquuN4DeSyuF5H5DJdoHzMYIFLxF4RVQB1oMqh3JvvGDiexYcGkWVBVBt3q+3hI
OHGhBxnBpeHHVOwqRnXkHgBt7Eqf3ILTIH6kAGkMpEBHVsQEnHi2E3l+X/AMv3z9
C/HjFPZMOfbRHcSWmKjILhpMGK5nHhWpWsxLM+5UGQLaSnqt2Jca7qdJP+wN2zt7
BEI0m1AexGhBdL7ADMXYNKdJNqVFuKFBtI5sJ6XLjLv8m8mGmpxVE8gWB8DCBQID
AQAB
-----END PUBLIC KEY-----"""

# Headers base para login web OS (copiados de genshin.py/genshin/utility/auth.py)
_WEB_LOGIN_HEADERS: dict[str, str] = {
    "x-rpc-app_id":      "c9oqaq3s3gu8",
    "x-rpc-client_type": "4",
    "x-rpc-app_version": "2.55.0",
    "Origin":            "https://account.hoyolab.com",
    "Referer":           "https://account.hoyolab.com/",
    "Content-Type":      "application/json",
    "User-Agent":        _USER_AGENT,
}

# Headers base para login web CN (copiados de genshin.py/genshin/utility/auth.py)
_CN_LOGIN_HEADERS: dict[str, str] = {
    "x-rpc-app_id":      "bll8iq97cem8",
    "x-rpc-client_type": "4",
    "x-rpc-app_version": "2.55.0",
    "Origin":            "https://user.mihoyo.com",
    "Referer":           "https://user.mihoyo.com/",
    "Content-Type":      "application/json",
    "User-Agent":        _USER_AGENT,
}

# Headers para enviar/verificar código de email
_EMAIL_SEND_HEADERS: dict[str, str] = {
    "x-rpc-app_id":      "c9oqaq3s3gu8",
    "x-rpc-client_type": "4",
    "x-rpc-app_version": "2.55.0",
    "Content-Type":      "application/json",
    "User-Agent":        _USER_AGENT,
}

_EMAIL_VERIFY_HEADERS: dict[str, str] = {
    **_EMAIL_SEND_HEADERS,
}


# ---------------------------------------------------------------------------
# Utilidades de cifrado
# ---------------------------------------------------------------------------

def _encrypt_credentials(text: str, key_id: int = 1) -> str:
    """
    Cifra email o contraseña con RSA-PKCS1v1.5 antes de enviarlos a HoYoverse.
    Copiado de genshin.py/genshin/utility/auth.py

    Requiere: pip install pycryptodome
    """
    try:
        import base64
        from Crypto.Cipher import PKCS1_v1_5
        from Crypto.PublicKey import RSA
    except ImportError:
        raise ImportError(
            "Se requiere 'pycryptodome' para cifrar credenciales.\n"
            "Instálalo con: pip install pycryptodome"
        )

    key_pem = _RSA_KEY_1 if key_id == 1 else _RSA_KEY_2
    key = RSA.import_key(key_pem)
    cipher = PKCS1_v1_5.new(key)
    encrypted = cipher.encrypt(text.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


# ---------------------------------------------------------------------------
# Base de datos: modelos SQLAlchemy
# ---------------------------------------------------------------------------

class _Base(DeclarativeBase):
    pass


class _CookieRecord(_Base):
    """
    Almacena cookies de sesión de un usuario de HoYoLab.

    account_hash  SHA-256 del email normalizado (no se guarda el email en claro).
    region        'os' (HoYoLab global) o 'cn' (Miyoushe).
    cookies_json  JSON del dict de cookies.
    device_id     UUID del dispositivo usado en el login. Se reutiliza en
                  requests posteriores para simular un dispositivo real.
    updated_at    Última actualización (UTC).
    """
    __tablename__ = "hoyo_cookies"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    account_hash = Column(String(64), nullable=False, unique=True, index=True)
    region       = Column(String(8),  nullable=False)
    cookies_json = Column(Text,       nullable=False)
    device_id    = Column(String(36), nullable=False, default="")
    updated_at   = Column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------

class HoyoAPIError(Exception):
    """Error devuelto por la API de HoYoverse (retcode != 0)."""

    def __init__(self, retcode: int, message: str, raw: dict) -> None:
        self.retcode = retcode
        self.message = message
        self.raw = raw
        super().__init__(f"[{retcode}] {message}")


class LoginError(Exception):
    """Error durante el proceso de login."""


# ---------------------------------------------------------------------------
# HoyoSession: sesión autenticada
# ---------------------------------------------------------------------------

class HoyoSession:
    """
    Sesión autenticada con HoYoLab o Miyoushe.

    Atributos:
        cookies     dict de cookies activas de la sesión.
        region      'os' (HoYoLab global) o 'cn' (Miyoushe).
        device_id   UUID del dispositivo generado durante el login.
                    Se reutiliza en todos los requests posteriores para
                    mantener consistencia y evitar señales de automatización.

    Uso:
        session = await AuthManager().login("email", "pass")
        accounts = await session.get_game_accounts()
    """

    def __init__(
        self,
        cookies: dict[str, str],
        region: str,
        device_id: typing.Optional[str] = None,
    ) -> None:
        self.cookies = cookies
        self.region = region
        # Si no se proporciona device_id (e.g. sesión restaurada de DB sin él),
        # se genera uno nuevo. Un mismo dispositivo debe usar siempre el mismo ID.
        self.device_id: str = device_id or str(uuid.uuid4())

    @property
    def account_id(self) -> typing.Optional[int]:
        """ID numérico de la cuenta extraído de las cookies."""
        for key in ("ltuid_v2", "account_id_v2", "ltuid", "account_id"):
            if val := self.cookies.get(key):
                try:
                    return int(val)
                except ValueError:
                    pass
        return None

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: typing.Optional[dict] = None,
        # Renombrado de 'json' a 'body' para no shadowear el módulo json.
        body: typing.Optional[dict] = None,
        headers: typing.Optional[dict] = None,
    ) -> typing.Any:
        """
        Realiza una petición HTTP autenticada con las cookies de la sesión.

        Devuelve el contenido de data{} de la respuesta JSON de HoYoverse,
        o lanza HoyoAPIError si retcode != 0.

        Parámetros:
            method      'GET' o 'POST'.
            url         URL completa del endpoint.
            params      Query params opcionales.
            body        Cuerpo JSON del request (para POST).
            headers     Headers adicionales a los por defecto.
        """
        merged_headers = {"User-Agent": _USER_AGENT}
        if headers:
            merged_headers.update(headers)

        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar()
        ) as http_session:
            async with http_session.request(
                method,
                url,
                params=params,
                json=body,           # aiohttp espera 'json', no 'body'
                headers=merged_headers,
                cookies=self.cookies,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)

        retcode = data.get("retcode", 0)
        if retcode != 0:
            raise HoyoAPIError(
                retcode,
                data.get("message", "Unknown error"),
                data,
            )

        return data.get("data", data)

    async def get_game_accounts(self) -> list[dict]:
        """
        Devuelve la lista de cuentas de juego vinculadas al usuario.

        Cada elemento contiene:
            game_biz    Identificador del juego ('hk4e_global', 'hkrpg_global', ...)
            game_uid    UID del jugador en ese juego
            nickname    Nombre del personaje/jugador
            level       Nivel del jugador
            region      Servidor (e.g. 'os_asia', 'prod_gf_eu')
            region_name Nombre legible del servidor
        """
        url = (
            _GAME_ACCOUNTS_OS_URL
            if self.region == "os"
            else _GAME_ACCOUNTS_CN_URL
        )
        data = await self.request("GET", url)
        return data.get("list", [])

    def __repr__(self) -> str:
        return (
            f"<HoyoSession region={self.region!r} "
            f"account_id={self.account_id} "
            f"device_id={self.device_id!r} "
            f"cookies={list(self.cookies.keys())}>"
        )


# ---------------------------------------------------------------------------
# AuthManager
# ---------------------------------------------------------------------------

class AuthManager:
    """
    Gestor de autenticación con HoYoLab / Miyoushe.

    Parámetros:
        db_path         Ruta al archivo SQLite. Por defecto './hoyo_auth.db'.
        captcha_port    Puerto local para el servidor de captcha. Por defecto 5000.
        lang            Idioma del widget Geetest ('en', 'zh-cn', 'es', ...).

    Uso básico:
        manager = AuthManager()
        session = await manager.login("tu@email.com", "contraseña")
        accounts = await session.get_game_accounts()
    """

    def __init__(
        self,
        db_path: str = "./hoyo_auth.db",
        captcha_port: int = 5000,
        lang: str = "en",
    ) -> None:
        self._db_path = db_path
        self._captcha_port = captcha_port
        self._lang = lang

        engine = create_engine(f"sqlite:///{db_path}", echo=False)
        _Base.metadata.create_all(engine)
        self._SessionFactory = sessionmaker(bind=engine)

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    async def login(
        self,
        email: str,
        password: str,
        *,
        region: typing.Optional[typing.Literal["os", "cn"]] = None,
    ) -> HoyoSession:
        """
        Autentica al usuario y devuelve una HoyoSession lista para operar.

        Flujo:
          1. Busca cookies válidas en la DB (evita captcha y login innecesario).
          2. Si no hay o están caducadas, hace login con email + contraseña.
          3. Si HoYoverse activa un captcha Geetest, abre el navegador.
          4. Si HoYoverse pide verificación por email, solicita el código por consola.
          5. Guarda las cookies nuevas en la DB para futuros usos.
          6. Devuelve HoyoSession con el device_id del login persistido.

        Parámetros:
            email       Email o nombre de usuario de HoYoLab / Miyoushe.
            password    Contraseña de la cuenta.
            region      'os' o 'cn'. Si no se indica, se detecta automáticamente:
                        primero intenta OS, si falla intenta CN.

        Lanza:
            LoginError      Si las credenciales son incorrectas.
            RuntimeError    Si el captcha no se resuelve en 5 minutos.
        """
        # 1. Intentar cookies guardadas
        if region:
            cached_cookies, cached_device_id = self._load_cookies(email, region)
            if cached_cookies:
                session = HoyoSession(cached_cookies, region, cached_device_id)
                if await self._validate_session(session):
                    logger.info("Sesión restaurada desde DB para %s (%s)", email, region)
                    return session
                logger.info("Cookies caducadas para %s, haciendo login de nuevo.", email)
        else:
            for r in ("os", "cn"):
                cached_cookies, cached_device_id = self._load_cookies(email, r)  # type: ignore[arg-type]
                if cached_cookies:
                    session = HoyoSession(cached_cookies, r, cached_device_id)
                    if await self._validate_session(session):
                        logger.info("Sesión restaurada desde DB para %s (%s)", email, r)
                        return session

        # 2. Login con la API
        detected_region: typing.Literal["os", "cn"] = region or "os"
        try:
            cookies, device_id = await self._do_login(email, password, region=detected_region)
            final_region = detected_region
        except LoginError:
            if region is None and detected_region == "os":
                logger.info("Login OS fallido para %s, intentando CN...", email)
                cookies, device_id = await self._do_login(email, password, region="cn")
                final_region = "cn"
            else:
                raise

        # 3. Persistir cookies y device_id
        self._save_cookies(email, final_region, cookies, device_id)
        logger.info("Login exitoso para %s (%s)", email, final_region)

        return HoyoSession(cookies, final_region, device_id)

    async def logout(self, email: str) -> None:
        """Elimina las cookies guardadas de un usuario de la DB."""
        account_hash = _hash_email(email)
        with self._SessionFactory() as db:
            record = db.execute(
                select(_CookieRecord).where(
                    _CookieRecord.account_hash == account_hash
                )
            ).scalar_one_or_none()
            if record:
                db.delete(record)
                db.commit()
                logger.info("Cookies eliminadas para %s", email)

    # ------------------------------------------------------------------
    # Flujos de login
    # ------------------------------------------------------------------

    async def _do_login(
        self,
        email: str,
        password: str,
        region: typing.Literal["os", "cn"],
    ) -> tuple[dict[str, str], str]:
        """
        Despacha al flujo de login correcto según la región.

        Devuelve tupla (cookies, device_id).
        """
        if region == "os":
            return await self._os_login_flow(email, password)
        else:
            return await self._cn_login_flow(email, password)

    async def _os_login_flow(
        self, email: str, password: str
    ) -> tuple[dict[str, str], str]:
        """
        Login vía HoYoLab web endpoint (región global).

        Maneja:
          - Captcha Geetest (-3101): abre el navegador para resolverlo.
          - Verificación de email (-3239): solicita el código por consola.

        Devuelve tupla (cookies, device_id).
        """
        device_id = str(uuid.uuid4())
        headers = {
            **_WEB_LOGIN_HEADERS,
            "x-rpc-device_id": device_id,
        }
        payload = {
            "account":    _encrypt_credentials(email, 1),
            "password":   _encrypt_credentials(password, 1),
            "token_type": 6,
        }

        resp = await self._raw_post(_WEB_LOGIN_URL, payload, headers)

        # --- Captcha activado ---
        if resp["retcode"] == -3101:
            logger.info("Captcha activado en login OS. Abriendo navegador...")
            aigis_raw = resp["headers"].get("x-rpc-aigis", "{}")
            aigis = json.loads(aigis_raw)

            if isinstance(aigis.get("data"), str):
                aigis["data"] = json.loads(aigis["data"])

            mmt = SessionMMT(
                gt=aigis["data"]["gt"],
                challenge=aigis["data"]["challenge"],
                new_captcha=aigis["data"].get("new_captcha", 1),
                success=aigis.get("success", 1),
                session_id=aigis["session_id"],
            )
            mmt_result = await solve_geetest(
                mmt,
                lang=self._lang,
                api_server="api-na.geetest.com",
                port=self._captcha_port,
            )
            headers["x-rpc-aigis"] = _build_aigis_header(mmt_result)
            resp = await self._raw_post(_WEB_LOGIN_URL, payload, headers)

        # --- Verificación de email requerida (-3239) ---
        if resp["retcode"] == -3239:
            logger.info("Verificación de email requerida.")
            action_ticket_raw = resp["headers"].get("x-rpc-verify", "{}")
            action_ticket = json.loads(action_ticket_raw)

            if isinstance(action_ticket.get("verify_str"), str):
                action_ticket["verify_str"] = json.loads(action_ticket["verify_str"])

            ticket_str = action_ticket["verify_str"]["ticket"]

            await self._send_verification_email(ticket_str, headers=headers)

            code = await enter_code(
                "Introduce el código de verificación enviado a tu email: "
            )

            await self._verify_email_code(
                code=code,
                action_ticket=action_ticket["risk_ticket"],
                verify_ticket=ticket_str,
            )

            headers["x-rpc-verify"] = json.dumps({
                "risk_ticket":  action_ticket["risk_ticket"],
                "verify_str":   json.dumps(action_ticket["verify_str"]),
            })
            resp = await self._raw_post(_WEB_LOGIN_URL, payload, headers)

        if resp["retcode"] != 0:
            _raise_login_error(resp["retcode"], resp.get("message", ""))

        cookies = resp["cookies"]
        if resp["data"] and resp["data"].get("stoken"):
            cookies["stoken"] = resp["data"]["stoken"]

        if not cookies:
            raise LoginError(
                "Login OS completado pero no se recibieron cookies. "
                "Puede que la cuenta requiera pasos adicionales."
            )

        return cookies, device_id

    async def _cn_login_flow(
        self, email: str, password: str
    ) -> tuple[dict[str, str], str]:
        """
        Login vía Miyoushe web endpoint (región CN).

        Maneja captcha Geetest (-3102).
        Devuelve tupla (cookies, device_id).
        """
        device_id = str(uuid.uuid4())
        headers = {
            **_CN_LOGIN_HEADERS,
            "x-rpc-device_id": device_id,
        }
        payload = {
            "account":  _encrypt_credentials(email, 2),
            "password": _encrypt_credentials(password, 2),
        }

        resp = await self._raw_post(_CN_WEB_LOGIN_URL, payload, headers)

        # --- Captcha activado ---
        if resp["retcode"] == -3102:
            logger.info("Captcha activado en login CN. Abriendo navegador...")
            aigis_raw = resp["headers"].get("x-rpc-aigis", "{}")
            aigis = json.loads(aigis_raw)

            if isinstance(aigis.get("data"), str):
                aigis["data"] = json.loads(aigis["data"])

            mmt = SessionMMT(
                gt=aigis["data"]["gt"],
                challenge=aigis["data"]["challenge"],
                new_captcha=aigis["data"].get("new_captcha", 1),
                success=aigis.get("success", 1),
                session_id=aigis["session_id"],
            )
            mmt_result = await solve_geetest(
                mmt,
                lang="zh-cn",
                api_server="api.geetest.com",
                port=self._captcha_port,
            )
            headers["x-rpc-aigis"] = _build_aigis_header(mmt_result)
            resp = await self._raw_post(_CN_WEB_LOGIN_URL, payload, headers)

        if resp["retcode"] != 0:
            _raise_login_error(resp["retcode"], resp.get("message", ""))

        cookies = resp["cookies"]
        if not cookies:
            raise LoginError(
                "Login CN completado pero no se recibieron cookies. "
                "Puede que la cuenta requiera pasos adicionales."
            )

        return cookies, device_id

    # ------------------------------------------------------------------
    # Verificación de email
    # ------------------------------------------------------------------

    async def _send_verification_email(
        self,
        ticket: str,
        *,
        headers: typing.Optional[dict] = None,
    ) -> None:
        """
        Solicita a HoYoverse que envíe el código de verificación al email del usuario.
        Copiado de genshin.py/genshin/client/components/auth/app.py
        """
        send_headers = {**_EMAIL_SEND_HEADERS}
        if headers:
            for key in ("x-rpc-aigis", "x-rpc-device_id"):
                if key in headers:
                    send_headers[key] = headers[key]

        resp = await self._raw_post(
            _SEND_VERIFICATION_URL,
            {
                "action_type":   "verify_for_component",
                "action_ticket": ticket,
            },
            send_headers,
        )

        if resp["retcode"] != 0:
            raise LoginError(
                f"No se pudo enviar el email de verificación: "
                f"[{resp['retcode']}] {resp.get('message', '')}"
            )

    async def _verify_email_code(
        self,
        code: str,
        action_ticket: str,
        verify_ticket: str,
    ) -> None:
        """
        Verifica el código de email introducido por el usuario.
        Copiado de genshin.py/genshin/client/components/auth/app.py
        """
        resp = await self._raw_post(
            _VERIFY_EMAIL_URL,
            {
                "action_type":    "verify_for_component",
                "action_ticket":  verify_ticket,
                "email_captcha":  code,
                "verify_method":  2,
            },
            _EMAIL_VERIFY_HEADERS,
        )

        if resp["retcode"] != 0:
            raise LoginError(
                f"Código de verificación incorrecto o caducado: "
                f"[{resp['retcode']}] {resp.get('message', '')}"
            )

    # ------------------------------------------------------------------
    # HTTP raw (necesitamos acceso a headers Y cookies de la respuesta)
    # ------------------------------------------------------------------

    async def _raw_post(
        self,
        url: str,
        payload: dict,
        headers: dict,
    ) -> dict:
        """
        POST sin autenticación previa.

        Devuelve un dict con:
            retcode     int     Código de resultado de HoYoverse (0 = éxito).
            message     str     Mensaje de error si retcode != 0.
            data        dict    Datos de la respuesta (puede ser None).
            headers     dict    Cabeceras de la respuesta HTTP.
            cookies     dict    Cookies de la respuesta HTTP.

        No lanza excepciones por retcode != 0: el llamador decide qué hacer.
        """
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar()
        ) as session:
            async with session.post(
                url,
                json=payload,
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                body = await resp.json(content_type=None)
                resp_cookies = {
                    cookie.key: cookie.value
                    for cookie in resp.cookies.values()
                }
                resp_headers = dict(resp.headers)

        return {
            "retcode": body.get("retcode", 0),
            "message": body.get("message", ""),
            "data":    body.get("data"),
            "headers": resp_headers,
            "cookies": resp_cookies,
        }

    # ------------------------------------------------------------------
    # Validación de sesión
    # ------------------------------------------------------------------

    async def _validate_session(self, session: HoyoSession) -> bool:
        """
        Comprueba si las cookies de una sesión siguen siendo válidas
        intentando obtener las cuentas de juego del usuario.

        Devuelve True si son válidas, False si han caducado o son inválidas.
        """
        try:
            await session.get_game_accounts()
            return True
        except (HoyoAPIError, aiohttp.ClientError):
            return False

    # ------------------------------------------------------------------
    # Persistencia en DB
    # ------------------------------------------------------------------

    def _save_cookies(
        self,
        email: str,
        region: str,
        cookies: dict[str, str],
        device_id: str,
    ) -> None:
        """Guarda o actualiza cookies y device_id de un usuario en la DB (upsert)."""
        account_hash = _hash_email(email)
        now = datetime.now(timezone.utc)

        with self._SessionFactory() as db:
            record = db.execute(
                select(_CookieRecord).where(
                    _CookieRecord.account_hash == account_hash
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

    def _load_cookies(
        self,
        email: str,
        region: str,
    ) -> tuple[typing.Optional[dict[str, str]], typing.Optional[str]]:
        """
        Carga cookies y device_id guardados para un usuario y región.
        Devuelve (None, None) si no existen registros.
        """
        account_hash = _hash_email(email)

        with self._SessionFactory() as db:
            record = db.execute(
                select(_CookieRecord).where(
                    _CookieRecord.account_hash == account_hash,
                    _CookieRecord.region == region,
                )
            ).scalar_one_or_none()

        if record is None:
            return None, None

        return json.loads(record.cookies_json), record.device_id or None


# ---------------------------------------------------------------------------
# Funciones de utilidad a nivel de módulo (sin estado)
# ---------------------------------------------------------------------------

def _hash_email(email: str) -> str:
    """SHA-256 del email normalizado. Nunca guardamos el email en claro."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def _build_aigis_header(mmt_result: SessionMMTResult) -> str:
    """
    Construye el valor del header x-rpc-aigis a partir del resultado
    del captcha Geetest resuelto por el usuario.
    Copiado de genshin.py/genshin/utility/auth.py
    """
    session_id = mmt_result["session_id"]
    data = {
        "geetest_challenge": mmt_result["geetest_challenge"],
        "geetest_validate":  mmt_result["geetest_validate"],
        "geetest_seccode":   mmt_result["geetest_seccode"],
    }
    return json.dumps({"session_id": session_id, "data": json.dumps(data)})


def _raise_login_error(retcode: int, message: str) -> typing.NoReturn:
    """
    Convierte retcodes de HoYoverse en LoginError con mensajes descriptivos.
    Lanza siempre.
    """
    _MESSAGES: dict[int, str] = {
        -3001: "Cuenta o contraseña incorrectos.",
        -3002: "La cuenta no existe.",
        -3003: "Contraseña incorrecta.",
        -3004: "La cuenta está bloqueada.",
        -3101: "Captcha requerido (OS) pero no fue resuelto.",
        -3102: "Captcha requerido (CN) pero no fue resuelto.",
        -3104: "Falta el header x-rpc-device_id en la petición.",
        -3239: "Verificación de email requerida pero no completada.",
    }
    msg = _MESSAGES.get(retcode, f"Error de login [{retcode}]: {message}")
    raise LoginError(msg)
