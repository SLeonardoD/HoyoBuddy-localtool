"""
HoyoWrapper/auth_manager.py
---------------------------
Lógica de autenticación con HoYoLab / Miyoushe.

Responsabilidades:
  - Login con email + contraseña (regiones OS y CN).
  - Manejo del captcha Geetest (delega a captcha_server.py).
  - Manejo de verificación por email.
  - Validación de sesiones existentes.
  - Coordinación con CookieStore para persistencia (inyectado en __init__).

No contiene ninguna lógica de DB. CookieStore es la única interfaz
con la capa de persistencia y puede ser reemplazada sin tocar este archivo.

Imports de DB:
    Ninguno. CookieStore se recibe como dependencia externa.
"""

from __future__ import annotations

import json
import logging
import typing
import uuid

import aiohttp

from HoyoWrapper.captcha_server import SessionMMT, SessionMMTResult, enter_code, solve_geetest
from HoyoWrapper.session import HoyoAPIError, HoyoSession, LoginError

__all__ = ["AuthManager"]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

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

_WEB_LOGIN_HEADERS: dict[str, str] = {
    "x-rpc-app_id":      "c9oqaq3s3gu8",
    "x-rpc-client_type": "4",
    "x-rpc-app_version": "2.55.0",
    "Origin":            "https://account.hoyolab.com",
    "Referer":           "https://account.hoyolab.com/",
    "Content-Type":      "application/json",
    "User-Agent":        _USER_AGENT,
}

_CN_LOGIN_HEADERS: dict[str, str] = {
    "x-rpc-app_id":      "bll8iq97cem8",
    "x-rpc-client_type": "4",
    "x-rpc-app_version": "2.55.0",
    "Origin":            "https://user.mihoyo.com",
    "Referer":           "https://user.mihoyo.com/",
    "Content-Type":      "application/json",
    "User-Agent":        _USER_AGENT,
}

_EMAIL_SEND_HEADERS: dict[str, str] = {
    "x-rpc-app_id":      "c9oqaq3s3gu8",
    "x-rpc-client_type": "4",
    "x-rpc-app_version": "2.55.0",
    "Content-Type":      "application/json",
    "User-Agent":        _USER_AGENT,
}

_EMAIL_VERIFY_HEADERS: dict[str, str] = {**_EMAIL_SEND_HEADERS}


# ---------------------------------------------------------------------------
# Utilidades de cifrado
# ---------------------------------------------------------------------------

