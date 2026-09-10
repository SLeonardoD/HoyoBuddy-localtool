# HoYoWrapper — Documento de Continuidad del Proyecto

---

## 1. Qué es la herramienta

**HoYoWrapper** es un wrapper en Python que automatiza tareas diarias en
HoYoLab (plataforma web oficial de HoYoverse) para múltiples cuentas y
múltiples juegos simultáneamente. Actúa como cliente HTTP que replica el
comportamiento del navegador web del usuario, autenticándose con sus
credenciales y ejecutando las tareas en su nombre.

El MVP actual es una **aplicación de consola** (CLI) que sirve como banco
de pruebas para validar todas las funciones antes de migrarlas a una APK
Android (proyecto TaskBlazer, en planificación).

---

## 2. Objetivo

- Automatizar el check-in diario en todos los juegos de HoYoverse.
- Completar y reclamar tareas del Mimo Viajero (sistema de puntos de HoYoLab).
- Comprar moneda premium de cada juego con los puntos acumulados en el Mimo.
- Canjear códigos de regalo en todas las cuentas que tengan el juego objetivo.
- Soportar múltiples cuentas (OS y CN) y múltiples juegos simultáneamente.
- Ser escalable a nuevos servicios/juegos sin modificar el código existente.

---

## 3. Cómo se lleva a cabo

### Stack tecnológico
- **Python 3.14** (Windows, desarrollo y pruebas)
- **aiohttp** — cliente HTTP async
- **SQLAlchemy + SQLite** — persistencia local (cookies, aliases, historial)
- **tenacity** — retry con backoff exponencial ante rate limits
- **loguru** — logging exhaustivo en consola y archivo por ejecución
- **cryptography** — cifrado RSA PKCS1v15 para credenciales
- **aiohttp web** — servidor local temporal para resolver captcha Geetest
- **pytest + pytest-asyncio + aioresponses + mockk** — suite de tests

### Estructura del proyecto
```
proyecto/
├── main.py                        ← entrada de consola (menú 5 opciones)
├── app.py                         ← lógica de negocio (testeable)
├── tests.py                       ← runner de tests con loguru
├── pytest.ini
├── db/
│   ├── __init__.py
│   ├── cookie_store.py            ← cookies cifradas por (email, región)
│   └── account_store.py          ← aliases + email + región
└── HoyoWrapper/
    ├── __init__.py
    ├── session.py                 ← HoyoSession, HoyoAPIError, LoginError
    ├── auth_manager.py            ← login OS/CN, captcha, email verify
    ├── captcha_server.py          ← servidor Geetest local con delay humano
    ├── game_model.py              ← BaseGame: checkin, mimo, redeem
    ├── session_manager.py         ← HoyoManager, ActiveSession
    ├── genshin_game.py            ← constantes Genshin Impact
    ├── starrail_game.py           ← constantes Honkai: Star Rail
    ├── zzz_game.py                ← constantes Zenless Zone Zero
    └── honkai_game.py             ← constantes Honkai Impact 3rd
tests/
    ├── conftest.py                ← fixtures compartidos
    ├── test_cookie_store.py       ← 14 tests
    ├── test_account_store.py      ← 17 tests
    ├── test_session.py            ← 12 tests
    ├── test_game_model.py         ← 22 tests
    ├── test_auth_manager.py       ← 12 tests
    ├── test_session_manager.py    ← 13 tests
    └── test_app.py                ← 29 tests
```
**Total: ~135 tests, todos en verde.**

---

## 4. Funciones implementadas

### Gestión de cuentas (menú principal)
- **Agregar cuenta** — login con email+contraseña, captcha Geetest, verificación
  email opcional, guarda cookies cifradas y alias.
- **Listar cuentas** — muestra alias, región y fecha de registro.
- **Eliminar cuenta** — elimina alias (conserva cookies por si se re-registra).
- **Completar tareas diarias** — check-in + mimo + compra en todas las cuentas.
- **Canjear código de regalo** — menú de selección de juego + código, canjea
  en todas las cuentas OS que tengan el juego.

