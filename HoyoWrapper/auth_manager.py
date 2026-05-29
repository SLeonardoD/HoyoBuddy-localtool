"""
HoyoWrapper/auth_manager.py
---------------------------
Logica de autenticacion con HoYoLab / Miyoushe.

Logging exhaustivo via loguru para diagnostico en vivo:
  - Cada request HTTP loguea URL, retcode, headers relevantes y body.
  - Cada rama de flujo (captcha, email, cookies) loguea su estado.
  - Errores inesperados loguean el contexto completo.

No contiene logica de DB. CookieStore se recibe por inyeccion.
"""

from __future__ import annotations

import json
import typing
import uuid

import aiohttp
from loguru import logger

from HoyoWrapper.captcha_server import SessionMMT, SessionMMTResult, enter_code, solve_geetest
from HoyoWrapper.session import HoyoAPIError, HoyoSession, LoginError

__all__ = ["AuthManager"]

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

_RSA_KEY_1 = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA4PMS2JVMwBsOIrYWRluY\n"
    "wEiFZL7Aphtm9z5Eu/anzJ09nB00uhW+ScrDWFECPwpQto/GlOJYCUwVM/raQpAj\n"
    "/xvcjK5tNVzzK94mhk+j9RiQ+aWHaTXmOgurhxSp3YbwlRDvOgcq5yPiTz0+kSeK\n"
    "ZJcGeJ95bvJ+hJ/UMP0Zx2qB5PElZmiKvfiNqVUk8A8oxLJdBB5eCpqWV6CUqDKQ\n"
    "KSQP4sM0mZvQ1Sr4UcACVcYgYnCbTZMWhJTWkrNXqI8TMomekgny3y+d6NX/cFa6\n"
    "6jozFIF4HCX5aW8bp8C8vq2tFvFbleQ/Q3CU56EWWKMrOcpmFtRmC18s9biZBVR/\n"
    "8QIDAQAB\n"
    "-----END PUBLIC KEY-----"
)

_RSA_KEY_2 = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDDvekdPMHN3AYhm/vktJT+YJr7\n"
    "cI5DcsNKqdsx5DZX0gDuWFuIjzdwButrIYPNmRJ1G8ybDIF7oDW2eEpm5sMbL9zs\n"
    "9ExXCdvqrn51qELbqj0XxtMTIpaCHFSI50PfPpTFV9Xt/hmyVwokoOXFlAEgCn+Q\n"
    "CgGs52bFoYMtyi+xEQIDAQAB\n"
    "-----END PUBLIC KEY-----"
)

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

# Retcodes conocidos de captcha — se expanden aqui si HoYoverse añade nuevos
_CAPTCHA_RETCODES_OS: frozenset[int] = frozenset({-3101, -3200, -3206})
_CAPTCHA_RETCODES_CN: frozenset[int] = frozenset({-3102, -3200, -3206})

# Mensajes de errores de credenciales — usados para decidir si hacer fallback a CN
_MESSAGES_FOR_FALLBACK: dict[int, str] = {
    -3001: "Cuenta o contrasena incorrectos.",
    -3002: "La cuenta no existe.",
    -3003: "Contrasena incorrecta.",
}


# ---------------------------------------------------------------------------
# Utilidades de cifrado
# ---------------------------------------------------------------------------

