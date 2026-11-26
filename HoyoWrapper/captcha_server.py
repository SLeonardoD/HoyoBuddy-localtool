"""
HoyoWrapper/captcha_server.py
------------------------------
Servidor web local temporal para resolver captchas Geetest de HoYoverse.

Flujo:
  1. Levanta un servidor HTTP en localhost (por defecto puerto 5000).
  2. Abre el navegador del usuario automaticamente.
  3. Sirve la pagina con el widget Geetest v3.
  4. Espera a que el usuario (o el widget en modo silencioso) lo resuelva.
  5. Recibe el resultado via POST /send-data.
  6. Apaga el servidor y devuelve el resultado al flujo de auth.

Logging exhaustivo via loguru:
  - Cada evento del ciclo de vida del servidor queda registrado.
  - El resultado completo del captcha (challenge, validate, seccode)
    se loguea en DEBUG para diagnosticar rechazos del servidor de HoYoverse.
"""

from __future__ import annotations

import asyncio
import random
import typing
import webbrowser

import aiohttp
from aiohttp import web
from loguru import logger

import config

__all__ = [
    "SessionMMT", "SessionMMTResult", "SessionMMTv4", "SessionMMTv4Result",
    "solve_geetest", "solve_geetest_v4", "enter_code",
]


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------

class SessionMMT(typing.TypedDict):
    """Datos del captcha Geetest v3 devueltos por HoYoverse."""
    gt: str
    challenge: str
    new_captcha: int
    success: int
    session_id: str


class SessionMMTResult(typing.TypedDict):
    """Resultado del captcha Geetest v3 resuelto por el usuario."""
    geetest_challenge: str
    geetest_validate: str
    geetest_seccode: str
    session_id: str


class SessionMMTv4(typing.TypedDict):
    """Datos del captcha Geetest v4 (login por app) devueltos por HoYoverse."""
    captcha_id: str
    risk_type: str
    new_captcha: int
    success: int
    session_id: str


class SessionMMTv4Result(typing.TypedDict):
    """Resultado del captcha Geetest v4. El orden de los campos importa:
    es el orden en que se serializan dentro del header x-rpc-aigis."""
    captcha_id: str
    lot_number: str
    pass_token: str
    gen_time: str
    captcha_output: str
    session_id: str


# ---------------------------------------------------------------------------
# Pagina HTML con el widget Geetest
# ---------------------------------------------------------------------------

_CAPTCHA_PAGE = """<!DOCTYPE html>
<head>
  <meta charset="utf-8"/>
  <meta name="referrer" content="no-referrer"/>
  <style>
    body {{
      font-family: sans-serif;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      margin: 0;
      background: #1a1a2e;
      color: #eee;
    }}
    h2 {{ margin-bottom: 24px; }}
    #status {{ margin-top: 20px; font-size: 14px; color: #aaa; }}
  </style>
</head>
<html>
  <body>
    <h2>HoYoverse &mdash; verificacion de seguridad</h2>
    <div id="captcha-container"></div>
    <p id="status">Cargando captcha...</p>
  </body>
  <script src="./gt/v3.js"></script>
  <script>
    fetch("/mmt")
      .then(r => r.json())
      .then(mmt => {{
        document.getElementById("status").textContent = "Resuelve el captcha para continuar.";
        initGeetest({{
          gt: mmt.gt,
          challenge: mmt.challenge,
          new_captcha: mmt.new_captcha,
          api_server: "{api_server}",
          https: /^https/i.test(window.location.protocol),
          product: "bind",
          lang: "{lang}",
        }}, captcha => {{
          captcha.onReady(() => {{
            console.log("[Captcha] Widget listo. Iniciando verificacion...");
            captcha.verify();
          }});
          captcha.onSuccess(() => {{
            const result = {{
              session_id: mmt.session_id,
              ...captcha.getValidate()
            }};
            console.log("[Captcha] Resuelto:", JSON.stringify(result));
            fetch("/send-data", {{
              method: "POST",
              headers: {{"Content-Type": "application/json"}},
              body: JSON.stringify(result),
            }}).then(r => {{
              console.log("[Captcha] Resultado enviado al servidor. Status:", r.status);
            }});
            document.getElementById("status").textContent =
              "Captcha resuelto. Puedes cerrar esta ventana.";
          }});
          captcha.onError((err) => {{
            console.error("[Captcha] Error en el widget:", err);
            document.getElementById("status").textContent =
              "Error en el captcha: " + JSON.stringify(err);
          }});
        }});
      }})
      .catch(err => {{
        console.error("[Captcha] Error cargando MMT:", err);
        document.getElementById("status").textContent = "Error cargando el captcha.";
      }});
  </script>
</html>"""

