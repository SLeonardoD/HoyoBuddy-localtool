"""
HoyoWrapper/auth_manager.py
---------------------------
Logica de autenticacion con HoYoLab / Miyoushe.

Logging exhaustivo via loguru para diagnostico en vivo:
  - Cada request HTTP loguea URL, retcode, headers relevantes y body.
  - Cada rama de flujo (captcha, email, cookies) loguea su estado.
  - Errores inesperados loguean el contexto completo.

Los valores que cambian con el tiempo (URLs, claves RSA, salts, retcodes
de captcha) viven en config.py — este archivo solo contiene logica.

No contiene logica de DB. CookieStore se recibe por inyeccion.
"""

from __future__ import annotations

import base64
import json
import random
import string
import typing
import uuid

import aiohttp
from loguru import logger

import config
from HoyoWrapper import ds_utils
from HoyoWrapper.timing import random_delay
from HoyoWrapper.captcha_server import (
    SessionMMT, SessionMMTResult, SessionMMTv4, SessionMMTv4Result,
    enter_code, solve_geetest, solve_geetest_v4,
)
from HoyoWrapper.session import HoyoAPIError, HoyoSession, LoginError

__all__ = ["AuthManager"]

# ---------------------------------------------------------------------------
# Alias locales cortos de config.py
# ---------------------------------------------------------------------------

_USER_AGENT = config.USER_AGENT

_WEB_LOGIN_URL = config.WEB_LOGIN_URL
_CN_WEB_LOGIN_URL = config.CN_WEB_LOGIN_URL
_SEND_VERIFICATION_URL = config.SEND_VERIFICATION_URL
_VERIFY_EMAIL_URL = config.VERIFY_EMAIL_URL
_COOKIE_V2_REFRESH_URL = config.COOKIE_V2_REFRESH_URL
_DS_SALT_APP_LOGIN = config.DS_SALT_APP_LOGIN

_RSA_KEY_1 = config.RSA_KEY_1
_RSA_KEY_2 = config.RSA_KEY_2

_WEB_LOGIN_HEADERS: dict[str, str] = {**config.WEB_LOGIN_HEADERS, "User-Agent": _USER_AGENT}
_CN_LOGIN_HEADERS: dict[str, str] = {**config.CN_LOGIN_HEADERS, "User-Agent": _USER_AGENT}
_EMAIL_SEND_HEADERS: dict[str, str] = {**config.EMAIL_SEND_HEADERS, "User-Agent": _USER_AGENT}
_EMAIL_VERIFY_HEADERS: dict[str, str] = {**_EMAIL_SEND_HEADERS}

_CAPTCHA_RETCODES_OS = config.CAPTCHA_RETCODES_OS
_CAPTCHA_RETCODES_CN = config.CAPTCHA_RETCODES_CN

# Login OS por app (el que entrega el stoken, ver config.OS_LOGIN_METHOD).
_APP_LOGIN_URL = config.APP_LOGIN_URL
_APP_LOGIN_HEADERS: dict[str, str] = {**config.APP_LOGIN_HEADERS, "User-Agent": _USER_AGENT}
_APP_EMAIL_SEND_HEADERS: dict[str, str] = {**config.APP_EMAIL_SEND_HEADERS, "User-Agent": _USER_AGENT}
_APP_EMAIL_VERIFY_HEADERS: dict[str, str] = {**_APP_EMAIL_SEND_HEADERS}

