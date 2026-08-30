"""
config.py
---------
Valores centralizados de HoYoWrapper: rutas, URLs, IDs de actividad,
salts de firma, claves RSA y parametros anti-deteccion.

Por que existe este archivo:
    HoYoverse cambia estos valores sin aviso (nueva temporada -> nuevo
    act_id, reorganizacion de endpoints -> nueva URL, etc.). Tenerlos
    todos aca, en vez de repartidos entre game_model.py / auth_manager.py
    / captcha_server.py / app.py, permite corregir una temporada nueva o
    un endpoint movido editando UNA linea en UN archivo, sin tocar logica.

Cuando algo se rompe en vivo (retcode desconocido, HTTP 404, firma
invalida), el primer lugar a revisar es este archivo, comparando contra
la fuente de verdad: `pip install genshin` y leer genshin/client/routes.py
y genshin/constants.py de la version instalada.

Este archivo NO debe importar nada de HoyoWrapper/ (evita imports
circulares) — solo constantes y estructuras de datos simples.
"""

from __future__ import annotations

# =============================================================================
# Persistencia
# =============================================================================

# Ruta a la base de datos SQLite (CookieStore + AccountStore comparten archivo).
# Antes hardcodeado como default en cada CookieStore(...)/AccountStore(...) y
# duplicado en main.py. Ahora se importa desde aca en los tres lugares.
DB_PATH = "./hoyo_auth.db"


# =============================================================================
# HTTP generico
# =============================================================================

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# =============================================================================
# Login — URLs
# Fuente: genshin.py routes.py (WEB_LOGIN_URL, CN_WEB_LOGIN_URL,
# SEND_VERIFICATION_CODE_URL, VERIFY_EMAIL_URL, COOKIE_V2_REFRESH_URL).
# =============================================================================

WEB_LOGIN_URL = (
    "https://sg-public-api.hoyolab.com/account/ma-passport/api/webLoginByPassword"
)
CN_WEB_LOGIN_URL = (
    "https://passport-api.miyoushe.com/account/ma-cn-passport/web/loginByPassword"
)
SEND_VERIFICATION_URL = (
    "https://sg-public-api.hoyoverse.com/account/ma-verifier/api"
    "/createEmailCaptchaByActionTicket"
)
VERIFY_EMAIL_URL = (
    "https://sg-public-api.hoyoverse.com/account/ma-verifier/api"
    "/verifyActionTicketPartly"
)

# Intercambio stoken_v2 + ltmid_v2 -> cookie_token_v2 (+ ltoken_v2).
# Requerido porque redeem_code() en genshin.py exige cookie_token o
# cookie_token_v2 (ver managers.requires_cookie_token). webLoginByPassword
# NO lo entrega, solo ltoken_v2/ltuid_v2/stoken(/ltmid_v2).
# Verificado leyendo el codigo fuente instalado de genshin.py:
#   client/routes.py -> COOKIE_V2_REFRESH_URL
#   client/manager/cookie.py -> fetch_cookie_with_stoken_v2()
COOKIE_V2_REFRESH_URL = (
    "https://sg-public-api.hoyoverse.com/account/ma-passport/token/getBySToken"
)

# Cuentas de juego vinculadas al usuario (usado por HoyoSession.get_game_accounts()).
GAME_ACCOUNTS_OS_URL = (
    "https://api-os-takumi.mihoyo.com/binding/api/getUserGameRolesByCookie"
)
GAME_ACCOUNTS_CN_URL = (
    "https://api-takumi.mihoyo.com/binding/api/getUserGameRolesByCookie"
)


# =============================================================================
# Login — app_ids y headers por region
# Fuente: genshin.py (x-rpc-app_id observado en requests de login/auth).
# =============================================================================

APP_ID_OS = "c9oqaq3s3gu8"
APP_ID_CN = "bll8iq97cem8"

# Version de la app reportada en el login. HoYoverse a veces empieza a
# rechazar versiones muy viejas (retcode de "app desactualizada") — si eso
# pasa, este es el primer valor a subir.
APP_VERSION_LOGIN = "2.55.0"

WEB_LOGIN_HEADERS: dict[str, str] = {
    "x-rpc-app_id":      APP_ID_OS,
    "x-rpc-client_type": "4",
    "x-rpc-app_version": APP_VERSION_LOGIN,
    "Origin":            "https://account.hoyolab.com",
    "Referer":           "https://account.hoyolab.com/",
    "Content-Type":      "application/json",
}