_GT_V3_URL = config.GEETEST_JS_URL
_DEFAULT_API_SERVER = config.GEETEST_API_SERVER_OS


# ---------------------------------------------------------------------------
# Servidor web temporal
# ---------------------------------------------------------------------------

async def _launch_webapp(
    mmt: SessionMMT,
    *,
    lang: str = "en",
    api_server: str = _DEFAULT_API_SERVER,
    port: int = 5000,
) -> SessionMMTResult:
    """
    Levanta un servidor HTTP local, abre el browser y espera el resultado
    del captcha Geetest.

    Devuelve SessionMMTResult si se resuelve antes de 5 minutos.
    Lanza RuntimeError si se agota el tiempo.
    """
    logger.debug(
        "[CAPTCHA] Iniciando servidor local. "
        "puerto={} api_server={} lang={} gt={} challenge={}",
        port, api_server, lang,
        mmt.get("gt", "?"),
        mmt.get("challenge", "?"),
    )

    loop = asyncio.get_running_loop()
    future: asyncio.Future[SessionMMTResult] = loop.create_future()

    router = web.RouteTableDef()

    @router.get("/")
    async def index(request: web.Request) -> web.Response:
        logger.debug("[CAPTCHA] GET / — sirviendo pagina HTML del captcha.")
        body = _CAPTCHA_PAGE.format(api_server=api_server, lang=lang)
        return web.Response(
            body=body.encode("utf-8"),
            content_type="text/html",
            charset="utf-8",
        )

    @router.get("/gt/v3.js")
    async def gt_script(request: web.Request) -> web.Response:
        """Sirve el JS de Geetest desde su CDN para evitar bloqueos CORS."""
        logger.debug("[CAPTCHA] GET /gt/v3.js — descargando script de Geetest CDN.")
        try:
            async with aiohttp.ClientSession() as http_session:
                async with http_session.get(_GT_V3_URL) as r:
                    content = await r.read()
            logger.debug(
                "[CAPTCHA] Script Geetest descargado. Tamano={} bytes.", len(content)
            )
            return web.Response(body=content, content_type="text/javascript")
        except Exception as e:
            logger.error("[CAPTCHA] Error descargando script Geetest: {}", e)
            return web.Response(status=500, text=f"Error descargando Geetest: {e}")

    @router.get("/mmt")
    async def mmt_endpoint(request: web.Request) -> web.Response:
        """Devuelve los datos del captcha al frontend."""
        mmt_data = dict(mmt)
        logger.debug(
            "[CAPTCHA] GET /mmt — enviando datos al widget: "
            "gt={} challenge={} new_captcha={} success={} session_id={}",
            mmt_data.get("gt"),
            mmt_data.get("challenge"),
            mmt_data.get("new_captcha"),
            mmt_data.get("success"),
            mmt_data.get("session_id"),
        )
        return web.json_response(mmt_data)

    @router.post("/send-data")
    async def send_data(request: web.Request) -> web.Response:
        """Recibe el resultado del captcha resuelto."""
        try:
            data = await request.json()
            logger.debug(
                "[CAPTCHA] POST /send-data recibido: "
                "session_id={} geetest_challenge={} "
                "geetest_validate={} geetest_seccode={}",
                data.get("session_id"),
                data.get("geetest_challenge"),
                data.get("geetest_validate"),
                data.get("geetest_seccode"),
            )

            # Validar que los campos requeridos esten presentes
            required = ("geetest_challenge", "geetest_validate",
                        "geetest_seccode", "session_id")
            missing = [f for f in required if not data.get(f)]
            if missing:
                logger.warning(
                    "[CAPTCHA] Resultado incompleto. Campos faltantes: {}", missing
                )

            if not future.done():
                # Delay con distribucion uniforme entre valores con decimales
                # para simular tiempo de reaccion humano real (3-5s).
                # Se usa random.uniform para obtener valores como 3.2s, 4.7s, etc.
                import random
                delay = round(random.uniform(*config.DELAY_CAPTCHA_HUMAN), 2)
                logger.debug(
                    "[CAPTCHA] Aplicando delay humano de {}s antes de resolver...",
                    delay
                )
                await asyncio.sleep(delay)

                result = SessionMMTResult(
                    geetest_challenge=data.get("geetest_challenge", ""),
                    geetest_validate=data.get("geetest_validate", ""),
                    geetest_seccode=data.get("geetest_seccode", ""),
                    session_id=data.get("session_id", mmt.get("session_id", "")),
                )
                logger.info(
                    "[CAPTCHA] Resultado aceptado y enviado al flujo de auth."
                )
                future.set_result(result)
            else:
                logger.warning(
                    "[CAPTCHA] POST /send-data recibido pero el future ya estaba done. "
                    "Resultado ignorado."
                )

            return web.Response(status=204)

        except Exception as e:
            logger.error("[CAPTCHA] Error procesando /send-data: {}", e)
            return web.Response(status=500)

    app = web.Application()
    app.add_routes(router)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="localhost", port=port)
    await site.start()

    logger.info(
        "[CAPTCHA] Servidor iniciado en http://localhost:{}. "
        "Abriendo navegador...", port
    )
    webbrowser.open_new_tab(f"http://localhost:{port}")

    try:
        result = await asyncio.wait_for(future, timeout=300)
        logger.info(
            "[CAPTCHA] Captcha resuelto exitosamente. "
            "challenge={} validate={} seccode={}",
            result["geetest_challenge"],
            result["geetest_validate"],
            result["geetest_seccode"],
        )
        return result

    except asyncio.TimeoutError:
        logger.error("[CAPTCHA] Timeout: el captcha no fue resuelto en 5 minutos.")
        raise RuntimeError(
            "El captcha no fue resuelto en 5 minutos. Intenta de nuevo."
        )
    finally:
        logger.debug("[CAPTCHA] Apagando servidor local...")
        await asyncio.sleep(0.3)
        await runner.shutdown()
        await runner.cleanup()
        logger.debug("[CAPTCHA] Servidor apagado.")