### HoyoWrapper (capa de negocio)
- **Login OS** (HoYoLab global) con captcha Geetest y verificación email.
- **Login CN** (Miyoushe) con captcha Geetest.
- **Restaurar sesión** desde cookies sin necesitar contraseña.
- **Check-in diario** para Genshin, Star Rail, ZZZ, Honkai (OS y CN).
- **Mimo — obtener tareas** con clasificación por status y tipo.
- **Mimo — finish task** para tareas completables vía API (VISIT, VIEW_TOPIC, etc.).
- **Mimo — claim reward** para tareas completadas.
- **Mimo — tienda** listado de items disponibles.
- **Mimo — compra de moneda** filtrado por keywords por juego.
- **Redeem de códigos** (Genshin GET, StarRail POST, ZZZ GET).

### Persistencia
- **CookieStore** — SQLite, cookies por (email, región), cifradas, reversibles.
  Método `get_by_hash()` para re-auth sin email.
- **AccountStore** — SQLite, alias + email + región + hash.

---

## 5. Funciones validadas en ambiente real ✅

- **Login OS con captcha Geetest** — probado y funcionando.
  - Captcha se resuelve automáticamente en modo silencioso.
  - Aigis header formato correcto: `session_id;base64(json(mmt_data))`.
  - Claves RSA correctas (obtenidas de genshin.py v. actual).
  - Delay humano 3-5s antes de enviar resultado del captcha.
- **Persistencia de cookies** — cookies guardadas y recuperadas correctamente.

---

## 6. Funciones implementadas NO validadas en vivo ⚠️

Estas funciones pasan todos los tests unitarios pero **nunca se han ejecutado
contra los servidores reales de HoYoverse**:

1. **Restaurar sesión** — cookies guardadas → sesión válida sin login.
2. **Check-in diario** — `claim_daily_reward()` para cada juego.
3. **Mimo — get tasks** — `get_mimo_tasks()` y estructura real de respuesta.
4. **Mimo — finish task** — `finish_mimo_task()` para tareas tipo VISIT etc.
5. **Mimo — claim reward** — `claim_mimo_task_reward()`.
6. **Mimo — tienda** — `get_mimo_shop_items()`.
7. **Mimo — compra** — `buy_mimo_currency()`.
8. **Redeem de códigos** — `redeem_code()` con código real.

---

## 7. Plan de validación en vivo (en orden)

### Paso 1 — Restaurar sesión (prerequisito de todo)
**Acción**: Ejecutar opción 4 del menú con una cuenta ya registrada.
**Éxito**: Log muestra `[AUTH] Sesion restaurada desde store`.
**Posibles fallos**:
- Cookies expiradas → log `[AUTH] Cookies expiradas` → solución: re-login.
- `get_game_accounts()` falla con retcode desconocido → revisar log.

### Paso 2 — Check-in diario
**Acción**: La opción 4 ejecuta check-in automáticamente tras restaurar sesión.
**Éxito**: Log muestra `[CHECKIN] Completado | game=... dia=N`.
**Posibles fallos**:
- `act_id` caducado → retcode `-100` o similar → actualizar `_ACT_IDS`
  en `game_model.py` y en Supabase remote_config (cuando exista).
- URL de endpoint cambiada → retcode HTTP 404 → actualizar `_REWARD_URLS`.
- `x-rpc-signgame` incorrecto → retcode `-1` → verificar en genshin.py.

### Paso 3 — Mimo: obtener tareas
**Acción**: Automático tras check-in en opción 4.
**Éxito**: Log muestra cada tarea con `task_id`, `task_name`, `task_type`, `status`.
**Posibles fallos**:
- URL mimo incorrecta → actualmente `e2023mimotravel` (verificado en genshin.py).
- Prefijo Genshin incorrecto → actualmente `/nata/` (verificado en genshin.py).
- Campos de respuesta diferentes → log DEBUG mostrará la estructura real,
  ajustar los campos `task.get("task_name")`, `task.get("task_type")` etc.
- `version_id` no encontrado → revisar estructura del índice mimo en log.