def _encrypt_credentials(text: str, key_id: int = 1) -> str:
    """
    Cifra email o contrasena con RSA-PKCS1v15 antes de enviarlos a HoYoverse.
    Requiere: pip install cryptography
    """
    try:
        import base64
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        raise ImportError(
            "Se requiere 'cryptography'. Instalalo con: pip install cryptography"
        )

    key_pem = _RSA_KEY_1 if key_id == 1 else _RSA_KEY_2
    key_bytes = key_pem.replace("\\n", "\n").encode("utf-8")

    try:
        public_key = serialization.load_pem_public_key(key_bytes)
    except Exception as e:
        logger.error(
            "[AUTH] Error al cargar clave RSA key_id={}: {} | "
            "primeros 80 chars de la clave: {}",
            key_id, e, key_pem[:80]
        )
        raise

    encrypted = public_key.encrypt(text.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode("utf-8")


# ---------------------------------------------------------------------------
# Utilidad de diagnostico de respuesta HTTP
# ---------------------------------------------------------------------------

def _log_response(label: str, resp: dict) -> None:
    """
    Loguea en DEBUG el contenido completo de una respuesta _raw_post.
    Filtra headers relevantes para no llenar el log de ruido.
    """
    relevant_headers = {
        k: v for k, v in resp.get("headers", {}).items()
        if any(kw in k.lower() for kw in (
            "aigis", "verify", "rpc", "content-type", "set-cookie"
        ))
    }
    logger.debug(
        "[AUTH] {} | retcode={} | message={!r} | "
        "data={} | cookies_keys={} | relevant_headers={}",
        label,
        resp.get("retcode"),
        resp.get("message", ""),
        resp.get("data"),
        list(resp.get("cookies", {}).keys()),
        relevant_headers,
    )


# ---------------------------------------------------------------------------
# AuthManager
# ---------------------------------------------------------------------------

class _CookieStoreProtocol(typing.Protocol):
    def get(self, email: str, region: str) -> tuple[
        typing.Optional[dict[str, str]], typing.Optional[str]
    ]: ...
    def set(self, email: str, region: str, cookies: dict[str, str], device_id: str) -> None: ...
    def delete(self, email: str) -> None: ...


class AuthManager:
    """
    Gestor de autenticacion con HoYoLab / Miyoushe.

    Parametros:
        store           CookieStore inyectado externamente.
        captcha_port    Puerto local del servidor de captcha.
        lang            Idioma del widget Geetest.
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
    # API publica
    # ------------------------------------------------------------------

    async def login(
        self,
        email: str,
        password: str,
        *,
        region: typing.Optional[typing.Literal["os", "cn"]] = None,
    ) -> HoyoSession:
        """
        Autentica y devuelve HoyoSession lista para operar.
        Intenta cookies guardadas primero. Si no hay o expiraron, hace login.
        """
        logger.debug("[AUTH] login() iniciado para email={} region={}", email, region)

        # 1. Intentar cookies guardadas
        regions_to_try = [region] if region else ["os", "cn"]
        for r in regions_to_try:
            cookies, device_id = self._store.get(email, r)
            if cookies:
                logger.debug(
                    "[AUTH] Cookies encontradas en store para region={}. "
                    "Validando...", r
                )
                session = HoyoSession(cookies, r, device_id)
                if await self._validate_session(session):
                    logger.info("[AUTH] Sesion restaurada desde store ({}).", r)
                    return session
                logger.info(
                    "[AUTH] Cookies expiradas para region={}. "
                    "Procediendo con login.", r
                )
            else:
                logger.debug("[AUTH] Sin cookies en store para region={}.", r)

        # 2. Login con la API
        detected: typing.Literal["os", "cn"] = region or "os"
        logger.debug("[AUTH] Intentando login directo. Region detectada: {}", detected)

        try:
            cookies, device_id = await self._do_login(email, password, region=detected)
            final_region = detected
        except LoginError as e:
            # Solo hacer fallback a CN si:
            # 1. No se especifico region explicita
            # 2. El intento era OS
            # 3. El error es de credenciales (no de captcha ni de red)
            # Un error de captcha significa que OS reconocio el email — no tiene
            # sentido intentar CN, y ademas miyoushe.com no es accesible fuera de China.
            credential_errors = {-3001, -3002, -3003}
            is_credential_error = any(
                f"[{code}]" in str(e) or _MESSAGES_FOR_FALLBACK.get(code) in str(e)
                for code in credential_errors
            )
            if region is None and detected == "os" and is_credential_error:
                logger.info(
                    "[AUTH] Login OS fallido por credenciales. Intentando CN...", 
                )
                cookies, device_id = await self._do_login(email, password, region="cn")
                final_region = "cn"
            else:
                logger.error(
                    "[AUTH] Login fallido. No se intentara CN. Error: {}", e
                )
                raise

        # 3. Persistir
        self._store.set(email, final_region, cookies, device_id)
        logger.info("[AUTH] Login exitoso. Region={} | Cookies={}", 
                    final_region, list(cookies.keys()))

        return HoyoSession(cookies, final_region, device_id)

    async def logout(self, email: str) -> None:
        """Elimina cookies del usuario del store."""
        self._store.delete(email)
        logger.info("[AUTH] Cookies eliminadas para email={}", email)

    # ------------------------------------------------------------------
    # Flujos de login
    # ------------------------------------------------------------------

    async def _do_login(
        self,
        email: str,
        password: str,
        region: typing.Literal["os", "cn"],
    ) -> tuple[dict[str, str], str]:
        logger.debug("[AUTH] _do_login region={}", region)
        if region == "os":
            return await self._os_login_flow(email, password)
        return await self._cn_login_flow(email, password)

    async def _os_login_flow(
        self, email: str, password: str
    ) -> tuple[dict[str, str], str]:
        """Login OS. Maneja captcha y verificacion email."""
        logger.debug("[AUTH] _os_login_flow iniciado.")
        device_id = str(uuid.uuid4())
        logger.debug("[AUTH] device_id generado: {}", device_id)

        headers = {**_WEB_LOGIN_HEADERS, "x-rpc-device_id": device_id}
        payload = {
            "account":    _encrypt_credentials(email, 1),
            "password":   _encrypt_credentials(password, 1),
            "token_type": 6,
        }
        logger.debug("[AUTH] Payload OS cifrado OK. Enviando request inicial...")

        resp = await self._raw_post(_WEB_LOGIN_URL, payload, headers)
        _log_response("OS login inicial", resp)

        # ── Captcha ──────────────────────────────────────────────────────
        if resp["retcode"] in _CAPTCHA_RETCODES_OS:
            logger.info(
                "[AUTH] Captcha requerido en OS (retcode={}). "
                "Procesando widget Geetest...", resp["retcode"]
            )
            resp = await self._handle_captcha_os(resp, payload, headers)
            _log_response("OS login post-captcha", resp)

        # ── Verificacion de email ─────────────────────────────────────────
        if resp["retcode"] == -3239:
            logger.info("[AUTH] Verificacion de email requerida en OS.")
            resp = await self._handle_email_verification(resp, payload, headers)
            _log_response("OS login post-email", resp)

        # ── Resultado final ───────────────────────────────────────────────
        if resp["retcode"] != 0:
            logger.error(
                "[AUTH] Login OS fallido. retcode={} message={!r} data={}",
                resp["retcode"], resp.get("message"), resp.get("data")
            )
            _raise_login_error(resp["retcode"], resp.get("message", ""))

        cookies = resp["cookies"]
        logger.debug("[AUTH] Cookies OS recibidas: {}", list(cookies.keys()))

        if resp["data"] and resp["data"].get("stoken"):
            cookies["stoken"] = resp["data"]["stoken"]
            logger.debug("[AUTH] stoken añadido a cookies OS.")

        if not cookies:
            logger.error(
                "[AUTH] Login OS retcode=0 pero sin cookies. "
                "data={} headers_cookies={}",
                resp.get("data"), resp.get("cookies")
            )
            raise LoginError("Login OS completado pero no se recibieron cookies.")

        logger.info("[AUTH] _os_login_flow completado. Cookies: {}", list(cookies.keys()))
        return cookies, device_id

    async def _cn_login_flow(
        self, email: str, password: str
    ) -> tuple[dict[str, str], str]:
        """Login CN. Maneja captcha."""
        logger.debug("[AUTH] _cn_login_flow iniciado.")
        device_id = str(uuid.uuid4())
        logger.debug("[AUTH] device_id CN generado: {}", device_id)

        headers = {**_CN_LOGIN_HEADERS, "x-rpc-device_id": device_id}
        payload = {
            "account":  _encrypt_credentials(email, 2),
            "password": _encrypt_credentials(password, 2),
        }
        logger.debug("[AUTH] Payload CN cifrado OK. Enviando request inicial...")

        resp = await self._raw_post(_CN_WEB_LOGIN_URL, payload, headers)
        _log_response("CN login inicial", resp)

        if resp["retcode"] in _CAPTCHA_RETCODES_CN:
            logger.info(
                "[AUTH] Captcha requerido en CN (retcode={}). "
                "Procesando widget Geetest...", resp["retcode"]
            )
            resp = await self._handle_captcha_cn(resp, payload, headers)
            _log_response("CN login post-captcha", resp)

        if resp["retcode"] != 0:
            logger.error(
                "[AUTH] Login CN fallido. retcode={} message={!r} data={}",
                resp["retcode"], resp.get("message"), resp.get("data")
            )
            _raise_login_error(resp["retcode"], resp.get("message", ""))

        cookies = resp["cookies"]
        logger.debug("[AUTH] Cookies CN recibidas: {}", list(cookies.keys()))

        if not cookies:
            logger.error(
                "[AUTH] Login CN retcode=0 pero sin cookies. "
                "data={} headers_cookies={}",
                resp.get("data"), resp.get("cookies")
            )
            raise LoginError("Login CN completado pero no se recibieron cookies.")

        logger.info("[AUTH] _cn_login_flow completado. Cookies: {}", list(cookies.keys()))
        return cookies, device_id

    # ------------------------------------------------------------------
    # Manejo de captcha
    # ------------------------------------------------------------------

    def _extract_captcha_data(self, resp: dict, region: str) -> dict:
        """
        Extrae los datos del captcha Geetest de la respuesta.

        HoYoverse puede devolver los datos del captcha en:
          - resp["headers"]["x-rpc-aigis"]  (formato clasico)
          - resp["data"]                    (formato nuevo, observado 2025+)
          - resp["data"]["mmt_data"]        (variante del formato nuevo)

        Loguea la estructura completa para diagnostico cuando no se encuentra
        el campo esperado.
        """
        # Intentar x-rpc-aigis primero (formato clasico)
        aigis_raw = resp["headers"].get("x-rpc-aigis", "")
        logger.debug(
            "[AUTH] Captcha extract ({}): x-rpc-aigis={!r} | data={}",
            region, aigis_raw, resp.get("data")
        )

        if aigis_raw:
            try:
                aigis = json.loads(aigis_raw)
                # data puede ser string JSON anidado
                if isinstance(aigis.get("data"), str):
                    aigis["data"] = json.loads(aigis["data"])

                captcha_data = aigis.get("data") or {}
                # gt puede estar en data o en el nivel raiz
                if not captcha_data.get("gt"):
                    captcha_data = aigis

                logger.debug(
                    "[AUTH] Captcha via x-rpc-aigis: session_id={} gt_present={} "
                    "challenge_present={} captcha_data_keys={}",
                    aigis.get("session_id"),
                    "gt" in captcha_data,
                    "challenge" in captcha_data,
                    list(captcha_data.keys()),
                )

                if captcha_data.get("gt") and captcha_data.get("challenge"):
                    return {
                        "source":      "aigis_header",
                        "gt":          captcha_data["gt"],
                        "challenge":   captcha_data["challenge"],
                        "new_captcha": captcha_data.get("new_captcha", 1),
                        "success":     aigis.get("success", 1),
                        "session_id":  aigis.get("session_id", ""),
                        "mmt_type":    aigis.get("mmt_type", 1),
                    }
                else:
                    logger.warning(
                        "[AUTH] x-rpc-aigis presente pero sin gt/challenge. "
                        "aigis completo: {}", aigis
                    )
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(
                    "[AUTH] Error parseando x-rpc-aigis: {} | raw: {!r}",
                    e, aigis_raw
                )

        # Intentar en resp["data"] (formato nuevo)
        data = resp.get("data") or {}
        if isinstance(data, dict):
            # Variante 1: data contiene gt/challenge directamente
            if data.get("gt") and data.get("challenge"):
                logger.debug("[AUTH] Captcha encontrado en resp['data'] directamente.")
                return {
                    "source":      "data_direct",
                    "gt":          data["gt"],
                    "challenge":   data["challenge"],
                    "new_captcha": data.get("new_captcha", 1),
                    "success":     data.get("success", 1),
                    "session_id":  data.get("session_id", ""),
                    "mmt_type":    data.get("mmt_type", 1),
                }

            # Variante 2: data["mmt_data"]
            mmt = data.get("mmt_data") or {}
            if isinstance(mmt, str):
                try:
                    mmt = json.loads(mmt)
                except json.JSONDecodeError:
                    pass
            if mmt.get("gt") and mmt.get("challenge"):
                logger.debug("[AUTH] Captcha encontrado en resp['data']['mmt_data'].")
                return {
                    "source":      "data_mmt",
                    "gt":          mmt["gt"],
                    "challenge":   mmt["challenge"],
                    "new_captcha": mmt.get("new_captcha", 1),
                    "success":     mmt.get("success", 1),
                    "session_id":  data.get("session_id", mmt.get("session_id", "")),
                    "mmt_type":    data.get("mmt_type", mmt.get("mmt_type", 1)),
                }

        # No se encontro en ninguna ubicacion conocida
        logger.error(
            "[AUTH] CAPTCHA DATA NO ENCONTRADO. "
            "retcode={} | x-rpc-aigis={!r} | data={} | "
            "Todos los headers: {}",
            resp.get("retcode"),
            aigis_raw,
            resp.get("data"),
            {k: v for k, v in resp.get("headers", {}).items()
             if any(kw in k.lower() for kw in ("rpc", "aigis", "verify", "cookie"))}
        )
        raise LoginError(
            f"No se encontraron datos del captcha Geetest en la respuesta de HoYoverse. "
            f"retcode={resp.get('retcode')} — Revisa el error_log para el detalle completo."
        )

    async def _handle_captcha_os(
        self, resp: dict, payload: dict, headers: dict
    ) -> dict:
        """Resuelve captcha OS y reintenta el login."""
        captcha = self._extract_captcha_data(resp, "os")
        logger.info(
            "[AUTH] Captcha OS encontrado via {}. "
            "Abriendo navegador...", captcha["source"]
        )

        mmt_result = await solve_geetest(
            SessionMMT(
                gt=captcha["gt"],
                challenge=captcha["challenge"],
                new_captcha=captcha["new_captcha"],
                success=captcha["success"],
                session_id=captcha["session_id"],
            ),
            lang=self._lang,
            api_server="api-na.geetest.com",
            port=self._captcha_port,
        )
        logger.debug("[AUTH] Captcha OS resuelto: {}", mmt_result)

        mmt_type = captcha.get("mmt_type", 1)
        headers["x-rpc-aigis"] = _build_aigis_header(mmt_result, mmt_type)
        logger.debug("[AUTH] x-rpc-aigis construido con mmt_type={}. Reintentando login OS...", mmt_type)
        return await self._raw_post(_WEB_LOGIN_URL, payload, headers)

    async def _handle_captcha_cn(
        self, resp: dict, payload: dict, headers: dict
    ) -> dict:
        """Resuelve captcha CN y reintenta el login."""
        captcha = self._extract_captcha_data(resp, "cn")
        logger.info(
            "[AUTH] Captcha CN encontrado via {}. "
            "Abriendo navegador...", captcha["source"]
        )

        mmt_result = await solve_geetest(
            SessionMMT(
                gt=captcha["gt"],
                challenge=captcha["challenge"],
                new_captcha=captcha["new_captcha"],
                success=captcha["success"],
                session_id=captcha["session_id"],
            ),
            lang="zh-cn",
            api_server="api.geetest.com",
            port=self._captcha_port,
        )
        logger.debug("[AUTH] Captcha CN resuelto: {}", mmt_result)

        mmt_type = captcha.get("mmt_type", 1)
        headers["x-rpc-aigis"] = _build_aigis_header(mmt_result, mmt_type)
        logger.debug("[AUTH] Reintentando login CN con mmt_type={}...", mmt_type)
        return await self._raw_post(_CN_WEB_LOGIN_URL, payload, headers)

    # ------------------------------------------------------------------
    # Manejo de verificacion de email
    # ------------------------------------------------------------------

    async def _handle_email_verification(
        self, resp: dict, payload: dict, headers: dict
    ) -> dict:
        """Gestiona el flujo de verificacion por email."""
        verify_raw = resp["headers"].get("x-rpc-verify", "")
        logger.debug(
            "[AUTH] Email verify: x-rpc-verify={!r} | data={}",
            verify_raw, resp.get("data")
        )

        try:
            action_ticket = json.loads(verify_raw) if verify_raw else {}
            if isinstance(action_ticket.get("verify_str"), str):
                action_ticket["verify_str"] = json.loads(action_ticket["verify_str"])
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(
                "[AUTH] Error parseando x-rpc-verify: {} | raw: {!r}",
                e, verify_raw
            )
            raise LoginError(f"No se pudo parsear la verificacion de email: {e}")

        logger.debug(
            "[AUTH] action_ticket keys: {} | verify_str keys: {}",
            list(action_ticket.keys()),
            list(action_ticket.get("verify_str", {}).keys())
        )

        try:
            ticket_str = action_ticket["verify_str"]["ticket"]
        except KeyError as e:
            logger.error(
                "[AUTH] Campo ticket no encontrado en verify_str. "
                "action_ticket completo: {}", action_ticket
            )
            raise LoginError(f"Estructura de verificacion de email inesperada: {e}")

        logger.info("[AUTH] Enviando email de verificacion...")
        await self._send_verification_email(ticket_str, headers=headers)

        code = await enter_code("Introduce el codigo de verificacion enviado a tu email: ")
        logger.debug("[AUTH] Codigo introducido por el usuario. Verificando...")

        await self._verify_email_code(
            code=code,
            action_ticket=action_ticket["risk_ticket"],
            verify_ticket=ticket_str,
        )
        logger.info("[AUTH] Codigo de email verificado correctamente.")

        headers["x-rpc-verify"] = json.dumps({
            "risk_ticket": action_ticket["risk_ticket"],
            "verify_str":  json.dumps(action_ticket["verify_str"]),
        })
        logger.debug("[AUTH] Reintentando login OS tras verificacion email...")
        return await self._raw_post(_WEB_LOGIN_URL, payload, headers)

    async def _send_verification_email(
        self, ticket: str, *, headers: typing.Optional[dict] = None
    ) -> None:
        send_headers = {**_EMAIL_SEND_HEADERS}
        if headers:
            for key in ("x-rpc-aigis", "x-rpc-device_id"):
                if key in headers:
                    send_headers[key] = headers[key]

        logger.debug("[AUTH] Enviando email de verificacion. ticket={}", ticket)
        resp = await self._raw_post(
            _SEND_VERIFICATION_URL,
            {"action_type": "verify_for_component", "action_ticket": ticket},
            send_headers,
        )
        _log_response("send_verification_email", resp)

        if resp["retcode"] != 0:
            logger.error(
                "[AUTH] Error al enviar email: retcode={} message={!r}",
                resp["retcode"], resp.get("message")
            )
            raise LoginError(
                f"No se pudo enviar el email de verificacion: "
                f"[{resp['retcode']}] {resp.get('message', '')}"
            )

    async def _verify_email_code(
        self, code: str, action_ticket: str, verify_ticket: str
    ) -> None:
        logger.debug("[AUTH] Verificando codigo de email...")
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
        _log_response("verify_email_code", resp)

        if resp["retcode"] != 0:
            logger.error(
                "[AUTH] Codigo de email incorrecto: retcode={} message={!r}",
                resp["retcode"], resp.get("message")
            )
            raise LoginError(
                f"Codigo incorrecto o caducado: "
                f"[{resp['retcode']}] {resp.get('message', '')}"
            )

    # ------------------------------------------------------------------
    # HTTP raw
    # ------------------------------------------------------------------

    async def _raw_post(self, url: str, payload: dict, headers: dict) -> dict:
        """
        POST sin autenticacion previa.
        Devuelve dict con retcode, message, data, headers y cookies.
        No lanza por retcode != 0 — el llamador decide.
        """
        logger.debug("[AUTH] _raw_post -> {}", url)
        try:
            async with aiohttp.ClientSession(
                cookie_jar=aiohttp.DummyCookieJar()
            ) as s:
                async with s.post(url, json=payload, headers=headers) as resp:
                    http_status = resp.status
                    body = await resp.json(content_type=None)
                    resp_cookies = {c.key: c.value for c in resp.cookies.values()}
                    resp_headers = dict(resp.headers)

            logger.debug(
                "[AUTH] _raw_post <- {} | HTTP={} retcode={} message={!r}",
                url, http_status,
                body.get("retcode"), body.get("message", "")
            )

            return {
                "retcode": body.get("retcode", 0),
                "message": body.get("message", ""),
                "data":    body.get("data"),
                "headers": {k.lower(): v for k, v in resp_headers.items()},
                "cookies": resp_cookies,
            }

        except aiohttp.ClientResponseError as e:
            logger.error(
                "[AUTH] HTTP error en _raw_post: status={} url={} msg={}",
                e.status, url, e.message
            )
            raise
        except aiohttp.ClientError as e:
            logger.error(
                "[AUTH] Error de red en _raw_post: url={} error={}",
                url, e
            )
            raise

    # ------------------------------------------------------------------
    # Validacion de sesion
    # ------------------------------------------------------------------

    async def _validate_session(self, session: HoyoSession) -> bool:
        """True si las cookies siguen siendo validas."""
        logger.debug(
            "[AUTH] Validando sesion. region={} account_id={}",
            session.region, session.account_id
        )
        try:
            accounts = await session.get_game_accounts()
            logger.debug(
                "[AUTH] Sesion valida. {} cuentas de juego encontradas.",
                len(accounts)
            )
            return True
        except HoyoAPIError as e:
            logger.debug(
                "[AUTH] Sesion invalida (HoyoAPIError): retcode={} msg={}",
                e.retcode, e.message
            )
            return False
        except aiohttp.ClientError as e:
            logger.debug("[AUTH] Sesion invalida (ClientError): {}", e)
            return False


# ---------------------------------------------------------------------------
# Utilidades de modulo
# ---------------------------------------------------------------------------

def _build_aigis_header(mmt_result: SessionMMTResult, mmt_type: int = 1) -> str:
    """
    Construye el valor del header x-rpc-aigis a partir del resultado del captcha.

    mmt_type debe coincidir con el mmt_type recibido en el aigis original
    (normalmente 1 para Geetest v3). HoYoverse rechaza la solucion si falta.
    """
    data = {
        "geetest_challenge": mmt_result["geetest_challenge"],
        "geetest_validate":  mmt_result["geetest_validate"],
        "geetest_seccode":   mmt_result["geetest_seccode"],
    }
    return json.dumps({
        "session_id": mmt_result["session_id"],
        "mmt_type":   mmt_type,
        "data":       json.dumps(data),
    })


def _raise_login_error(retcode: int, message: str) -> typing.NoReturn:
    """Convierte retcodes de HoYoverse en LoginError con mensajes descriptivos."""
    _MESSAGES: dict[int, str] = {
        -3001: "Cuenta o contrasena incorrectos.",
        -3002: "La cuenta no existe.",
        -3003: "Contrasena incorrecta.",
        -3004: "La cuenta esta bloqueada.",
        -3101: "Captcha requerido (OS) pero no fue resuelto.",
        -3102: "Captcha requerido (CN) pero no fue resuelto.",
        -3104: "Falta el header x-rpc-device_id en la peticion.",
        -3200: "Captcha requerido pero no fue resuelto.",
        -3206: "Captcha requerido pero no fue resuelto.",
        -3239: "Verificacion de email requerida pero no completada.",
    }
    raise LoginError(_MESSAGES.get(retcode, f"Error de login [{retcode}]: {message}"))