# ---------------------------------------------------------------------------
# Funciones publicas
# ---------------------------------------------------------------------------

async def solve_geetest(
    mmt: SessionMMT,
    *,
    lang: str = "en",
    api_server: str = _DEFAULT_API_SERVER,
    port: int = 5000,
) -> SessionMMTResult:
    """
    Resuelve un captcha Geetest abriendo el navegador del usuario.

    Parametros:
        mmt         Datos del captcha devueltos por HoYoverse.
        lang        Idioma del widget ('en', 'zh-cn', 'es', ...).
        api_server  Servidor Geetest. Por defecto 'api-na.geetest.com'.
                    Para CN usar 'api.geetest.com'.
        port        Puerto local del servidor temporal.

    Lanza:
        RuntimeError  Si el captcha no se resuelve en 5 minutos.
    """
    logger.debug(
        "[CAPTCHA] solve_geetest() llamado. "
        "lang={} api_server={} port={}",
        lang, api_server, port
    )
    return await _launch_webapp(mmt, lang=lang, api_server=api_server, port=port)


class CodeProvider(typing.Protocol):
    """
    Contrato de quien le pide al usuario el codigo de verificacion del email
    durante un login. La consola (`enter_code`) es la implementacion por
    defecto; una interfaz distinta (ej. web) inyecta la suya via
    AuthManager / HoyoManager(code_provider=...).

    prompt           texto a mostrar; ya trae las instrucciones para una consola.
    resend_keyword   si no es None, el login admite reenviar el codigo: el
                     proveedor debe devolver exactamente ese texto cuando el
                     usuario pide reenviar. None = no se ofrece reenvio.

    Devuelve lo que el usuario escribio. Una cadena vacia (o None) significa
    "cancelar"; la logica de login decide que hacer con cada respuesta.
    """

    async def __call__(
        self, prompt: str, *, resend_keyword: typing.Optional[str] = None,
    ) -> typing.Optional[str]: ...