CN_LOGIN_HEADERS: dict[str, str] = {
    "x-rpc-app_id":      APP_ID_CN,
    "x-rpc-client_type": "4",
    "x-rpc-app_version": APP_VERSION_LOGIN,
    "Origin":            "https://user.mihoyo.com",
    "Referer":           "https://user.mihoyo.com/",
    "Content-Type":      "application/json",
}

EMAIL_SEND_HEADERS: dict[str, str] = {
    "x-rpc-app_id":      APP_ID_OS,
    "x-rpc-client_type": "4",
    "x-rpc-app_version": APP_VERSION_LOGIN,
    "Content-Type":      "application/json",
}


# =============================================================================
# Login — claves RSA (cifrado de email/contrasena)
# Fuente: genshin.py utility/auth.py (claves publicas usadas para
# PKCS1v15 antes de enviar credenciales). "Raro pero ocurre" que cambien.
# =============================================================================

RSA_KEY_1 = (
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

RSA_KEY_2 = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDDvekdPMHN3AYhm/vktJT+YJr7\n"
    "cI5DcsNKqdsx5DZX0gDuWFuIjzdwButrIYPNmRJ1G8ybDIF7oDW2eEpm5sMbL9zs\n"
    "9ExXCdvqrn51qELbqj0XxtMTIpaCHFSI50PfPpTFV9Xt/hmyVwokoOXFlAEgCn+Q\n"
    "CgGs52bFoYMtyi+xEQIDAQAB\n"
    "-----END PUBLIC KEY-----"
)


# =============================================================================
# Dynamic Secret (header 'ds') — salts
# Fuente: genshin.py constants.py -> DS_SALT.
# La FORMULA de generacion vive en HoyoWrapper/ds_utils.py (es logica, no
# un valor); aca solo van los salts, que es lo que realmente cambia.
# =============================================================================

DS_SALT_APP_LOGIN = "IZPgfb0dRPtBeLuFkdDznSZ6f4wWt6y2"   # getBySToken (redeem fix)
DS_SALT_CN_SIGNIN = "xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs"    # check-in diario CN


# =============================================================================
# Captcha Geetest
# =============================================================================

CAPTCHA_PORT_DEFAULT = 5000
CAPTCHA_LANG_OS = "en"
CAPTCHA_LANG_CN = "zh-cn"

# Servidor Geetest por region. CN debe usar el server chino porque
# api-na.geetest.com no es alcanzable desde China de forma confiable.
GEETEST_API_SERVER_OS = "api-na.geetest.com"
GEETEST_API_SERVER_CN = "api.geetest.com"

# CDN del script del widget Geetest v3. Si HoYoverse/Geetest cambia de
# version (gt.0.5.0.js -> gt.0.6.0.js, etc.) el captcha deja de cargar
# y hay que actualizar esto.
GEETEST_JS_URL = "https://static.geetest.com/static/js/gt.0.5.0.js"


# =============================================================================
# Login — retcodes conocidos
# =============================================================================

# Retcodes que indican "se requiere resolver un captcha Geetest".
CAPTCHA_RETCODES_OS: frozenset[int] = frozenset({-3101, -3200, -3206})
CAPTCHA_RETCODES_CN: frozenset[int] = frozenset({-3102, -3200, -3206})

# Retcodes de credenciales invalidas — se usan para decidir si hacer
# fallback OS -> CN (solo tiene sentido si el fallo es de credenciales,
# no de captcha: un captcha significa que HoYoLab SI reconocio el email).
CREDENTIAL_ERROR_RETCODES: frozenset[int] = frozenset({-3001, -3002, -3003})