### Paso 4 — Mimo: finish y claim
**Acción**: Automático si hay tareas ONGOING con tipo completable (2,3,4,10).
**Éxito**: Log `[MIMO] Tarea completada via API` y `[MIMO] recompensa reclamada`.
**Posibles fallos**:
- `finish-task` rechazado para ciertos tipos → retcode en log, ajustar
  `MIMO_FINISHABLE_TASK_TYPES` en `game_model.py`.
- `receive-point` falla → retcode en log, verificar método GET/POST por juego.

### Paso 5 — Mimo: tienda y compra
**Acción**: Automático tras tareas en opción 4.
**Éxito**: Log `[MIMO] item tienda` por cada item, `[MIMO] Compra exitosa` si hay puntos.
**Posibles fallos**:
- Keywords de moneda incorrectos → ajustar `CURRENCY_ITEM_KEYWORDS`.
- Sin puntos suficientes → `HoyoAPIError` con retcode de saldo insuficiente.
- Campo `award_id` con nombre diferente → revisar log de items de tienda.

### Paso 6 — Redeem de códigos
**Acción**: Opción 5 del menú, seleccionar juego e introducir código.
**Éxito**: Log `[REDEEM] Canjeado en...` por cada cuenta.
**Posibles fallos**:
- Código ya usado → retcode `-2017` (esperado, reportado como failure).
- Código caducado → retcode diferente → reportado en log.
- URL de redeem incorrecta → HTTP error → verificar en genshin.py.
- Cooldown insuficiente entre cuentas → aumentar delay (actualmente 6-10s).

---

## 8. Particularidades importantes del proyecto

### Valores que cambian regularmente (CRÍTICO)
HoYoverse actualiza estos valores sin aviso previo:

| Valor | Frecuencia de cambio | Dónde está en el código |
|-------|---------------------|------------------------|
| `act_id` por juego | Cada temporada (meses) | `_ACT_IDS` en `game_model.py` |
| URLs de check-in | Ocasional (reorganización) | `_REWARD_URLS` en `game_model.py` |
| URL base del mimo | Ocasional | `_MIMO_BASE_URL` en `game_model.py` |
| Prefijo Genshin mimo | Ocasional (`/qiuqiu/`→`/nata/`) | `_mimo_url()` en `game_model.py` |
| Claves RSA | Raro pero ocurre | `_RSA_KEY_1/2` en `auth_manager.py` |
| Retcodes de captcha | Ocasional | `_CAPTCHA_RETCODES_OS/CN` en `auth_manager.py` |

**Fuente de verdad**: siempre verificar en
`genshin.py` (pip install genshin) → archivos:
- `genshin/client/routes.py` → URLs y act_ids
- `genshin/utility/auth.py` → claves RSA y aigis
- `genshin/client/components/hoyolab.py` → lógica mimo
- `genshin/client/components/daily.py` → lógica check-in

### Juegos soportados y sus capacidades

| Juego | game_biz | Check-in OS | Check-in CN | Mimo | Redeem |
|-------|----------|-------------|-------------|------|--------|
| Genshin Impact | hk4e_global/cn | ✅ | ✅ | ✅ OS | ✅ GET |
| Honkai: Star Rail | hkrpg_global/cn | ✅ | ✅ | ✅ OS | ✅ POST |
| Zenless Zone Zero | nap_global/cn | ✅ | ✅ | ✅ OS | ✅ GET |
| Honkai Impact 3rd | bh3_global/cn | ✅ | ✅ | ❌ | ❌ |
| Tears of Themis | nxx_global/cn | ⏳ planificado | ⏳ | ❌ | ❌ |

### Mimo — tipos de tareas
```
FINISHABLE via API (finish_mimo_task):
  1 = FINISHABLE (tutoriales)
  2 = VISIT (visitar grupo)
  3 = COMMENT (comentar)
  4 = VIEW_TOPIC (ver tema)
  10 = TRAILER (ver trailer)

NO completables via API (requieren acción en juego):
  5 = GI_LOGIN
  6 = GI_GAME
  8 = HSR_GAME
  12 = ZZZ_DAILY_LOGIN
  13 = ZZZ_CONSECUTIVE_LOGIN
  16 = ZZZ_GAME
```