def _encrypt_credentials(text: str, key_id: int = 1) -> str:
    """
    Cifra email o contraseña con RSA-PKCS1v1.5 antes de enviarlos a HoYoverse.
    Requiere: pip install pycryptodome
    """
    try:
        import base64
        from Crypto.Cipher import PKCS1_v1_5
        from Crypto.PublicKey import RSA
    except ImportError:
        raise ImportError(
            "Se requiere 'pycryptodome'.\nInstálalo con: pip install pycryptodome"
        )

    key_pem = _RSA_KEY_1 if key_id == 1 else _RSA_KEY_2
    key = RSA.import_key(key_pem)
    cipher = PKCS1_v1_5.new(key)
    encrypted = cipher.encrypt(text.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


# ---------------------------------------------------------------------------
# AuthManager
# ---------------------------------------------------------------------------

# Tipo mínimo que debe satisfacer cualquier CookieStore inyectado.
# No importamos CookieStore directamente para mantener el desacoplamiento.
class _CookieStoreProtocol(typing.Protocol):
    def get(self, email: str, region: str) -> tuple[
        typing.Optional[dict[str, str]], typing.Optional[str]
    ]: ...
    def set(self, email: str, region: str, cookies: dict[str, str], device_id: str) -> None: ...
    def delete(self, email: str) -> None: ...


class AuthManager:
    """
    Gestor de autenticación con HoYoLab / Miyoushe.

    Parámetros:
        store           Instancia de CookieStore (o cualquier objeto compatible).
                        Inyectado externamente: AuthManager no crea ni conoce la DB.
        captcha_port    Puerto local para el servidor de captcha. Por defecto 5000.
        lang            Idioma del widget Geetest ('en', 'zh-cn', 'es', ...).

    Uso:
        from db import CookieStore
        from HoyoWrapper import AuthManager

        store = CookieStore("./hoyo_auth.db")
        auth  = AuthManager(store)
        session = await auth.login("email", "pass")
    """

    def __init__(
        self,
        store: _CookieStoreProtocol,
        captcha_port: int = 5000,
        lang: str = "en",
    ) -> None:
        self._store = store
        self._captcha_port = captcha_port
        self._lang = lang

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
          1. Busca cookies válidas en el store.
          2. Si no hay o están caducadas, hace login con email + contraseña.
          3. Si HoYoverse activa captcha, abre el navegador.
          4. Si pide verificación de email, solicita el código por consola.
          5. Guarda las cookies nuevas en el store.

        Lanza:
            LoginError      Credenciales incorrectas.
            RuntimeError    Captcha no resuelto en 5 minutos.
        """
        # 1. Intentar cookies guardadas
        regions_to_try = [region] if region else ["os", "cn"]
        for r in regions_to_try:
            cookies, device_id = self._store.get(email, r)
            if cookies:
                session = HoyoSession(cookies, r, device_id)
                if await self._validate_session(session):
                    logger.info("Sesión restaurada desde store para %s (%s).", email, r)
                    return session
                logger.info("Cookies caducadas para %s (%s), rehaciendo login.", email, r)

        # 2. Login con la API
        detected: typing.Literal["os", "cn"] = region or "os"
        try:
            cookies, device_id = await self._do_login(email, password, region=detected)
            final_region = detected
        except LoginError:
            if region is None and detected == "os":
                logger.info("Login OS fallido para %s, intentando CN...", email)
                cookies, device_id = await self._do_login(email, password, region="cn")
                final_region = "cn"
            else:
                raise

        # 3. Persistir
        self._store.set(email, final_region, cookies, device_id)
        logger.info("Login exitoso para %s (%s).", email, final_region)

        return HoyoSession(cookies, final_region, device_id)

    async def logout(self, email: str) -> None:
        """Elimina las cookies guardadas del usuario del store."""
        self._store.delete(email)
        logger.info("Cookies eliminadas para %s.", email)

    # ------------------------------------------------------------------
    # Flujos de login
    # ------------------------------------------------------------------

    async def _do_login(
        self,
        email: str,
        password: str,
        region: typing.Literal["os", "cn"],
    ) -> tuple[dict[str, str], str]:
        """Despacha al flujo correcto según región. Devuelve (cookies, device_id)."""
        if region == "os":
            return await self._os_login_flow(email, password)
        return await self._cn_login_flow(email, password)

    async def _os_login_flow(
        self, email: str, password: str
    ) -> tuple[dict[str, str], str]:
        """
        Login OS (HoYoLab global). Maneja captcha (-3101) y verificación email (-3239).
        Devuelve (cookies, device_id).
        """
        device_id = str(uuid.uuid4())
        headers = {**_WEB_LOGIN_HEADERS, "x-rpc-device_id": device_id}
        payload = {
            "account":    _encrypt_credentials(email, 1),
            "password":   _encrypt_credentials(password, 1),
            "token_type": 6,
        }

        resp = await self._raw_post(_WEB_LOGIN_URL, payload, headers)

        # Captcha
        if resp["retcode"] == -3101:
            logger.info("Captcha activado en login OS.")
            aigis = json.loads(resp["headers"].get("x-rpc-aigis", "{}"))
            if isinstance(aigis.get("data"), str):
                aigis["data"] = json.loads(aigis["data"])

            mmt_result = await solve_geetest(
                SessionMMT(
                    gt=aigis["data"]["gt"],
                    challenge=aigis["data"]["challenge"],
                    new_captcha=aigis["data"].get("new_captcha", 1),
                    success=aigis.get("success", 1),
                    session_id=aigis["session_id"],
                ),
                lang=self._lang,
                api_server="api-na.geetest.com",
                port=self._captcha_port,
            )
            headers["x-rpc-aigis"] = _build_aigis_header(mmt_result)
            resp = await self._raw_post(_WEB_LOGIN_URL, payload, headers)

        # Verificación de email
        if resp["retcode"] == -3239:
            logger.info("Verificación de email requerida.")
            action_ticket = json.loads(resp["headers"].get("x-rpc-verify", "{}"))
            if isinstance(action_ticket.get("verify_str"), str):
                action_ticket["verify_str"] = json.loads(action_ticket["verify_str"])

            ticket_str = action_ticket["verify_str"]["ticket"]
            await self._send_verification_email(ticket_str, headers=headers)
            code = await enter_code("Introduce el código de verificación enviado a tu email: ")
            await self._verify_email_code(
                code=code,
                action_ticket=action_ticket["risk_ticket"],
                verify_ticket=ticket_str,
            )
            headers["x-rpc-verify"] = json.dumps({
                "risk_ticket": action_ticket["risk_ticket"],
                "verify_str":  json.dumps(action_ticket["verify_str"]),
            })
            resp = await self._raw_post(_WEB_LOGIN_URL, payload, headers)

        if resp["retcode"] != 0:
            _raise_login_error(resp["retcode"], resp.get("message", ""))

        cookies = resp["cookies"]
        if resp["data"] and resp["data"].get("stoken"):
            cookies["stoken"] = resp["data"]["stoken"]

        if not cookies:
            raise LoginError("Login OS completado pero no se recibieron cookies.")

        return cookies, device_id

    async def _cn_login_flow(
        self, email: str, password: str
    ) -> tuple[dict[str, str], str]:
        """
        Login CN (Miyoushe). Maneja captcha (-3102).
        Devuelve (cookies, device_id).
        """
        device_id = str(uuid.uuid4())
        headers = {**_CN_LOGIN_HEADERS, "x-rpc-device_id": device_id}
        payload = {
            "account":  _encrypt_credentials(email, 2),
            "password": _encrypt_credentials(password, 2),
        }

        resp = await self._raw_post(_CN_WEB_LOGIN_URL, payload, headers)

        if resp["retcode"] == -3102:
            logger.info("Captcha activado en login CN.")
            aigis = json.loads(resp["headers"].get("x-rpc-aigis", "{}"))
            if isinstance(aigis.get("data"), str):
                aigis["data"] = json.loads(aigis["data"])

            mmt_result = await solve_geetest(
                SessionMMT(
                    gt=aigis["data"]["gt"],
                    challenge=aigis["data"]["challenge"],
                    new_captcha=aigis["data"].get("new_captcha", 1),
                    success=aigis.get("success", 1),
                    session_id=aigis["session_id"],
                ),
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
            raise LoginError("Login CN completado pero no se recibieron cookies.")

        return cookies, device_id

    # ------------------------------------------------------------------
    # Verificación de email
    # ------------------------------------------------------------------

    async def _send_verification_email(
        self, ticket: str, *, headers: typing.Optional[dict] = None
    ) -> None:
        send_headers = {**_EMAIL_SEND_HEADERS}
        if headers:
            for key in ("x-rpc-aigis", "x-rpc-device_id"):
                if key in headers:
                    send_headers[key] = headers[key]

        resp = await self._raw_post(
            _SEND_VERIFICATION_URL,
            {"action_type": "verify_for_component", "action_ticket": ticket},
            send_headers,
        )
        if resp["retcode"] != 0:
            raise LoginError(
                f"No se pudo enviar el email de verificación: [{resp['retcode']}] {resp.get('message', '')}"
            )

    async def _verify_email_code(
        self, code: str, action_ticket: str, verify_ticket: str
    ) -> None:
        resp = await self._raw_post(
            _VERIFY_EMAIL_URL,
            {
                "action_type":   "verify_for_component",
                "action_ticket": verify_ticket,
                "email_captcha": code,
                "verify_method": 2,
            },
            _EMAIL_VERIFY_HEADERS,
        )
        if resp["retcode"] != 0:
            raise LoginError(
                f"Código incorrecto o caducado: [{resp['retcode']}] {resp.get('message', '')}"
            )

    # ------------------------------------------------------------------
    # HTTP raw
    # ------------------------------------------------------------------

    async def _raw_post(self, url: str, payload: dict, headers: dict) -> dict:
        """
        POST sin autenticación previa. Devuelve dict con retcode, message,
        data, headers de respuesta y cookies de respuesta.
        No lanza por retcode != 0.
        """
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as s:
            async with s.post(url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                body = await resp.json(content_type=None)
                resp_cookies = {c.key: c.value for c in resp.cookies.values()}
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
        """True si las cookies siguen siendo válidas."""
        try:
            await session.get_game_accounts()
            return True
        except (HoyoAPIError, aiohttp.ClientError):
            return False


# ---------------------------------------------------------------------------
# Utilidades de módulo
# ---------------------------------------------------------------------------

def _build_aigis_header(mmt_result: SessionMMTResult) -> str:
    data = {
        "geetest_challenge": mmt_result["geetest_challenge"],
        "geetest_validate":  mmt_result["geetest_validate"],
        "geetest_seccode":   mmt_result["geetest_seccode"],
    }
    return json.dumps({"session_id": mmt_result["session_id"], "data": json.dumps(data)})


def _raise_login_error(retcode: int, message: str) -> typing.NoReturn:
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
    raise LoginError(_MESSAGES.get(retcode, f"Error de login [{retcode}]: {message}"))
