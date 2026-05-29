"""
captcha_server.py
-----------------
Servidor web local temporal para resolver captchas Geetest de HoYoverse.

Cuando el login con email+contraseña activa un captcha, este módulo:
1. Levanta un servidor HTTP en localhost (por defecto puerto 5000)
2. Abre el navegador del usuario automáticamente
3. Sirve la página con el widget Geetest
4. Espera a que el usuario lo resuelva
5. Recibe el resultado y apaga el servidor
6. Devuelve los datos del captcha resuelto al flujo de auth

Copiado y simplificado de genshin.py/genshin/client/components/auth/server.py
Solo se mantiene el flujo relevante para login web (SessionMMT / SessionMMTResult).

Correcciones respecto a la versión anterior:
  - Bug 4: asyncio.get_event_loop() reemplazado por asyncio.get_running_loop()
            compatible con Python 3.10+ y 3.12+.
  - solve_geetest() ahora es async (era sync devolviendo una coroutine,
    lo que causaría warnings en algunas versiones de Python).
"""

from __future__ import annotations

import asyncio
import typing
import webbrowser

import aiohttp
from aiohttp import web

__all__ = ["SessionMMT", "SessionMMTResult", "solve_geetest", "enter_code"]


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


# ---------------------------------------------------------------------------
# Página HTML con el widget Geetest
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
    <h2>HoYoverse — verificación de seguridad</h2>
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
          captcha.onReady(() => captcha.verify());
          captcha.onSuccess(() => {{
            const result = {{
              session_id: mmt.session_id,
              ...captcha.getValidate()
            }};
            fetch("/send-data", {{
              method: "POST",
              headers: {{"Content-Type": "application/json"}},
              body: JSON.stringify(result),
            }});
            document.getElementById("status").textContent =
              "Captcha resuelto. Puedes cerrar esta ventana.";
          }});
        }});
      }});
  </script>
</html>"""

_GT_V3_URL = "https://static.geetest.com/static/js/gt.0.5.0.js"
_DEFAULT_API_SERVER = "api-na.geetest.com"


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
    Levanta un servidor HTTP local, abre el browser y espera a que el usuario
    resuelva el captcha Geetest.

    Devuelve SessionMMTResult si el usuario lo resuelve antes de 5 minutos,
    o lanza RuntimeError si se agota el tiempo.
    """
    # FIX Bug 4: get_running_loop() en lugar del deprecado get_event_loop().
    # Siempre hay un loop activo aquí porque esta función es async.
    loop = asyncio.get_running_loop()
    future: asyncio.Future[SessionMMTResult] = loop.create_future()

    router = web.RouteTableDef()

    @router.get("/")
    async def index(request: web.Request) -> web.Response:
        body = _CAPTCHA_PAGE.format(api_server=api_server, lang=lang)
        return web.Response(body=body, content_type="text/html")

    @router.get("/gt/v3.js")
    async def gt_script(request: web.Request) -> web.Response:
        """Sirve el JS de Geetest desde su CDN para evitar bloqueos CORS."""
        async with aiohttp.ClientSession() as http_session:
            async with http_session.get(_GT_V3_URL) as r:
                content = await r.read()
        return web.Response(body=content, content_type="text/javascript")

    @router.get("/mmt")
    async def mmt_endpoint(request: web.Request) -> web.Response:
        """Devuelve los datos del captcha al frontend."""
        return web.json_response(dict(mmt))

    @router.post("/send-data")
    async def send_data(request: web.Request) -> web.Response:
        """Recibe el resultado del captcha resuelto por el usuario."""
        data = await request.json()
        if not future.done():
            future.set_result(SessionMMTResult(**data))
        return web.Response(status=204)

    app = web.Application()
    app.add_routes(router)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="localhost", port=port)
    await site.start()

    print(f"\n[captcha] Abriendo http://localhost:{port} en el navegador...")
    print("[captcha] Resuelve el captcha para continuar.\n")
    webbrowser.open_new_tab(f"http://localhost:{port}")

    try:
        result = await asyncio.wait_for(future, timeout=300)
    except asyncio.TimeoutError:
        raise RuntimeError(
            "El captcha no fue resuelto en 5 minutos. Intenta de nuevo."
        )
    finally:
        # Pequeña pausa para que el browser reciba la respuesta 204
        # antes de que el servidor se apague.
        await asyncio.sleep(0.3)
        await runner.shutdown()
        await runner.cleanup()

    return result


async def solve_geetest(
    mmt: SessionMMT,
    *,
    lang: str = "en",
    api_server: str = _DEFAULT_API_SERVER,
    port: int = 5000,
) -> SessionMMTResult:
    """
    Resuelve un captcha Geetest abriendo el navegador del usuario.

    Uso:
        mmt_result = await solve_geetest(mmt_data)

    Parámetros:
        mmt         Datos del captcha devueltos por HoYoverse (SessionMMT).
        lang        Idioma del widget ('en', 'zh-cn', 'es', ...).
        api_server  Servidor Geetest. Por defecto 'api-na.geetest.com' (global).
                    Para CN usar 'api.geetest.com'.
        port        Puerto local del servidor temporal. Por defecto 5000.

    Lanza:
        RuntimeError  Si el usuario no resuelve el captcha en 5 minutos.
    """
    return await _launch_webapp(mmt, lang=lang, api_server=api_server, port=port)


async def enter_code(prompt: str = "Introduce el código de verificación: ") -> str:
    """
    Solicita al usuario un código de verificación (email/SMS) por consola.

    Es async para no bloquear el event loop mientras espera input del usuario.
    """
    return await asyncio.to_thread(input, prompt)