### Formato del aigis header (CRÍTICO)
El header `x-rpc-aigis` de respuesta al captcha tiene formato:
```
{session_id};{base64(json({"geetest_challenge": "...", "geetest_validate": "...", "geetest_seccode": "..."}))}
```
**NO es JSON**. Usar cualquier otro formato resulta en retcode `-3102`.
Implementado en `_build_aigis_header()` en `auth_manager.py`.

### Anti-detección implementada
- `device_id` generado una vez en el login y reutilizado en todos los
  requests (simula dispositivo real constante).
- `x-rpc-device_model` con `account_id` en headers CN.
- Delay aleatorio con decimales entre cuentas (1.0-3.0s tareas, 6-10s redeems).
- Delay humano en resolución del captcha (3.0-5.0s).
- User-Agent moderno: Chrome 124 (Windows 10).
- Retry con backoff exponencial + jitter ante rate limits (-429).
- `finish_mimo_task` con delay 0.8-2.0s entre tareas.

### Fallback CN — comportamiento importante
El sistema intenta login CN **solo si** el error OS contiene un retcode
de credenciales (`-3001`, `-3002`, `-3003`). Si OS falla por captcha,
**no** intenta CN porque `passport-api.miyoushe.com` no es accesible
fuera de China y causaría un error de DNS.

### Logging
- **Consola**: loguru, solo INFO+ con formato limpio.
- **Archivo**: `error_log_YYYYMMDD_HHMMSS.txt` por ejecución, nivel DEBUG,
  tracebacks completos con `backtrace=True` y `diagnose=True`.
- `test_output.txt`: generado por `python tests.py`, output completo de pytest.

---

## 9. Consideraciones adicionales para la continuidad

### Tears of Themis (pendiente de implementar)
Añadir soporte requiere exactamente:
1. Crear `HoyoWrapper/tot_game.py` con `GAME_BIZ="nxx_global"`,
   `GAME_NAME="Tears of Themis"`, `GAME_ID=0`.
2. Añadir entradas en `_REWARD_URLS`, `_ACT_IDS`, `_SIGNGAME` de `game_model.py`.
3. Registrar en `create_game()` factory.
4. Añadir `"nxx_global"` y `"nxx_cn"` al set `_SUPPORTED` en
   `session_manager.py` y `app.py`.

### Remote Config (planificado, no implementado en MVP)
La idea es usar Supabase para almacenar los valores que cambian
(`act_ids`, URLs, etc.) y que la app los sincronice al arrancar.
Así un cambio de `act_id` se corrige en Supabase sin tocar el código.
En el MVP actual estos valores están hardcodeados en `game_model.py`.

### Re-autenticación cuando las cookies expiran
El flujo actual: si `restore_session()` devuelve `None`, el usuario
debe ir a opción 1 (Agregar cuenta) e introducir email + contraseña.
El email **se guarda en `AccountStore`** para poder mostrarlo al usuario,
pero la contraseña nunca se guarda.

### Tests que usan archivo temporal vs :memory:
Tres tests de `test_cookie_store.py` usan el fixture `cookie_store_file`
(archivo temporal real via `tmp_path`) porque necesitan abrir una segunda
conexión SQLite directa. Los demás usan `:memory:`. Esta distinción es
importante si se añaden más tests que inspeccionen la DB directamente.

### Dependencias del proyecto
```
aiohttp
sqlalchemy
tenacity
loguru
cryptography
pytest
pytest-asyncio
aioresponses
```

### Próxima sesión — orden recomendado
1. Ejecutar `python tests.py` para confirmar que todos los tests siguen en verde.
2. Seguir el plan de validación en vivo (Pasos 1-6).
3. Corregir los errores que aparezcan (principalmente act_ids y estructura mimo).
4. Validar redeem con un código real.
5. Una vez todo validado → comenzar APK TaskBlazer.

### APK TaskBlazer (planificado)
Arquitectura completa ya diseñada: Kotlin Multiplatform, Jetpack Compose,
SQLDelight, Ktor, Koin, Supabase, 7 fases de desarrollo. Documento completo
guardado en `outputs/TaskBlazer_Architecture.md`.
El MVP de consola debe estar 100% validado antes de empezar la APK.
