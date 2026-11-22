# HoYoWrapper

Automatización de tareas diarias en HoYoLab (check-in, Mimo Viajero, canje de códigos de regalo) para múltiples cuentas y múltiples juegos de HoYoverse, desde la línea de comandos.

---

## Tabla de contenidos

1. [Qué es y por qué existe](#qué-es-y-por-qué-existe)
2. [Filosofía del proyecto](#filosofía-del-proyecto)
3. [Qué hace exactamente](#qué-hace-exactamente)
4. [Arquitectura](#arquitectura)
5. [Estructura de carpetas](#estructura-de-carpetas)
6. [Módulo por módulo](#módulo-por-módulo)
7. [Base de datos](#base-de-datos)
8. [Sistema de códigos de regalo](#sistema-de-códigos-de-regalo)
9. [Sesiones y renovación del token](#sesiones-y-renovación-del-token)
10. [Seguridad anti-baneo](#seguridad-anti-baneo)
11. [Regiones OS y CN](#regiones-os-y-cn)
12. [`config.py` — valores que cambian con el tiempo](#configpy--valores-que-cambian-con-el-tiempo)
13. [Instalación y uso](#instalación-y-uso)
14. [Tests](#tests)
15. [Limitaciones conocidas](#limitaciones-conocidas)
16. [Mantenimiento futuro](#mantenimiento-futuro)
17. [Roadmap](#roadmap)
18. [Créditos y fuente de verdad](#créditos-y-fuente-de-verdad)

---

## Qué es y por qué existe

Jugar varios juegos de HoYoverse (Genshin Impact, Honkai: Star Rail, Zenless Zone Zero, Honkai Impact 3rd) en varias cuentas significa repetir, todos los días, la misma rutina aburrida en HoYoLab: entrar, hacer check-in, entrar al Mimo Viajero, reclamar tareas, comprar moneda premium con los puntos acumulados, y de vez en cuando canjear un código de regalo en cada cuenta que tenga el juego correspondiente. Multiplicado por varias cuentas, es una pérdida de tiempo mecánica y repetitiva.

**HoYoWrapper** automatiza esa rutina. Actúa como un cliente HTTP que se autentica con las credenciales del usuario y replica, paso a paso, las mismas llamadas que hace el navegador cuando una persona navega HoYoLab manualmente — con las pausas y el ritmo de una persona real, no de un script.

El MVP actual es una **aplicación de consola (CLI)**. Sirvió como banco de pruebas para validar todas las funciones contra los servidores reales de HoYoverse antes de invertir en algo más ambicioso (una app móvil — ver [Roadmap](#roadmap)). Esa validación ya se completó: todas las funciones fueron probadas en vivo y funcionan.

## Filosofía del proyecto

Algunas decisiones de diseño que atraviesan todo el proyecto y conviene entender antes de leer el resto:

- **Es una herramienta de uso personal y local.** No es un servicio, no tiene usuarios múltiples, no se expone a internet. Esto justifica decisiones que en un sistema multiusuario serían inaceptables: el email se guarda en texto plano (no hasheado) y las cookies de sesión se guardan en JSON plano (no cifradas). La ganancia de seguridad de cifrar estos datos en una base SQLite local, de un solo usuario, en su propia máquina, es marginal — y a cambio se gana poder reconstruir un flujo de "reingresar contraseña" simple, sin tener que re-teclear el email cada vez.

- **Preferimos que el proceso tarde más antes que arriesgar una cuenta.** Cada decisión de timing (ver [Seguridad anti-baneo](#seguridad-anti-baneo)) está inclinada deliberadamente hacia el lado lento-pero-seguro. HoYoverse no publica sus umbrales exactos de detección de bots, así que la postura del proyecto es "más lento que un humano rápido, nunca más rápido que uno lento".

- **`genshin.py` (la librería de Python) es la fuente de verdad, no la memoria del asistente ni la documentación.** Cada endpoint, cada salt de firma, cada retcode documentado en este proyecto fue verificado leyendo el código fuente instalado de `genshin.py` (`pip install genshin`), no recordado de memoria. Esto importa porque HoYoverse cambia endpoints sin avisar (ver el [incidente real documentado más abajo](#configpy--valores-que-cambian-con-el-tiempo)), y la memoria de un LLM sobre APIs de terceros se desactualiza; el código fuente instalado, no.

- **Centralización de valores volátiles.** Todo lo que HoYoverse puede cambiar sin aviso (URLs, `act_id`, salts, retcodes) vive en un único archivo, `config.py`, separado de la lógica. Cuando algo se rompe en vivo, ese es el único lugar que hay que mirar.

- **Clasificar errores por alcance, no tratarlos todos igual.** Un fallo de red no es lo mismo que "código ya usado por esta cuenta", que tampoco es lo mismo que "código expirado para todo el mundo". El proyecto distingue explícitamente estos tres niveles en el sistema de canje de códigos (ver más abajo) en vez de la solución perezosa de "reintentar siempre" o "no reintentar nunca".

## Qué hace exactamente

Desde el menú de consola (`main.py`):

1. **Agregar cuenta** — login con email + contraseña, resuelve el captcha Geetest automáticamente, guarda la sesión.
2. **Listar cuentas** — muestra alias, región y fecha de registro de cada cuenta guardada.
3. **Eliminar cuenta** — quita una cuenta de la lista visible (ver nota sobre soft-delete en [Base de datos](#base-de-datos)).
4. **Completar tareas diarias** — para cada cuenta y cada juego: check-in diario, reclamar tareas del Mimo Viajero, y comprar moneda premium con los puntos acumulados.
   - **Enter** ejecuta todo (todas las cuentas, todos sus juegos). **`e`** permite elegir: primero las cuentas (`1,3`, `1-3`) y luego, cuando le toca el turno a cada una, los juegos que detectó en ella (Enter = todos, números = solo esos, `0` o Ctrl+C = omitir la cuenta). Sirve para probar un solo juego o una sola cuenta sin tocar las demás.
5. **Canjear código de regalo (manual)** — el usuario elige el juego, pega un código, y se canjea en todas las cuentas OS que tengan ese juego.
6. **Agregar código de regalo a la base de datos** — guarda un código para que el sistema automático lo recuerde y lo canjee en cualquier cuenta futura sin volver a pedirlo.
7. **Canjear códigos pendientes (automático)** — revisa todos los códigos guardados en la base de datos y los canjea en todas las cuentas/juegos que corresponda y que todavía no lo hayan intentado.
8. **Reingresar contraseña** — un login real (sin reutilizar las cookies guardadas) para una cuenta que no se puede renovar sola, sin volver a escribir el email (ya se conoce).
9. **Ver medición de sesiones** — cuánto vive el token de canje y cuál es el intervalo máximo seguro entre corridas (ver [Sesiones y renovación del token](#sesiones-y-renovación-del-token)).

## Arquitectura

```
                     ┌──────────────┐
                     │   main.py    │   Menú de consola. Solo I/O.
                     └──────┬───────┘
                            │
                     ┌──────▼───────┐
                     │    app.py    │   Lógica de negocio. Orquesta
                     └──┬────────┬──┘   cuentas, tareas diarias y códigos.
                        │        │
              ┌─────────▼──┐  ┌──▼──────────────┐
              │  db/store.py│  │ HoyoWrapper/    │
              │             │  │ session_manager │
              │ AccountStore│  │      .py        │
              │ GiftCodeSt. │  └──┬──────────┬───┘
              └─────────────┘     │          │
                          ┌───────▼──┐   ┌───▼──────────┐
                          │  auth_   │   │ game_model.py│
                          │ manager  │   │ (+ 4 juegos) │
                          │   .py    │   └──────┬───────┘
                          └────┬─────┘          │
                               │                │
                          ┌────▼────────────────▼───┐
                          │      session.py         │  HTTP autenticado
                          │   (HoyoSession.request)  │  contra HoYoverse
                          └──────────────────────────┘

  config.py         → valores volátiles, usado por TODOS los módulos de arriba
  ds_utils.py        → firma 'ds' (dynamic secret), compartido
  timing.py          → delays anti-detección, compartido
  captcha_server.py  → resolución del captcha Geetest
```

La regla de dependencia es de abajo hacia arriba: `session.py` no sabe nada de juegos ni de autenticación; `auth_manager.py` y `game_model.py` son capas paralelas que ambas dependen de `session.py` pero no dependen la una de la otra (por eso `ds_utils.py` y `timing.py`, que ambas necesitan, son módulos compartidos aparte y no viven dentro de ninguna de las dos). `app.py` es la única capa que conoce tanto la DB como el wrapper de HoYoverse; `main.py` no sabe nada de HoYoverse, solo le habla a `app.py`.

## Estructura de carpetas

```
proyecto/
├── main.py                    Menú de consola (entrada del programa)
├── app.py                     Lógica de negocio (testeable sin consola)
├── config.py                  Valores volátiles centralizados
├── pytest.ini
│
├── db/
│   ├── __init__.py
│   └── store.py               AccountStore + GiftCodeStore (SQLite)
│
├── HoyoWrapper/
│   ├── __init__.py
│   ├── session.py              HoyoSession, HoyoAPIError, LoginError
│   ├── auth_manager.py         Login OS/CN, captcha, email verify
│   ├── captcha_server.py       Servidor local para resolver Geetest
│   ├── session_manager.py      HoyoManager, ActiveSession
│   ├── game_model.py           BaseGame: check-in, mimo, redeem
│   ├── ds_utils.py             Firma 'ds' compartida
│   ├── timing.py               Delays anti-detección compartidos
│   ├── genshin_game.py         Constantes de Genshin Impact
│   ├── starrail_game.py        Constantes de Honkai: Star Rail
│   ├── zzz_game.py             Constantes de Zenless Zone Zero
│   └── honkai_game.py          Constantes de Honkai Impact 3rd
│
└── tests/
    ├── conftest.py             Fixtures compartidos + principios de testing
    ├── test_store.py
    ├── test_session.py
    ├── test_session_manager.py
    ├── test_auth_manager.py
    ├── test_auth_app_login.py   Login por app, captcha v4/v3, medición
    ├── test_captcha_server.py
    ├── test_ds_utils.py
    ├── test_game_model.py
    └── test_app.py
```

## Módulo por módulo

### `main.py`
Único punto de entrada (`python main.py`). Contiene el bucle del menú y todo el `print()`/`input()` de la aplicación. No sabe nada sobre HoYoverse, cookies, ni HTTP — solo llama funciones de `app.py` y muestra sus resultados. Configura `loguru` para dos salidas: consola (nivel INFO, formato limpio) y un archivo `error_log_<fecha>.txt` por ejecución (nivel DEBUG, con traceback completo) para diagnóstico.

### `app.py`
El cerebro de la aplicación. Cada acción del menú tiene una función correspondiente aquí, y todas son `async` y testeables sin tocar la consola:

- `add_account()` / `relogin_account()` / `delete_account()` / `list_accounts()` — gestión de cuentas.
- `restore_session()` — reconstruye una `ActiveSession` a partir de una fila de `AccountStore`, validando que las cookies sigan sirviendo.
- `run_daily_tasks()` → `_process_account_games()` → `_process_mimo_tasks()` / `_process_mimo_buys()` — la cadena completa de tareas diarias.
- `redeem_code_for_all()` — canje manual de un código puntual.
- `add_gift_code()` / `redeem_pending_codes()` — el sistema de canje automático (ver [sección dedicada](#sistema-de-códigos-de-regalo)).

`AppContext` es un `NamedTuple` con las tres dependencias que necesita todo lo anterior (`account_store`, `code_store`, `manager`), inyectadas desde `main.py` — nada en `app.py` crea sus propias conexiones a la base de datos ni instancia `HoyoManager` por su cuenta, lo cual hace posible testear cada función con mocks/DB en memoria sin arrancar nada real.

### `HoyoWrapper/session.py`
La pieza más pequeña y más fundamental. `HoyoSession` guarda las cookies, la región (`os`/`cn`) y el `device_id` de una sesión autenticada, y expone `request()`: hace la petición HTTP con `aiohttp`, revisa el `retcode` de la respuesta, y lanza `HoyoAPIError` si no es `0`. Todo lo demás en el proyecto que habla con HoYoverse pasa, en algún punto, por este método.

### `HoyoWrapper/auth_manager.py`
Toda la lógica de login y de renovación de sesión. Para OS hay dos métodos, elegibles con `config.OS_LOGIN_METHOD`: el **login por app** (por defecto; devuelve el `stoken`) y el **login web** (el original; no lo devuelve). Resuelve captcha Geetest (v4 y v3) y la verificación por email de dispositivo nuevo. `renew_cookie_token()` pide un `cookie_token_v2` nuevo con el `stoken` guardado, sin contraseña. También hace fallback automático de OS a CN, pero **solo** cuando el fallo es de credenciales, nunca cuando es de captcha (un captcha ya implica que HoYoLab reconoció el email, y además `miyoushe.com` no es alcanzable de forma confiable fuera de China). Los logs nunca contienen tokens ni valores de cookies (`_redact`): se comparten para diagnosticar.

### `HoyoWrapper/captcha_server.py`
Levanta un servidor HTTP local temporal, abre el navegador del usuario, sirve el widget Geetest v3, y espera a que se resuelva (automáticamente, en modo silencioso) antes de devolver el resultado al flujo de login. Incluye un delay humano antes de enviar el resultado resuelto.

### `HoyoWrapper/session_manager.py`
`HoyoManager` es la fachada pública del wrapper: `HoyoManager(store).login(email, password)` devuelve una `ActiveSession` con los juegos ya detectados y listos. `ActiveSession` filtra las cuentas de juego a solo las soportadas (`config.SUPPORTED_GAME_BIZ`) e instancia el objeto de juego correcto (`GenshinGame`, `StarRailGame`, etc.) para cada una.

### `HoyoWrapper/game_model.py`
`BaseGame` es la clase base de la que heredan los 4 módulos de juego. Contiene **toda** la lógica de check-in, Mimo Viajero y canje de códigos — los 4 archivos `*_game.py` son solo un puñado de constantes de clase (`GAME_BIZ`, `GAME_NAME`, `GAME_ID`) que le dicen a `BaseGame` cómo comportarse para ese juego particular. Esto significa que agregar un juego nuevo (como Tears of Themis, ver [Roadmap](#roadmap)) es, en el caso ideal, solo agregar un archivo de constantes y una entrada en `config.py` — no lógica nueva.

### `HoyoWrapper/ds_utils.py` y `HoyoWrapper/timing.py`
Dos utilidades genéricas, deliberadamente separadas de `game_model.py` para no invertir la jerarquía de capas (`auth_manager.py` las necesita tanto como `game_model.py`, y la capa de autenticación no debería depender de la capa de lógica de juego):

- `ds_utils.generate_dynamic_secret(salt)` — genera el header `ds` que HoYoverse exige como prueba de que el cliente es "legítimo".
- `timing.random_delay(min_s, max_s)` — espera aleatoria; toda la [seguridad anti-baneo](#seguridad-anti-baneo) del proyecto se construye llamando esta función con distintos rangos de `config.py`.

## Base de datos

Todo vive en un único archivo SQLite (`config.DB_PATH`), gestionado por dos clases en `db/store.py`:

### `AccountStore` — una sola tabla para cuentas

Originalmente el proyecto tenía dos tablas (una para cookies, otra para el alias) enlazadas por el hash del email. Se fusionaron en una sola tabla `accounts` con estas columnas: `alias`, `email` (texto plano), `region`, `cookies_json`, `device_id`, `created_at`, `updated_at`.

Dos particularidades de diseño importantes:

- **El email no se hashea.** Es una decisión explícita (ver [Filosofía](#filosofía-del-proyecto)): permite que "reingresar contraseña" funcione sin pedirle el email al usuario de nuevo, ya que se conoce.
- **"Eliminar cuenta" es un soft-delete.** No borra la fila — solo pone `alias = NULL`. Las cookies quedan intactas pero la cuenta deja de aparecer en `list_all()`. Si el usuario vuelve a registrar la misma cuenta, el login reutiliza la sesión guardada sin pedir captcha de nuevo. La única forma de un borrado físico y total es `AuthManager.logout()` (`delete(email)`), pensado para "quiero olvidar esta cuenta por completo", no para el flujo normal de "eliminar cuenta" del menú.

`AccountStore` expone dos interfaces sobre la misma tabla: una orientada a sesión (`get`/`set`/`delete` por email+región, la que usa `AuthManager` sin saber que existe un alias) y una orientada a alias (`set_alias`/`list_all`/`get_by_alias`/`delete_by_alias`/`exists_alias`, la que usa `app.py`).

### `GiftCodeStore` — dos tablas para el sistema de códigos

Ver la sección siguiente, dedicada por completo a esto.

### `session_events` — medición de sesiones

Tercera tabla del mismo archivo, gestionada por `AccountStore`: un registro por cada emisión de token y por cada canje que demuestra si el token servía (con su edad en horas). Es solo medición: ningún flujo depende de ella, y si registrar un evento falla, el login o el canje siguen igual. Se conserva aunque se elimine la cuenta, porque el historial de vida útil sigue siendo útil. Ver [Sesiones y renovación del token](#sesiones-y-renovación-del-token).

## Sistema de códigos de regalo

Dos tablas, `gift_codes` y `code_redemptions`, diseñadas para resolver un problema concreto: **no todos los fallos de canje significan lo mismo.**

- `gift_codes`: el código en sí, el juego al que pertenece, su fecha de expiración (si se conoce) y un estado global (`active` / `dead`).
- `code_redemptions`: qué cuenta puntual ya intentó qué código, y con qué resultado (`success` / `claimed` / `ineligible`).

Cuando un canje falla, el retcode de HoYoverse se clasifica en una de tres categorías (verificadas contra `genshin.py/errors.py`, no inventadas):

| Categoría | Significado | Qué pasa |
|---|---|---|
| **Global** | El código murió para *todas* las cuentas (expirado, formato inválido, tope de usos alcanzado) | Se marca `gift_codes.status = 'dead'`. Ninguna cuenta lo vuelve a intentar. |
| **Por cuenta** | Definitivo, pero solo para *esa* cuenta puntual (ya lo reclamó, o no aplica por región/nivel de personaje) | Se registra en `code_redemptions`. Esa cuenta no lo reintenta, pero el código sigue activo para las demás. |
| **Transitorio** | Temporal (cooldown, código aún no activado, error de red, o un retcode que no reconocemos todavía) | No se guarda nada. La próxima corrida lo vuelve a intentar automáticamente — la ausencia de registro *es* el mecanismo de reintento. |

El canje manual (opción 5 del menú) y el automático (opción 7) comparten toda esta lógica: un canje manual exitoso también queda registrado en `GiftCodeStore`, así que otras cuentas lo aprovechan automáticamente en la próxima corrida del sistema automático, sin que el usuario tenga que repetir el código a mano en cada una.

## Sesiones y renovación del token

El canje de códigos exige una cookie, `cookie_token_v2`, que **caduca**. Check-in y Mimo no la usan, así que pueden funcionar perfecto mientras el canje falla con `retcode -1071` ("Please log in to your account first").

**Por qué había que reingresar la contraseña cada tanto.** El login web devuelve ese token pero no un `stoken` (la credencial duradera; según el propio modelo `WebLoginResult` de `genshin.py`). Sin `stoken` no hay forma de renovarlo: cuando vence, solo queda volver a loguearse.

**Cómo se resuelve ahora.** Al registrar una cuenta OS se hace el *login por app* (`appLoginByPassword`), que sí devuelve el `stoken`. Se guarda, y con él (más el `mid`) se generan `ltoken_v2` y `cookie_token_v2` cuando hacen falta. La contraseña se usa una vez.

| Cuándo | Qué pasa |
|---|---|
| Cada corrida de tareas diarias (opción 4) | Se renueva el token de cada cuenta OS, aunque ese día no haya códigos. Se desactiva con `RENEW_COOKIE_TOKEN_ON_DAILY_TASKS = False`. |
| Un canje devuelve `-1071` | Se renueva el token y se reintenta **una sola vez**. Nunca más de una renovación por cuenta y corrida. |
| La renovación no es posible | Aparece un aviso `[!]` en el resumen: falta `stoken`/`ltmid_v2` (cuentas registradas antes con login web). Se arregla con la opción 8, que ahora hace un login real. |

**El primer login de una cuenta en este dispositivo** pasa por dos pasos que conviene conocer:

1. Un captcha (Geetest v4), que se resuelve en el navegador que se abre.
2. Un código de verificación por email, porque HoYoverse trata a la app como un dispositivo nuevo. El código llega a tu correo y se escribe en la consola. Ahí mismo: `r` reenvía el código (una vez, con un captcha nuevo), Enter vacío cancela, y un código incorrecto se puede reintentar hasta 3 veces.

Si HoYoverse responde que ya envió un código hace poco (`-3206`), no es un error: hay que usar ese código. Después del primer login el `device_id` se conserva y no se vuelve a pedir.

**La contraseña solo vuelve a hacer falta** para cuentas registradas con el login web antiguo, o si el propio `stoken` deja de servir (caso excepcional; el aviso lo señala). En ese caso se repite el flujo de arriba: captcha y, si HoYoverse lo considera un dispositivo nuevo, código por email. El `device_id` se conserva entre logins de la misma cuenta justamente para evitarlo.

### Medición de la vida útil del token

Ni HoYoverse ni `genshin.py` documentan cuánto dura un `cookie_token_v2`, así que el proyecto lo mide. Se guarda un evento en la tabla `session_events` cada vez que:

- se **emite** un token (login o renovación), junto con la edad que tenía el anterior;
- un **canje** demuestra que el token servía, o que ya había vencido, con la edad exacta que tenía en ese momento (se registra el primer resultado por cuenta y corrida).

Además, el log registra (`[MEDICION]`) el vencimiento que el servidor declara en sus respuestas (`Max-Age`/`Expires`, y cualquier campo extra de `getBySToken`), sin el valor secreto.

La opción 9 del menú resume todo: edad actual de cada token, si la cuenta se renueva sola, y el dato importante — *"un token ya venció con X h de edad: no dejes pasar X h entre dos corridas de tareas diarias"*. Mientras no se observe ningún vencimiento, no afirma ningún límite: solo informa cuánto aguantó como mínimo.

### Volver al login web

Si el login por app deja de funcionar (HoYoverse cambia algo), basta con poner `OS_LOGIN_METHOD = "web"` en `config.py`. Las cuentas registradas así funcionan igual que antes, pero sin renovación automática.

## Seguridad anti-baneo

Ninguna llamada HTTP a HoYoverse sale sin que antes haya pasado una espera aleatoria pensada para parecer humana — **incluidas** las llamadas que ocurren "adentro" de una sola operación lógica. El check-in diario, por ejemplo, es en realidad 3 peticiones HTTP (`/info` → `/sign` → `/info` de confirmación); en una auditoría de seguridad se encontró que las 3 se disparaban una detrás de otra sin ninguna pausa, y se corrigió agregando espera entre cada una.

Principios aplicados:

- **Los rangos son generosos, no el mínimo que funciona.** Una llamada a la API es mucho más rápida que cargar una página web real con imágenes y JS — los delays de este proyecto incluyen ese tiempo de "carga de página" encima del tiempo de reacción humana pura.
- **Todo delay declara explícitamente qué transición está simulando** (cambiar de cuenta, cambiar de juego, navegar de la pestaña de tareas a la de tienda del Mimo, etc.), usando un rango de `config.py` dedicado — nunca un número mágico local.
- **El `device_id` es persistente** (se genera una vez en el login y se reutiliza en todos los requests de esa cuenta), no aleatorio en cada petición — simula un dispositivo físico constante en vez de uno que cambia todo el tiempo.
- **Preferimos que el proceso tarde más antes que arriesgar una cuenta** (filosofía explícita del usuario del proyecto, no una conjetura del desarrollo).

Todos los rangos de delay viven en `config.py` (`DELAY_*`) y están documentados ahí con la razón de cada uno. Cada uno tiene, además, un test dedicado que verifica que se dispara con el rango correcto en el punto correcto del código (no solo "que exista algún delay en algún lado").

## Regiones OS y CN

El wrapper soporta dos sistemas de cuentas completamente distintos: **OS** (HoYoLab, cuentas globales) y **CN** (Miyoushe, cuentas chinas). No son variantes del mismo sistema — tienen dominios, cookies y flujos de login diferentes. Esto tiene consecuencias reales sobre qué funciona en cada región:

| Función | OS | CN |
|---|---|---|
| Check-in diario | ✅ | ✅ |
| Mimo Viajero | ✅ | ❌ — no existe en absoluto para CN (es una función de HoYoLab, las cookies de Miyoushe no autenticarían ahí) |
| Canje de códigos | ✅ | ❌ — HoYoverse no expone este endpoint para CN |

Esto no es una limitación de la implementación: está verificado contra el propio `genshin.py` (que tampoco lo soporta para CN) y reflejado explícitamente en el código con `NotImplementedError` en los puntos correspondientes.

## `config.py` — valores que cambian con el tiempo

HoYoverse modifica endpoints, IDs de actividad y salts de firma sin previo aviso. Todo lo que puede cambiar así vive en un único archivo, separado de la lógica, para que corregirlo sea editar una constante en un solo lugar en vez de rastrear el valor por todo el código.

**Esto no es teórico — ya pasó.** Durante el desarrollo, el canje de códigos de Zenless Zone Zero empezó a fallar en vivo con un `405 Method Not Allowed`. La causa: HoYoverse había movido el endpoint de ZZZ a una URL con sufijo `Risk` (que sí acepta `POST`); la URL vieja (sin ese sufijo) solo aceptaba `GET`. Se corrigió releyendo `genshin.py` instalado de cero (no confiando en una revisión anterior) y de paso se encontró que **los tres** dominios de canje habían cambiado, no solo el de ZZZ. Este es exactamente el tipo de cambio para el que existe `config.py`: la próxima vez que algo similar pase, el primer lugar a revisar es ahí, comparando contra `genshin.py` actualizado.

Categorías de valores en `config.py`:

- **URLs**: login, check-in, mimo, canje de códigos, intercambio de tokens.
- **Identificadores por juego**: `act_id` (cambia cada temporada), `GAME_ID_MAP` (para el sistema Mimo), `SIGNGAME`.
- **Salts de firma** (`DS_SALT_*`) y claves RSA de login.
- **Retcodes conocidos**: de captcha, de credenciales, y los tres niveles de clasificación de canje de códigos.
- **Rangos de delay anti-baneo** (`DELAY_*`).
- **Ruta de la base de datos** (`DB_PATH`).

## Instalación y uso

```bash
pip install aiohttp sqlalchemy tenacity cryptography loguru pytest pytest-asyncio aioresponses
python main.py
```

El programa crea/usa un archivo SQLite (por defecto `./hoyo_auth.db`, configurable en `config.DB_PATH`) para guardar cuentas y códigos. Al agregar una cuenta por primera vez se abre el navegador para resolver el captcha de HoYoverse; las siguientes ejecuciones reutilizan la sesión guardada sin pedirlo de nuevo, mientras las cookies sigan siendo válidas.

## Tests

```bash
pytest
```

425 tests, todos ejecutables en pocos segundos (ninguno hace red real — HTTP se intercepta con `aioresponses`, la base de datos usa SQLite en memoria real sin mockear). `tests/conftest.py` documenta, en su encabezado, los principios de testing que sostienen toda la suite:

1. Un test verifica una regla de negocio, no un detalle de implementación.
2. Los mensajes de assert explican qué se esperaba, qué se obtuvo, y por qué importa.
3. Se mockea en el límite de confianza (red, reloj) — nunca el motor de base de datos real ni el objeto bajo prueba.
4. Los errores se clasifican por alcance (global/cuenta/transitorio), con un test explícito por categoría.
5. La idempotencia se prueba explícitamente cuando una operación puede repetirse con los mismos datos.
6. La anti-detección se prueba con números exactos (cuántos delays, con qué rango), no con confianza ciega.
7. Los tests importan las mismas constantes que usa el código bajo prueba (`config.py`) — nunca duplican un valor literal aparte.

Estos principios están pensados para trasladarse a cualquier herramienta futura del mismo autor, en cualquier lenguaje o framework — no son específicos de Python ni de este proyecto puntual.

## Limitaciones conocidas

- **Honkai Impact 3rd**: check-in soportado; sin Mimo Viajero ni canje de códigos vía API (HoYoverse no los expone para este juego).
- **Genshin Impact — Mimo Viajero**: cada cuenta debe configurar el evento **una vez** en HoYoLAB. Mientras no lo haga, las tareas y la tienda del Mimo de Genshin responden `-510001 Parameter Error` aunque la request sea correcta (el check-in no se ve afectado). La herramienta lo reconoce, muestra cómo resolverlo (el enlace está en `config.MIMO_GENSHIN_SETUP_URL`) y no insiste con la tienda. HSR y ZZZ no lo necesitan.
- **Región CN**: sin Mimo Viajero ni canje de códigos (ver [Regiones OS y CN](#regiones-os-y-cn)). El check-in CN sí funciona, incluyendo la firma `ds` correspondiente.
- **Tears of Themis**: no implementado todavía (ver [Roadmap](#roadmap)).
- **Login por app sin validar en vivo todavía**: está portado de `genshin.py` y probado de punta a punta contra HoYoverse *simulado* (cifrado, firma, captcha v4/v3, verificación por email, persistencia), pero no contra el servidor real. Si falla, `OS_LOGIN_METHOD = "web"` vuelve al comportamiento anterior.
- **Vida útil de `cookie_token_v2` y del `stoken`**: desconocida hasta que la medición acumule datos (opción 9).
- **Remote config**: los valores de `config.py` están hardcodeados en el código fuente. La idea original era sincronizarlos desde un backend remoto (Supabase) para poder corregir un cambio de HoYoverse sin tocar el código instalado — no implementado en este MVP de consola.

## Mantenimiento futuro

Cuando algo se rompe en vivo, el orden de diagnóstico recomendado es:

1. Mirar el `error_log_<fecha>.txt` de la ejecución — tiene el traceback completo y el retcode/mensaje crudo de HoYoverse.
2. Comparar el endpoint/valor sospechoso contra `genshin.py` **instalado de nuevo** (`pip install --upgrade genshin`), no contra una revisión anterior de este mismo proyecto ni contra la memoria de quien lo esté depurando — HoYoverse cambia cosas, y `genshin.py` recibe esos cambios antes de que se reflejen en cualquier documentación.
3. Si el valor cambió, corregirlo en `config.py` (es, casi siempre, el único archivo que hace falta tocar).
4. Correr `pytest` para confirmar que nada más se rompió.

## Roadmap

El plan original (documentado desde el inicio del proyecto) es migrar esta lógica, ya validada en vivo, a una aplicación Android — proyecto interno llamado **TaskBlazer** — con Kotlin Multiplatform, Jetpack Compose, SQLDelight, Ktor y Koin. La condición explícita para empezar esa migración era que el MVP de consola estuviera 100% validado en vivo, lo cual ya se cumplió.

Pendiente también, sin fecha definida: soporte para Tears of Themis (juego ya contemplado en `config.GAME_BIZ_MAP` pero sin implementar), y la posibilidad de sincronizar `config.py` desde un backend remoto en vez de tenerlo hardcodeado.

## Créditos y fuente de verdad

Este proyecto no reimplementa el protocolo de HoYoverse desde cero por inspección de tráfico — cada endpoint, salt, y estructura de datos fue verificado contra el código fuente de [`genshin.py`](https://github.com/thesadru/genshin.py), la librería de Python de referencia para la API no oficial de HoYoverse. Cuando este README o cualquier comentario del código dice "verificado contra genshin.py", significa exactamente eso: se instaló el paquete (`pip install genshin`) y se leyó el archivo fuente correspondiente, no se asumió ni se recordó de memoria.