async def enter_code(
    prompt: str = "Introduce el codigo de verificacion: ",
    *,
    resend_keyword: typing.Optional[str] = None,
) -> str:
    """
    Solicita al usuario un codigo de verificacion por consola (CodeProvider por
    defecto). Es async para no bloquear el event loop.

    `resend_keyword` se acepta por contrato pero aqui no hace falta: el prompt
    de consola ya explica como reenviar.
    """
    logger.debug("[CAPTCHA] Esperando codigo de verificacion del usuario...")
    code = await asyncio.to_thread(input, prompt)
    logger.debug("[CAPTCHA] Codigo introducido (longitud={}).", len(code))
    return code


# ===========================================================================
# Geetest v4 (captcha del login por app)
#
# Servidor aparte del v3 a proposito: el v3 ya esta validado en vivo y no
# se toca. Se porta el comportamiento de genshin.py (server.py) para v4:
# initGeetest4 con captchaId/riskType/userInfo={session_id} y el servidor
# de la app de HoYoLab (config.GEETEST_V4_API_SERVER_OS).
# ===========================================================================

_V4_RESULT_FIELDS = ("captcha_id", "lot_number", "pass_token", "gen_time", "captcha_output")

_CAPTCHA_PAGE_V4 = """<!DOCTYPE html>
<head>
  <meta charset="utf-8"/>
  <meta name="referrer" content="no-referrer"/>
  <style>
    body { font-family: sans-serif; display: flex; flex-direction: column;
           align-items: center; justify-content: center; min-height: 100vh;
           margin: 0; background: #1a1a2e; color: #eee; }
    #status { margin-top: 20px; font-size: 14px; color: #aaa; }
  </style>
</head>
<html>
  <body>
    <h2>HoYoverse &mdash; verificacion de seguridad</h2>
    <p id="status">Cargando captcha...</p>
  </body>
  <script src="./gt/v4.js"></script>
  <script>
    const status = (t) => { document.getElementById("status").textContent = t; };
    fetch("/mmt")
      .then((r) => r.json())
      .then((mmt) => {
        initGeetest4({
          captchaId: mmt.captcha_id,
          riskType: mmt.risk_type,
          userInfo: JSON.stringify({ session_id: mmt.session_id }),
          apiServers: ["__API_SERVER__"],
          product: "bind",
          language: "__LANG__",
        }, (captcha) => {
          captcha.onReady(() => {
            status("Resuelve el captcha para continuar.");
            captcha.showCaptcha();
          });
          captcha.onSuccess(() => {
            fetch("/send-data", {
              method: "POST",
              body: JSON.stringify({ session_id: mmt.session_id, ...captcha.getValidate() }),
            });
            status("Captcha resuelto. Puedes cerrar esta ventana.");
          });
          captcha.onError((err) => {
            console.error("[Captcha v4] Error en el widget:", err);
            status("Error en el captcha: " + JSON.stringify(err));
          });
        });
      })
      .catch((err) => {
        console.error("[Captcha v4] Error cargando MMT:", err);
        status("Error cargando el captcha.");
      });
  </script>
</html>"""