# Un solo dict de mensajes para todos los retcodes de login conocidos.
# (Antes esto estaba duplicado: _MESSAGES_FOR_FALLBACK repetia las
# primeras 3 entradas de _MESSAGES en _raise_login_error. Consolidado aca.)
LOGIN_ERROR_MESSAGES: dict[int, str] = {
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


# =============================================================================
# Juegos — identificadores y nombres
# Fuente: genshin.py models/hoyolab/mimo.py (MimoGame.game — confirma
# hk4e=2, hkrpg=6, nap=8) y GameRoute overseas/chinese de routes.py.
# =============================================================================

GAME_BIZ_MAP: dict[str, str] = {
    "hk4e_global":  "Genshin Impact",
    "hk4e_cn":      "Genshin Impact (CN)",
    "hkrpg_global": "Honkai: Star Rail",
    "hkrpg_cn":     "Honkai: Star Rail (CN)",
    "nap_global":   "Zenless Zone Zero",
    "nap_cn":       "Zenless Zone Zero (CN)",
    "bh3_global":   "Honkai Impact 3rd",
    "bh3_cn":       "Honkai Impact 3rd (CN)",
    "nxx_global":   "Tears of Themis",
    "nxx_cn":       "Tears of Themis (CN)",
}

# game_id usado por el sistema Mimo Viajero. Confirmado 1:1 contra
# MimoGame.game de genshin.py (id==2 -> GENSHIN, id==6 -> STARRAIL,
# id==8 -> ZZZ). Honkai Impact 3rd no tiene mimo, por eso no aparece.
GAME_ID_MAP: dict[str, int] = {
    "hk4e_global":  2,
    "hk4e_cn":      2,
    "hkrpg_global": 6,
    "hkrpg_cn":     6,
    "nap_global":   8,
    "nap_cn":       8,
}

# Juegos soportados por el wrapper (usado para filtrar get_game_accounts()).
SUPPORTED_GAME_BIZ: frozenset[str] = frozenset({
    "hk4e_global",  "hk4e_cn",
    "hkrpg_global", "hkrpg_cn",
    "nap_global",   "nap_cn",
    "bh3_global",   "bh3_cn",
})


# =============================================================================
# Check-in diario
# Fuente: genshin.py routes.py (REWARD_URL) y client/components/daily.py
# (x-rpc-signgame por juego, headers CN).
# =============================================================================

REWARD_URLS: dict[str, str] = {
    "hk4e_global":  "https://sg-hk4e-api.hoyolab.com/event/sol",
    "hk4e_cn":      "https://api-takumi.mihoyo.com/event/luna/",
    "hkrpg_global": "https://sg-public-api.hoyolab.com/event/luna/os",
    "hkrpg_cn":     "https://api-takumi.mihoyo.com/event/luna/",
    "nap_global":   "https://sg-act-nap-api.hoyolab.com/event/luna/zzz/os",
    "nap_cn":       "https://api-takumi.mihoyo.com/event/luna/zzz",
    "bh3_global":   "https://sg-public-api.hoyolab.com/event/mani",
    "bh3_cn":       "https://api-takumi.mihoyo.com/event/luna/",
}

# act_id requerido en check-in. Cambia cada temporada (meses) — es el
# valor que mas mantenimiento va a necesitar.
ACT_IDS: dict[str, str] = {
    "hk4e_global":  "e202102251931481",
    "hk4e_cn":      "e202311201442471",
    "hkrpg_global": "e202303301540311",
    "hkrpg_cn":     "e202304121516551",
    "nap_global":   "e202406031448091",
    "nap_cn":       "e202406242138391",
    "bh3_global":   "e202110291205111",
    "bh3_cn":       "e202306201626331",
}

# x-rpc-signgame por juego.
SIGNGAME: dict[str, str] = {
    "hk4e_global":  "hk4e",
    "hk4e_cn":      "hk4e",
    "hkrpg_global": "hkrpg",
    "hkrpg_cn":     "hkrpg",
    "nap_global":   "zzz",
    "nap_cn":       "zzz",
    "bh3_global":   "bh3",
    "bh3_cn":       "bh3",
}

# Headers extra que HoYoverse espera en el check-in CN (ademas del 'ds').
# app_version aca es el de la app movil de Miyoushe, NO el de login web
# (son endpoints distintos con requisitos distintos).
CN_CHECKIN_APP_VERSION = "2.70.1"
CN_CHECKIN_CLIENT_TYPE = "5"
CN_CHECKIN_SYS_VERSION = "12"
CN_CHECKIN_PLATFORM = "android"
CN_CHECKIN_CHANNEL = "miyousheluodi"


# =============================================================================
# Mimo Viajero
# Fuente: genshin.py routes.py (MIMO_URL) y hoyolab.py (_request_mimo,
# redeem_code) y models/hoyolab/mimo.py (MimoTaskType, MimoTaskStatus).
# =============================================================================

MIMO_BASE_URL = "https://sg-public-api.hoyolab.com/event/e2023mimotravel"

# Tipos de tarea mimo completables via finish_mimo_task() (no requieren
# accion real dentro del juego). NO incluir GI_LOGIN(5), GI_GAME(6),
# HSR_GAME(8), ZZZ_DAILY_LOGIN(12), ZZZ_CONSECUTIVE_LOGIN(13), ZZZ_GAME(16)
# — esos SI requieren jugar.
MIMO_FINISHABLE_TASK_TYPES: frozenset[int] = frozenset({
    1,   # FINISHABLE — tutoriales y similares
    2,   # VISIT — visitar grupo de interes
    3,   # COMMENT — participar en interacciones
    4,   # VIEW_TOPIC — ver un tema
    10,  # TRAILER — ver trailer
})

# Estados de tarea mimo (MimoTaskStatus de genshin.py).
MIMO_TASK_STATUS_FINISHED = 1   # completada, recompensa sin reclamar
MIMO_TASK_STATUS_ONGOING = 2    # en progreso / pendiente
MIMO_TASK_STATUS_CLAIMED = 3    # ya reclamada

# Keywords (en minusculas) para identificar el item de moneda premium
# en la tienda mimo, por juego. HoYoverse puede renombrar el item.
CURRENCY_ITEM_KEYWORDS: dict[str, list[str]] = {
    "hk4e_global":  ["genesis crystal", "genesis crystals"],
    "hk4e_cn":      ["创世结晶"],
    "hkrpg_global": ["oneiric shard", "oneiric shards"],
    "hkrpg_cn":     ["星琼"],
    "nap_global":   ["monochrome", "polychrome"],
    "nap_cn":       ["菲林", "加密母带"],
    "bh3_global":   ["ether fuel"],
    "bh3_cn":       ["以太燃料"],
}


# =============================================================================
# Canje de codigos de regalo
# Fuente: genshin.py routes.py (CODE_URL) y hoyolab.py (redeem_code).
# Solo OS: CODE_URL.chinese es un dict vacio en genshin.py — CN no
# soporta redeem via esta API en absoluto.
# =============================================================================

CODE_URLS: dict[str, str] = {
    "hk4e_global":  "https://sg-hk4e-api.hoyoverse.com/common/apicdkey/api/webExchangeCdkey",
    "hkrpg_global": "https://sg-hkrpg-api.hoyoverse.com/common/apicdkey/api/webExchangeCdkeyRisk",
    "nap_global":   "https://public-operation-nap.hoyoverse.com/common/apicdkey/api/webExchangeCdkey",
}

# StarRail y ZZZ usan POST para redeem. Solo Genshin usa GET.
# Fuente: hoyolab.py redeem_code():
#   post_request_games = {types.Game.ZZZ, types.Game.STARRAIL}
# BUG CORREGIDO: antes solo tenia hkrpg_global — nap_global (ZZZ) faltaba
# y se enviaba como GET quedando inconsistente con el comportamiento real.
CODE_POST_GAMES: frozenset[str] = frozenset({"hkrpg_global", "nap_global"})

CODE_ORIGINS: dict[str, str] = {
    "hk4e_global":  "https://genshin.hoyoverse.com",
    "hkrpg_global": "https://hsr.hoyoverse.com",
    "nap_global":   "https://zenless.hoyoverse.com",
}

# URLs publicas de canje mostradas al usuario tras una compra mimo
# exitosa (para que canjee el codigo obtenido). Son paginas web, no
# endpoints de API — pueden cambiar de dominio/ruta independientemente
# de CODE_URLS.
REDEEM_WEBPAGE_URLS: dict[str, str] = {
    "hk4e_global":  "https://genshin.hoyoverse.com/en/gift",
    "hkrpg_global": "https://hsr.hoyoverse.com/gift",
    "nap_global":   "https://zenless.hoyoverse.com/redemption",
}

# Juegos que se ofrecen en el menu de canje de codigos (main.py opcion 5).
# Derivado de GAME_BIZ_MAP + CODE_URLS para que nunca queden desincronizados
# entre si (antes existia una lista aparte en app.py que se podia
# desactualizar sin que nada avisara).
REDEEMABLE_GAMES: list[dict[str, str]] = [
    {"display": GAME_BIZ_MAP[biz], "game_biz": biz}
    for biz in CODE_URLS
]


# =============================================================================
# Anti-deteccion — rangos de delay (segundos, min/max para random.uniform)
# Centralizados para poder ajustar el "ritmo" del bot sin buscar por
# todo el codigo. Nombrados por el contexto donde se usan.
# =============================================================================

DELAY_BETWEEN_ACCOUNTS: tuple[float, float] = (2.0, 5.0)
DELAY_BETWEEN_GAMES: tuple[float, float] = (1.0, 3.0)
DELAY_BETWEEN_MIMO_TASKS: tuple[float, float] = (0.8, 2.0)
DELAY_BETWEEN_MIMO_PURCHASES: tuple[float, float] = (1.5, 4.0)
DELAY_BETWEEN_REDEEMS: tuple[float, float] = (6.0, 10.0)
DELAY_CAPTCHA_HUMAN: tuple[float, float] = (3.0, 5.0)


# =============================================================================
# Retry / rate limiting (tenacity)
# =============================================================================

RETRY_WAIT_MULTIPLIER = 2
RETRY_WAIT_MIN = 5
RETRY_WAIT_MAX = 60
RETRY_STOP_ATTEMPTS = 4