# Mensajes de errores de credenciales — usados para decidir si hacer fallback a CN
_MESSAGES_FOR_FALLBACK: dict[int, str] = {
    code: config.LOGIN_ERROR_MESSAGES[code]
    for code in config.CREDENTIAL_ERROR_RETCODES
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
        k: ("<oculto>" if "cookie" in k.lower() else v)
        for k, v in resp.get("headers", {}).items()
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
        _redact(resp.get("data")),
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
        captcha_port: int = config.CAPTCHA_PORT_DEFAULT,
        lang: str = config.CAPTCHA_LANG_OS,
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
        force_fresh: bool = False,
    ) -> HoyoSession:
        """
        Autentica y devuelve HoyoSession lista para operar.
        Intenta cookies guardadas primero. Si no hay o expiraron, hace login.

        force_fresh=True se salta las cookies guardadas y hace SIEMPRE un
        login real con la contrasena. Es lo que necesita "Reingresar
        contrasena": las cookies guardadas pueden pasar la validacion
        (ltoken_v2 sigue vivo) y aun asi tener el cookie_token_v2 vencido
        o ausente — sin esto, reingresar la contrasena no cambiaba nada.
        """
        logger.debug(
            "[AUTH] login() iniciado para email={} region={} force_fresh={}",
            email, region, force_fresh,
        )

        # 1. Intentar cookies guardadas (salvo que se pida login real)
        if force_fresh:
            regions_to_try: list = []
        else:
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

                    # Cuentas sin cookie_token_v2 (logueadas antes del fix de
                    # redeem) se reparan aca. Renovar el token ya existente
                    # es otro caso: lo hacen las tareas diarias y el canje
                    # (ver app.py), no el login interactivo.
                    if r == "os" and "cookie_token_v2" not in cookies:
                        await self.renew_cookie_token(email, r, cookies, device_id)
                    return session
                logger.info(
                    "[AUTH] Cookies expiradas para region={}. "
                    "Procediendo con login.", r
                )
            else:
                logger.debug("[AUTH] Sin cookies en store para region={}.", r)

        # 2. Login con la API
        # Si esta cuenta ya tuvo un dispositivo, se reutiliza: el login por
        # app exige un device_id CONSISTENTE (genshin.py: si cambia, HoYoverse
        # vuelve a pedir verificacion por email en cada login).
        known_device_id = self._known_device_id(email, region)
        login_extra = {"device_id": known_device_id} if known_device_id else {}
        detected: typing.Literal["os", "cn"] = region or "os"
        logger.debug("[AUTH] Intentando login directo. Region detectada: {}", detected)

        try:
            cookies, device_id = await self._do_login(
                email, password, region=detected, **login_extra,
            )
            final_region = detected
        except LoginError as e:
            # Solo hacer fallback a CN si:
            # 1. No se especifico region explicita
            # 2. El intento era OS
            # 3. El error es de credenciales (no de captcha ni de red)
            # Un error de captcha significa que OS reconocio el email — no tiene
            # sentido intentar CN, y ademas miyoushe.com no es accesible fuera de China.
            credential_errors = config.CREDENTIAL_ERROR_RETCODES
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
        if final_region == "os" and "cookie_token_v2" in cookies:
            self._record_token_issued(
                email, final_region, source=f"login_{config.OS_LOGIN_METHOD}"
            )

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
        device_id: typing.Optional[str] = None,
    ) -> tuple[dict[str, str], str]:
        logger.debug("[AUTH] _do_login region={} metodo_os={}", region, config.OS_LOGIN_METHOD)
        if region == "os":
            if config.OS_LOGIN_METHOD == "web":
                return await self._os_login_flow(email, password)
            return await self._os_app_login_flow(email, password, device_id=device_id)
        return await self._cn_login_flow(email, password)

    async def _os_app_login_flow(
        self, email: str, password: str, *, device_id: typing.Optional[str] = None,
    ) -> tuple[dict[str, str], str]:
        """
        Login OS por app (appLoginByPassword): el que entrega el STOKEN.

        Replica genshin.py (subclients/app.py: login_with_app_password).
        Con el stoken + el mid se generan ltoken_v2 y cookie_token_v2
        cuando haga falta (renew_cookie_token), sin volver a pedir la
        contrasena. Eso es lo que el login web no permite: no devuelve
        stoken, asi que su cookie_token_v2 vence sin forma de renovarlo.

        Orden (igual que genshin.py): login -> captcha (v4 o v3) ->
        verificacion por email (dispositivo nuevo) -> resultado.
        Despues del login se piden ltoken_v2 y cookie_token_v2 con el
        stoken, para que la sesion quede completa desde el primer dia.
        """
        device_id = device_id or _generate_app_device_id()
        logger.debug("[AUTH] _os_app_login_flow iniciado. device_id={}", device_id)

        payload = {
            "account":  _encrypt_credentials(email, 1),
            "password": _encrypt_credentials(password, 1),
        }
        headers = {**_APP_LOGIN_HEADERS, "x-rpc-device_id": device_id}

        resp = await self._app_login_post(payload, headers)
        _log_response("APP login inicial", resp)

        if resp["retcode"] == config.APP_LOGIN_CAPTCHA_RETCODE:
            resp = await self._handle_captcha_app(resp, payload, headers)
            _log_response("APP login post-captcha", resp)

        if resp["retcode"] == config.APP_LOGIN_EMAIL_VERIFY_RETCODE:
            resp = await self._handle_email_verification(resp, payload, headers, app=True)
            _log_response("APP login post-email", resp)

        if resp["retcode"] != 0:
            logger.error(
                "[AUTH] Login por app fallido: retcode={} message={!r}",
                resp["retcode"], resp.get("message"),
            )
            _raise_login_error(resp["retcode"], resp.get("message", ""))

        data = resp.get("data") or {}
        stoken = (data.get("token") or {}).get("token")
        user_info = data.get("user_info") or {}
        aid, mid = user_info.get("aid"), user_info.get("mid")
        if not (stoken and aid and mid):
            logger.error(
                "[AUTH] appLoginByPassword retcode=0 pero la respuesta no trae "
                "token/aid/mid. data={}", _redact(data),
            )
            raise LoginError(
                "El login por app no devolvio stoken/aid/mid. Si persiste, "
                "pone OS_LOGIN_METHOD = \"web\" en config.py."
            )

        cookies = {
            "stoken":         stoken,
            "ltuid_v2":       str(aid),
            "ltmid_v2":       mid,
            "account_id_v2":  str(aid),
            "account_mid_v2": mid,
        }

        # ANTI-BANEO: pausa entre el login y la request de los tokens.
        await random_delay(*config.DELAY_LOGIN_TO_COOKIE_EXCHANGE)
        extra = await self._fetch_cookie_token(cookies)
        if not extra.get("cookie_token_v2") or not extra.get("ltoken_v2"):
            raise LoginError(
                "El login por app funciono, pero no se pudieron obtener "
                "ltoken_v2 y cookie_token_v2 con el stoken (ver el log). "
                "Reintenta; si persiste, pone OS_LOGIN_METHOD = \"web\" en config.py."
            )
        cookies.update(extra)
        logger.info("[AUTH] Login por app OK. Cookies={}", sorted(cookies.keys()))
        return cookies, device_id

    async def _app_login_post(self, payload: dict, headers: dict) -> dict:
        """POST a appLoginByPassword con la firma ds (incluye el body, se recalcula cada vez)."""
        headers["ds"] = ds_utils.generate_app_login_ds(payload, config.DS_SALT_APP_LOGIN)
        return await self._raw_post(_APP_LOGIN_URL, payload, headers)

    async def _solve_app_captcha(self, resp: dict) -> str:
        """
        Resuelve el captcha que pidio una respuesta del flujo por app (v4 o
        v3) y devuelve el valor para el header x-rpc-aigis.

        IMPORTANTE: un captcha resuelto sirve para UNA sola request. Quien
        lo reciba no debe reutilizarlo en otra: HoYoverse lo rechaza
        (-3102 "Verification code failed" — 验证码 significa a la vez
        "codigo de verificacion" y "captcha").
        """
        aigis = _parse_app_aigis(resp)
        data = aigis["data"]

        if aigis["use_v4"]:
            logger.info("[AUTH] Captcha v4 (flujo por app). Abriendo navegador...")
            result = await solve_geetest_v4(
                SessionMMTv4(
                    captcha_id=data.get("captcha_id") or data["gt"],
                    risk_type=data.get("risk_type", ""),
                    new_captcha=data.get("new_captcha", 1),
                    success=data.get("success", 1),
                    session_id=aigis["session_id"],
                ),
                lang=self._lang,
                api_server=config.GEETEST_V4_API_SERVER_OS,
                port=self._captcha_port,
            )
            return _build_aigis_header_v4(result)

        logger.info("[AUTH] Captcha v3 (flujo por app). Abriendo navegador...")
        result = await solve_geetest(
            SessionMMT(
                gt=data["gt"],
                challenge=data["challenge"],
                new_captcha=data.get("new_captcha", 1),
                success=data.get("success", 1),
                session_id=aigis["session_id"],
            ),
            lang=self._lang,
            api_server=config.GEETEST_API_SERVER_OS,
            port=self._captcha_port,
        )
        return _build_aigis_header(result, aigis["mmt_type"])

    async def _handle_captcha_app(self, resp: dict, payload: dict, headers: dict) -> dict:
        """Resuelve el captcha del login por app y reintenta el login UNA vez."""
        headers["x-rpc-aigis"] = await self._solve_app_captcha(resp)
        logger.debug("[AUTH] Reintentando login por app tras el captcha...")
        return await self._app_login_post(payload, headers)

    async def _send_verification_email_app(self, ticket: str) -> None:
        """
        Envia el codigo de verificacion por email en el modo app.

        Replica genshin.py (subclients/app.py: _send_verification_email): las
        cabeceras son SOLO app_id y client_type. No lleva el x-rpc-aigis del
        login (ese captcha ya se consumio; reenviarlo produjo
        "-3102 Verification code failed" en vivo) ni el x-rpc-device_id. Si
        el propio endpoint pide un captcha (-3101) se resuelve uno NUEVO y se
        reenvia, una sola vez.
        """
        headers = {**_APP_EMAIL_SEND_HEADERS}
        payload = {"action_type": "verify_for_component", "action_ticket": ticket}

        for attempt in (1, 2):
            logger.debug(
                "[AUTH] Enviando email de verificacion (app). ticket={} intento={}",
                ticket, attempt,
            )
            resp = await self._raw_post(_SEND_VERIFICATION_URL, payload, headers)
            _log_response("send_verification_email (app)", resp)

            if resp["retcode"] == 0:
                return
            if resp["retcode"] == config.APP_LOGIN_CAPTCHA_RETCODE and attempt == 1:
                headers["x-rpc-aigis"] = await self._solve_app_captcha(resp)
                continue
            break

        logger.error(
            "[AUTH] Error al enviar email (app): retcode={} message={!r}",
            resp["retcode"], resp.get("message"),
        )
        raise LoginError(
            f"No se pudo enviar el email de verificacion: "
            f"[{resp['retcode']}] {resp.get('message', '')}"
        )

    # ------------------------------------------------------------------
    # Medicion de la vida util del cookie_token_v2
    # ------------------------------------------------------------------

    def _known_device_id(self, email: str, region: typing.Optional[str]) -> typing.Optional[str]:
        """device_id ya guardado para esta cuenta (de cualquier login previo), o None."""
        for r in ([region] if region else ["os", "cn"]):
            _, device_id = self._store.get(email, r)
            if device_id:
                return device_id
        return None

    def _record_token_issued(self, email: str, region: str, *, source: str) -> None:
        """
        Registra que se emitio un cookie_token_v2 (medicion). Guarda la edad
        del token ANTERIOR para ver cada cuanto se renueva. Si el store no
        soporta eventos, o algo falla, no hace nada: medir nunca debe
        tumbar un login ni una renovacion.
        """
        log_event = getattr(self._store, "log_session_event", None)
        if log_event is None:
            return
        try:
            hours_since = getattr(self._store, "hours_since_last_event", None)
            previous_age = hours_since(email, region, config.TOKEN_EVENT_ISSUED) if hours_since else None
            log_event(
                email, region, config.TOKEN_EVENT_ISSUED,
                detail=f"source={source}", age_hours=previous_age,
            )
            logger.info(
                "[MEDICION] cookie_token_v2 emitido para {} (fuente={}). "
                "Edad del token anterior: {}.", email, source,
                f"{previous_age:.1f} h" if previous_age is not None else "desconocida",
            )
        except Exception as e:  # noqa: BLE001 - la medicion jamas debe romper el flujo
            logger.warning("[MEDICION] No se pudo registrar el evento: {}", e)

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

        _log_cookie_lifetimes("login web OS", resp)
        cookies = resp["cookies"]
        logger.debug("[AUTH] Cookies OS recibidas: {}", list(cookies.keys()))

        if resp["data"] and resp["data"].get("stoken"):
            cookies["stoken"] = resp["data"]["stoken"]
            logger.debug("[AUTH] stoken añadido a cookies OS.")

            # cookie_token_v2 no llega en /webLoginByPassword — hay que
            # canjear el stoken (+ ltmid_v2) por el en un endpoint separado.
            # Sin esto redeem_code() falla con -1071 aunque check-in/mimo
            # funcionen perfecto (esos no lo necesitan).
            if "cookie_token_v2" not in cookies:
                # ANTI-BANEO: pausa entre el POST de login y la request
                # de intercambio de token — antes salian una detras de
                # la otra sin ninguna espera.
                await random_delay(*config.DELAY_LOGIN_TO_COOKIE_EXCHANGE)
                extra = await self._fetch_cookie_token(cookies)
                cookies.update(extra)

        if not cookies:
            logger.error(
                "[AUTH] Login OS retcode=0 pero sin cookies. "
                "data={} headers_cookies={}",
                resp.get("data"), resp.get("cookies")
            )
            raise LoginError("Login OS completado pero no se recibieron cookies.")

        logger.info("[AUTH] _os_login_flow completado. Cookies: {}", list(cookies.keys()))
        return cookies, device_id

    async def renew_cookie_token(
        self, email: str, region: str, cookies: dict, device_id: str,
    ) -> bool:
        """
        Pide un cookie_token_v2 NUEVO con el stoken y lo guarda.
        Devuelve True si se renovo, False si no se pudo (y por que queda
        en el log). No lanza excepciones: renovar es un intento de
        mantenimiento, nunca debe tumbar la operacion que lo disparo.

        POR QUE EXISTE: el canje de codigos exige cookie_token_v2 y ese
        token CADUCA. Antes solo se pedia cuando NO existia
        ("cookie_token_v2" not in cookies), asi que uno vencido se
        quedaba guardado para siempre y el canje fallaba con -1071
        ("Please log in to your account first") aunque check-in y mimo
        (que no lo usan) siguieran funcionando.

        Muta `cookies` IN PLACE a proposito: restore_session() le pasa a
        HoyoSession ese mismo dict, asi que la sesion viva y los juegos
        que ya la tienen ven el token nuevo sin reconstruir nada.

        Solo OS: CN no tiene este concepto (Miyoushe).
        """
        if region != "os":
            return False

        if not cookies.get("stoken") or not cookies.get("ltmid_v2"):
            logger.warning(
                "[AUTH] No se puede renovar cookie_token_v2 de {}: faltan "
                "{} en las cookies guardadas. Hace falta un login real "
                "('Reingresar contrasena').",
                email,
                " y ".join(k for k in ("stoken", "ltmid_v2") if not cookies.get(k)),
            )
            return False

        # ANTI-BANEO: pausa antes de la request, nunca pegada a la anterior.
        await random_delay(*config.DELAY_LOGIN_TO_COOKIE_EXCHANGE)
        extra = await self._fetch_cookie_token(cookies)
        if not extra:
            return False

        cookies.update(extra)
        self._store.set(email, region, cookies, device_id)
        logger.info("[AUTH] cookie_token_v2 renovado y guardado para {}.", email)
        self._record_token_issued(email, region, source="renewal")
        return True

    async def _fetch_cookie_token(
        self, cookies: dict[str, str]
    ) -> dict[str, str]:
        """
        Intercambia stoken (v2) + ltmid_v2 por cookie_token_v2 (y ltoken_v2).

        Replica fetch_cookie_with_stoken_v2() de genshin.py
        (genshin/client/manager/cookie.py), verificado leyendo el codigo
        fuente instalado via pip. Detalles NO obvios y criticos:
          - URL: COOKIE_V2_REFRESH_URL ("...hoyoverse.com/.../getBySToken"),
            NO el dominio hoyolab.com ni un endpoint estilo mihoyo.com/CN.
          - Metodo POST con body {"dst_token_types": [2, 4]} — NO son
            query params ni GET.
          - Requiere header 'ds' (dynamic secret) con el salt especifico
            DS_SALT["app_login"], generado SIN incluir el body en el hash
            (formato simple t,r,md5(salt&t&r) — distinto del ds usado en
            checkin/mimo que sí incluye body/query).
          - Requiere la cookie 'ltmid_v2' renombrada a 'mid' en el request
            (el endpoint no reconoce 'ltmid_v2', solo 'mid').
          - La respuesta trae una LISTA data.tokens, cada uno con
            token_type (2=ltoken_v2, 4=cookie_token_v2) y token (el valor).

        Necesario porque redeem_code() en genshin.py (via
        @managers.requires_cookie_token) exige cookie_token o
        cookie_token_v2 en las cookies. /webLoginByPassword NO lo entrega
        — solo ltoken_v2/ltuid_v2/stoken(/ltmid_v2). Sin este intercambio,
        redeem falla con retcode=-1071 "Please log in to your account
        first" aunque check-in/mimo funcionen perfecto (esos no lo
        necesitan).

        No lanza si falla: loguea el error y devuelve {} para no romper
        un login por lo demas exitoso. redeem_code seguira fallando en
        ese caso puntual, pero el resto del wrapper sigue operativo.
        """
        if "stoken" not in cookies:
            logger.error(
                "[AUTH] No se puede pedir cookie_token_v2: falta 'stoken' "
                "en las cookies del login."
            )
            return {}

        if "ltmid_v2" not in cookies:
            logger.error(
                "[AUTH] No se puede pedir cookie_token_v2: falta 'ltmid_v2' "
                "en las cookies devueltas por webLoginByPassword. Revisar "
                "el log '[AUTH] Cookies OS recibidas' para confirmar si "
                "HoYoverse dejo de mandar este campo — sin el, este "
                "intercambio no es posible y redeem_code seguira fallando."
            )
            return {}

        request_cookies = dict(cookies)
        request_cookies["mid"] = request_cookies["ltmid_v2"]

        headers = {
            "User-Agent":    _USER_AGENT,
            "ds":            ds_utils.generate_dynamic_secret(_DS_SALT_APP_LOGIN),
            "x-rpc-app_id":  "c9oqaq3s3gu8",
        }
        body = {"dst_token_types": [2, 4]}

        logger.debug("[AUTH] Solicitando cookie_token_v2 via getBySToken...")
        try:
            async with aiohttp.ClientSession(
                cookie_jar=aiohttp.DummyCookieJar()
            ) as s:
                async with s.post(
                    _COOKIE_V2_REFRESH_URL,
                    json=body,
                    headers=headers,
                    cookies=request_cookies,
                ) as resp:
                    data = await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            logger.error(
                "[AUTH] Error de red al pedir cookie_token_v2: {}", e
            )
            return {}

        retcode = data.get("retcode", 0)
        if retcode != 0:
            logger.error(
                "[AUTH] getBySToken fallo. retcode={} message={!r} — "
                "redeem_code no funcionara hasta resolver esto. "
                "Verificar salt DS_SALT['app_login'] y formato del ds "
                "contra genshin.py actualizado si el retcode indica "
                "firma invalida.",
                retcode, data.get("message", "")
            )
            return {}

        tokens = (data.get("data") or {}).get("tokens", [])
        for tok in tokens:
            # Medicion: cualquier campo que acompane al token (vencimiento,
            # ttl, etc.) queda en el log, sin el valor secreto del token.
            logger.info(
                "[MEDICION] getBySToken: token_type={} otros_campos={}",
                tok.get("token_type"),
                {k: v for k, v in tok.items() if k not in ("token", "token_type")},
            )
        result: dict[str, str] = {}
        for tok in tokens:
            if tok.get("token_type") == 2:
                result["ltoken_v2"] = tok["token"]
            elif tok.get("token_type") == 4:
                result["cookie_token_v2"] = tok["token"]

        if "cookie_token_v2" not in result:
            logger.error(
                "[AUTH] getBySToken retcode=0 pero sin token_type=4 "
                "(cookie_token_v2) en la respuesta. tokens={}", tokens
            )
            return {}

        logger.info(
            "[AUTH] cookie_token_v2 obtenido correctamente via getBySToken."
        )
        return result

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
            api_server=config.GEETEST_API_SERVER_OS,
            port=self._captcha_port,
        )
        logger.debug("[AUTH] Captcha OS resuelto: {}", mmt_result)

        mmt_type = captcha.get("mmt_type", 1)
        aigis_header = _build_aigis_header(mmt_result, mmt_type)
        headers["x-rpc-aigis"] = aigis_header
        logger.debug(
            "[AUTH] x-rpc-aigis construido con mmt_type={}: {}",
            mmt_type, aigis_header
        )
        logger.debug("[AUTH] Reintentando login OS...")
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
            lang=config.CAPTCHA_LANG_CN,
            api_server=config.GEETEST_API_SERVER_CN,
            port=self._captcha_port,
        )
        logger.debug("[AUTH] Captcha CN resuelto: {}", mmt_result)

        mmt_type = captcha.get("mmt_type", 1)
        aigis_header = _build_aigis_header(mmt_result, mmt_type)
        headers["x-rpc-aigis"] = aigis_header
        logger.debug(
            "[AUTH] x-rpc-aigis CN construido con mmt_type={}: {}",
            mmt_type, aigis_header
        )
        logger.debug("[AUTH] Reintentando login CN...")
        return await self._raw_post(_CN_WEB_LOGIN_URL, payload, headers)

    # ------------------------------------------------------------------
    # Manejo de verificacion de email
    # ------------------------------------------------------------------

    async def _handle_email_verification(
        self, resp: dict, payload: dict, headers: dict, *, app: bool = False,
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
        if app:
            await self._send_verification_email_app(ticket_str)
        else:
            await self._send_verification_email(
                ticket_str, headers=headers, base_headers=None,
            )

        code = await enter_code("Introduce el codigo de verificacion enviado a tu email: ")
        logger.debug("[AUTH] Codigo introducido por el usuario. Verificando...")

        await self._verify_email_code(
            code=code,
            action_ticket=action_ticket["risk_ticket"],
            verify_ticket=ticket_str,
            headers=_APP_EMAIL_VERIFY_HEADERS if app else None,
        )
        logger.info("[AUTH] Codigo de email verificado correctamente.")

        headers["x-rpc-verify"] = json.dumps({
            "risk_ticket": action_ticket["risk_ticket"],
            "verify_str":  json.dumps(action_ticket["verify_str"]),
        })
        logger.debug("[AUTH] Reintentando login OS tras verificacion email (app={})...", app)
        if app:
            # genshin.py reintenta con el ticket y SIN captcha: el del login ya
            # se consumio y reenviarlo hace que HoYoverse rechace la request.
            headers.pop("x-rpc-aigis", None)
            return await self._app_login_post(payload, headers)
        return await self._raw_post(_WEB_LOGIN_URL, payload, headers)

    async def _send_verification_email(
        self, ticket: str, *, headers: typing.Optional[dict] = None,
        base_headers: typing.Optional[dict] = None,
    ) -> None:
        send_headers = {**(base_headers or _EMAIL_SEND_HEADERS)}
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
        self, code: str, action_ticket: str, verify_ticket: str,
        headers: typing.Optional[dict] = None,
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
            headers or _EMAIL_VERIFY_HEADERS,
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
                    resp_cookie_lifetimes = {
                        c.key: {"max-age": c["max-age"], "expires": c["expires"]}
                        for c in resp.cookies.values()
                        if c["max-age"] or c["expires"]
                    }
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
                "cookie_lifetimes": resp_cookie_lifetimes,
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

    Formato correcto segun genshin.py/utility/auth.py:
        {session_id};{base64(json(mmt_data))}

    NO es JSON — es session_id en texto plano, punto y coma,
    y los datos del captcha codificados en base64.
    """
    import base64
    mmt_data = {
        "geetest_challenge": mmt_result["geetest_challenge"],
        "geetest_validate":  mmt_result["geetest_validate"],
        "geetest_seccode":   mmt_result["geetest_seccode"],
    }
    encoded = base64.b64encode(json.dumps(mmt_data).encode()).decode()
    return f"{mmt_result['session_id']};{encoded}"


def _raise_login_error(retcode: int, message: str) -> typing.NoReturn:
    """Convierte retcodes de HoYoverse en LoginError con mensajes descriptivos."""
    raise LoginError(
        config.LOGIN_ERROR_MESSAGES.get(retcode, f"Error de login [{retcode}]: {message}")
    )


# ---------------------------------------------------------------------------
# Login por app: utilidades
# ---------------------------------------------------------------------------

def _generate_app_device_id() -> str:
    """
    device_id del login por app: 16 caracteres alfanumericos en minuscula,
    como genshin.py (generate_app_device_id). NO es un UUID. Se guarda y
    se reutiliza en cada login de la misma cuenta.
    """
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=16))


def _parse_app_aigis(resp: dict) -> dict:
    """
    Lee el captcha que pide el login por app desde el header x-rpc-aigis.
    Devuelve {session_id, data, use_v4, mmt_type}. Lanza LoginError si el
    header falta o no tiene la forma esperada (nada de adivinar).
    """
    raw = (resp.get("headers") or {}).get("x-rpc-aigis", "")
    if not raw:
        raise LoginError(
            "HoYoverse pidio un captcha en el login por app pero no envio "
            "x-rpc-aigis. Revisa el error_log."
        )
    try:
        aigis = json.loads(raw)
        data = aigis.get("data")
        if isinstance(data, str):
            data = json.loads(data)
    except (json.JSONDecodeError, AttributeError) as e:
        raise LoginError(f"No se pudo leer el captcha del login por app: {e}") from e
    if not isinstance(data, dict) or not data.get("gt"):
        raise LoginError(
            "El captcha del login por app no trae el campo 'gt' esperado. "
            "Revisa el error_log."
        )
    return {
        "session_id": aigis.get("session_id", ""),
        "data":       data,
        "use_v4":     bool(data.get("use_v4")),
        "mmt_type":   aigis.get("mmt_type", 1),
    }


def _build_aigis_header_v4(result: SessionMMTv4Result) -> str:
    """
    x-rpc-aigis para un captcha v4: "session_id;base64(json(campos))".
    Mismo formato que v3 (genshin.py: get_aigis_header), pero con los 5
    campos de v4 y en este orden.
    """
    data = {
        key: result[key]
        for key in ("captcha_id", "lot_number", "pass_token", "gen_time", "captcha_output")
    }
    encoded = base64.b64encode(json.dumps(data).encode()).decode()
    return f"{result['session_id']};{encoded}"


_SECRET_KEY_WORDS = ("token", "password", "secret", "cookie")


def _redact(obj: typing.Any, _depth: int = 0) -> typing.Any:
    """
    Copia de `obj` apta para logs: oculta el VALOR de cualquier campo de
    texto cuyo nombre sugiera un secreto (token, stoken, password, cookie).
    Hace falta porque el login por app devuelve el stoken en claro dentro de
    `data`, y los logs se comparten para diagnosticar.
    """
    if _depth > 6:
        return "<...>"
    if isinstance(obj, dict):
        return {
            k: (f"<oculto len={len(v)}>"
                if isinstance(v, str) and any(w in str(k).lower() for w in _SECRET_KEY_WORDS)
                else _redact(v, _depth + 1))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v, _depth + 1) for v in obj]
    return obj


def _log_cookie_lifetimes(label: str, resp: dict) -> None:
    """
    Medicion: que vencimiento DECLARA el servidor para cada cookie (Max-Age
    / Expires del Set-Cookie). Una cookie sin ninguno es "de sesion": no
    declara cuanto dura, y solo la medicion en uso real lo dice.
    """
    lifetimes = resp.get("cookie_lifetimes") or {}
    if lifetimes:
        logger.info("[MEDICION] {}: vencimiento declarado -> {}", label, lifetimes)
    else:
        logger.info(
            "[MEDICION] {}: ninguna cookie declara vencimiento (sin Max-Age/Expires).",
            label,
        )