async def _launch_webapp_v4(
    mmt: SessionMMTv4,
    *,
    lang: str = "en",
    api_server: str = config.GEETEST_V4_API_SERVER_OS,
    port: int = 5000,
) -> SessionMMTv4Result:
    """
    Levanta un servidor local, abre el navegador y espera el resultado del
    captcha Geetest v4. Devuelve SessionMMTv4Result.

    A diferencia del v3, un resultado INCOMPLETO falla de inmediato
    (RuntimeError) en vez de continuar con campos vacios: un aigis armado
    con datos incompletos solo produce otro -3101 confuso del servidor.
    Lanza RuntimeError tambien si se agota el tiempo (5 minutos).
    """
    logger.debug(
        "[CAPTCHA-V4] Iniciando servidor local. puerto={} api_server={} lang={} "
        "captcha_id={} risk_type={}",
        port, api_server, lang, mmt.get("captcha_id"), mmt.get("risk_type"),
    )

    loop = asyncio.get_running_loop()
    future: asyncio.Future[SessionMMTv4Result] = loop.create_future()
    router = web.RouteTableDef()

    @router.get("/")
    async def index(request: web.Request) -> web.Response:
        body = _CAPTCHA_PAGE_V4.replace("__API_SERVER__", api_server).replace("__LANG__", lang)
        return web.Response(body=body.encode("utf-8"), content_type="text/html", charset="utf-8")

    @router.get("/gt/v4.js")
    async def gt_script(request: web.Request) -> web.Response:
        """Sirve gt4.js desde el CDN de Geetest para evitar bloqueos CORS."""
        try:
            async with aiohttp.ClientSession() as http_session:
                async with http_session.get(config.GEETEST_V4_JS_URL) as r:
                    content = await r.read()
            return web.Response(body=content, content_type="text/javascript")
        except Exception as e:
            logger.error("[CAPTCHA-V4] Error descargando gt4.js: {}", e)
            return web.Response(status=500, text=f"Error descargando Geetest v4: {e}")

    @router.get("/mmt")
    async def mmt_endpoint(request: web.Request) -> web.Response:
        return web.json_response(dict(mmt))

    @router.post("/send-data")
    async def send_data(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception as e:
            logger.error("[CAPTCHA-V4] /send-data con cuerpo invalido: {}", e)
            if not future.done():
                future.set_exception(RuntimeError(f"Resultado de captcha v4 ilegible: {e}"))
            return web.Response(status=400)

        required = (*_V4_RESULT_FIELDS, "session_id")
        missing = [f for f in required if not data.get(f)]
        if missing:
            logger.error("[CAPTCHA-V4] Resultado incompleto. Faltan: {}", missing)
            if not future.done():
                future.set_exception(
                    RuntimeError(f"Resultado de captcha v4 incompleto, faltan: {missing}")
                )
            return web.Response(status=400)

        if not future.done():
            # Delay humano antes de devolver el resultado (igual que el v3).
            delay = round(random.uniform(*config.DELAY_CAPTCHA_HUMAN), 2)
            logger.debug("[CAPTCHA-V4] Aplicando delay humano de {}s...", delay)
            await asyncio.sleep(delay)
            future.set_result(SessionMMTv4Result(
                captcha_id=data["captcha_id"],
                lot_number=data["lot_number"],
                pass_token=data["pass_token"],
                gen_time=data["gen_time"],
                captcha_output=data["captcha_output"],
                session_id=data["session_id"],
            ))
            logger.info("[CAPTCHA-V4] Resultado aceptado.")
        return web.Response(status=204)

    app = web.Application()
    app.add_routes(router)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="localhost", port=port)
    await site.start()

    logger.info("[CAPTCHA-V4] Servidor iniciado en http://localhost:{}. Abriendo navegador...", port)
    webbrowser.open_new_tab(f"http://localhost:{port}")

    try:
        return await asyncio.wait_for(future, timeout=300)
    except asyncio.TimeoutError:
        logger.error("[CAPTCHA-V4] Timeout: el captcha no fue resuelto en 5 minutos.")
        raise RuntimeError("El captcha no fue resuelto en 5 minutos. Intenta de nuevo.")
    finally:
        await asyncio.sleep(0.3)
        await runner.shutdown()
        await runner.cleanup()
        logger.debug("[CAPTCHA-V4] Servidor apagado.")


async def solve_geetest_v4(
    mmt: SessionMMTv4,
    *,
    lang: str = "en",
    api_server: str = config.GEETEST_V4_API_SERVER_OS,
    port: int = 5000,
) -> SessionMMTv4Result:
    """Resuelve un captcha Geetest v4 abriendo el navegador del usuario."""
    return await _launch_webapp_v4(mmt, lang=lang, api_server=api_server, port=port)

