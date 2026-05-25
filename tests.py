"""
tests.py
--------
Runner principal del suite de tests del proyecto HoYoWrapper.

Salidas:
  - Consola:        loguru, resumen visual por modulo y global.
  - test_output.txt output COMPLETO de pytest por modulo, incluyendo
                    todos los tracebacks tal como los genera pytest,
                    sin ningun parser intermedio que pueda perder informacion.

Estrategia de captura:
  pytest se ejecuta con --tb=long y -v. Su output completo se escribe
  directamente en el .txt. El runner solo parsea los totales (passed/failed)
  para el resumen de consola — nada mas.

Uso:
    python tests.py                    # Todos los tests
    python tests.py -v                 # Muestra el output completo en consola tambien
    python tests.py --module cookie    # Solo el modulo indicado
    python tests.py -k device_id       # Filtrar por nombre de test

Requisitos:
    pip install pytest pytest-asyncio aioresponses loguru
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys

from loguru import logger


# ---------------------------------------------------------------------------
# Configuracion de loguru
# ---------------------------------------------------------------------------

logger.remove()
logger.add(
    sys.stdout,
    colorize=True,
    format=(
        "<dim>{time:HH:mm:ss}</dim> "
        "<level>{level: <8}</level> "
        "{message}"
    ),
    level="DEBUG",
)


# ---------------------------------------------------------------------------
# Modulos de test
# ---------------------------------------------------------------------------

TEST_MODULES: dict[str, tuple[str, str]] = {
    "cookie":   ("tests/test_cookie_store.py",   "db.CookieStore - Persistencia y seguridad"),
    "account":  ("tests/test_account_store.py",  "db.AccountStore - Cuentas con alias"),
    "session":  ("tests/test_session.py",         "HoyoSession - Sesion autenticada"),
    "game":     ("tests/test_game_model.py",       "BaseGame / Juegos - Check-in, Mimo, Anti-deteccion"),
    "auth":     ("tests/test_auth_manager.py",     "AuthManager - Login y flujos de autenticacion"),
    "manager":  ("tests/test_session_manager.py",  "HoyoManager / ActiveSession - Orquestacion"),
    "app":      ("tests/test_app.py",              "App - Logica de negocio del main"),
}

OUTPUT_TXT = "test_output.txt"
SEP     = "-" * 70
SEP_FAT = "=" * 70


# ---------------------------------------------------------------------------
# Ejecucion de un modulo
# ---------------------------------------------------------------------------

def run_module(path: str, label: str, extra_args: list[str]) -> dict:
    """
    Ejecuta un modulo de test con pytest.

    Captura stdout/stderr como bytes y decodifica con utf-8 + reemplazo
    para evitar UnicodeDecodeError en Windows (cp1252 vs utf-8).
    """
    cmd = [
        sys.executable, "-m", "pytest",
        path,
        "--tb=long",    # Tracebacks completos
        "-v",           # Un test por linea
        "--no-header",
        "--color=no",   # Sin ANSI en el output capturado
        *extra_args,
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )

    # Decodificar bytes manualmente — evita UnicodeDecodeError en Windows
    stdout = (result.stdout or b"").decode("utf-8", errors="replace")
    stderr = (result.stderr or b"").decode("utf-8", errors="replace")

    # Normalizar CRLF -> LF
    raw = (stdout + stderr).replace("\r\n", "\n").replace("\r", "\n")

    # Parsear solo los totales para el resumen de consola
    passed, failed, errors, warnings = _parse_totals(raw)

    # Extraer lista simple de tests fallidos (nombre + linea de error)
    # para el resumen de consola — sin parsear tracebacks completos
    failed_names = _parse_failed_names(raw)

    return {
        "label":        label,
        "path":         path,
        "passed":       passed,
        "failed":       failed,
        "errors":       errors,
        "warnings":     warnings,
        "failed_names": failed_names,   # lista de str con nombre del test
        "raw":          raw,            # output completo de pytest sin tocar
        "returncode":   result.returncode,
    }


def _parse_totals(output: str) -> tuple[int, int, int, int]:
    """Extrae passed/failed/errors/warnings de la ultima linea de pytest."""
    passed = failed = errors = warnings = 0
    for line in output.splitlines():
        low = line.lower()
        if any(w in low for w in ("passed", "failed", "error")):
            for part in line.split(","):
                p = part.strip()
                if "passed"  in p: passed   = _num(p)
                if "failed"  in p: failed   = _num(p)
                if "error"   in p: errors   = _num(p)
                if "warning" in p: warnings = _num(p)
    return passed, failed, errors, warnings


def _parse_failed_names(output: str) -> list[str]:
    """
    Extrae los nombres de los tests fallidos de la seccion
    'short test summary info' de pytest (lineas que empiezan con 'FAILED').
    Es la forma mas robusta: pytest siempre escribe esa seccion al final.
    """
    names = []
    for line in output.splitlines():
        # Formato: "FAILED tests/test_x.py::Class::test_name - AssertionError: ..."
        if line.startswith("FAILED "):
            # Tomar solo el nombre del test, sin el mensaje de error
            part = line[len("FAILED "):].split(" - ")[0].strip()
            # Acortar a solo el nombre del metodo para la consola
            short = part.split("::")[-1] if "::" in part else part
            names.append(short)
    return names


def _num(text: str) -> int:
    m = re.search(r"\d+", text)
    return int(m.group()) if m else 0


# ---------------------------------------------------------------------------
# Salida en consola con loguru
# ---------------------------------------------------------------------------

def log_module_result(r: dict) -> None:
    """Muestra el resultado de un modulo: una linea de estado y los tests fallidos."""
    ok = r["failed"] == 0 and r["errors"] == 0

    if ok:
        logger.success(
            f"[{r['path']}]  {r['passed']} pasados  |  0 fallidos"
        )
    else:
        logger.error(
            f"[{r['path']}]  "
            f"{r['passed']} pasados  |  "
            f"{r['failed']} fallidos  |  {r['errors']} errores"
        )
        for name in r["failed_names"]:
            logger.warning(f"  x  {name}")


def log_summary(results: list[dict]) -> None:
    """Resumen global en consola al final de la ejecucion."""
    total_passed = sum(r["passed"] for r in results)
    total_failed = sum(r["failed"] + r["errors"] for r in results)
    all_ok       = total_failed == 0

    logger.info(SEP)
    logger.info("RESUMEN FINAL")
    logger.info(SEP)

    for r in results:
        ok    = r["failed"] == 0 and r["errors"] == 0
        kos   = r["failed"] + r["errors"]
        icon  = "OK" if ok else "XX"
        label = r["label"].split("-")[0].strip()
        detail = f"{r['passed']} ok" if ok else f"{r['passed']} ok / {kos} ko"
        if ok:
            logger.success(f"  [{icon}]  {label:<50} {detail}")
        else:
            logger.error(  f"  [{icon}]  {label:<50} {detail}")

    logger.info(SEP)
    if all_ok:
        logger.success(
            f"TODOS LOS TESTS PASARON - "
            f"{total_passed} tests en {len(results)} modulos"
        )
    else:
        logger.error(
            f"HAY TESTS FALLIDOS - "
            f"{total_passed} pasados / {total_failed} fallidos/errores"
        )
        logger.info("")
        logger.info("Los tracebacks completos estan en: test_output.txt")
    logger.info(SEP)


# ---------------------------------------------------------------------------
# Reporte .txt — output completo de pytest sin intermediarios
# ---------------------------------------------------------------------------

def write_txt_report(
    results: list[dict],
    run_at: datetime.datetime,
    modules_run: list[str],
    filter_expr: str | None,
) -> None:
    """
    Escribe test_output.txt con el output RAW completo de pytest por modulo.

    Estructura:
      [REPORT_START]        metadatos globales
      [MODULE_START] ...    por cada modulo: metadatos + output completo de pytest
        [PYTEST_OUTPUT]     output de pytest sin modificar (tracebacks incluidos)
        [PYTEST_OUTPUT_END]
      [MODULE_END]
      [SUMMARY]             tabla resumen final
      [REPORT_END]

    El output de pytest dentro de [PYTEST_OUTPUT] contiene exactamente lo que
    pytest imprimiria en consola con --tb=long -v, incluyendo todos los
    tracebacks, assertions fallidas y mensajes de error.
    """
    lines: list[str] = []

    def w(text: str = "") -> None:
        lines.append(text)

    total_passed = sum(r["passed"] for r in results)
    total_failed = sum(r["failed"] + r["errors"] for r in results)
    all_ok       = total_failed == 0

    # Cabecera
    w("[REPORT_START]")
    w(f"timestamp:      {run_at.isoformat()}")
    w(f"project:        HoYoWrapper")
    w(f"modules_run:    {', '.join(modules_run)}")
    w(f"filter:         {filter_expr or 'none'}")
    w(f"total_tests:    {total_passed + total_failed}")
    w(f"total_passed:   {total_passed}")
    w(f"total_failed:   {total_failed}")
    w(f"global_status:  {'PASS' if all_ok else 'FAIL'}")
    w()

    # Modulos
    for r in results:
        ok  = r["failed"] == 0 and r["errors"] == 0
        kos = r["failed"] + r["errors"]

        w("[MODULE_START]")
        w(f"label:    {r['label']}")
        w(f"path:     {r['path']}")
        w(f"status:   {'PASS' if ok else 'FAIL'}")
        w(f"passed:   {r['passed']}")
        w(f"failed:   {r['failed']}")
        w(f"errors:   {r['errors']}")
        w(f"warnings: {r['warnings']}")
        w()

        # Output completo de pytest — aqui estan los tracebacks
        w("[PYTEST_OUTPUT]")
        for line in r["raw"].splitlines():
            w(line)
        w("[PYTEST_OUTPUT_END]")

        w("[MODULE_END]")
        w()

    # Resumen
    w("[SUMMARY]")
    for r in results:
        ok  = r["failed"] == 0 and r["errors"] == 0
        kos = r["failed"] + r["errors"]
        w(f"  {'PASS' if ok else 'FAIL'}  {r['label']:<60}  "
          f"passed={r['passed']}  failed={kos}")
    w()
    w(f"global_status: {'PASS - all tests passed' if all_ok else 'FAIL - some tests failed'}")
    w("[REPORT_END]")

    txt_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), OUTPUT_TXT
    )
    with open(txt_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    logger.info(f"Reporte escrito en: {txt_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Runner de tests - HoYoWrapper.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modulos (para --module):
  cookie    CookieStore - persistencia y seguridad
  session   HoyoSession - sesion autenticada
  game      BaseGame / juegos - check-in, mimo, anti-deteccion
  auth      AuthManager - login, fallback OS->CN
  manager   HoyoManager / ActiveSession - orquestacion

Ejemplos:
  python tests.py
  python tests.py --module cookie
  python tests.py -k device_id
  python tests.py -v
        """,
    )
    parser.add_argument("--module", "-m", choices=list(TEST_MODULES.keys()),
                        help="Ejecutar solo el modulo indicado.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Mostrar output completo de pytest en consola tambien.")
    parser.add_argument("-k", metavar="EXPR",
                        help="Filtrar tests por nombre.")
    args = parser.parse_args()

    modules_to_run = (
        {args.module: TEST_MODULES[args.module]} if args.module else TEST_MODULES
    )
    extra_args = ["-k", args.k] if args.k else []
    run_at     = datetime.datetime.now()

    logger.info(SEP_FAT)
    logger.info("HoYoWrapper - Suite de Tests")
    logger.info(SEP_FAT)
    logger.info(
        f"Modulos: {len(modules_to_run)}  |  "
        f"Filtro: {args.k or 'ninguno'}  |  "
        f"Inicio: {run_at.strftime('%H:%M:%S')}"
    )

    results = []
    for key, (path, label) in modules_to_run.items():
        logger.info("")
        logger.info(f"Ejecutando: {label}")
        logger.info(f"  {path}")

        r = run_module(path, label, extra_args)
        results.append(r)

        if args.verbose:
            # Mostrar el output completo de pytest en consola tambien
            print(r["raw"])
        else:
            log_module_result(r)

    logger.info("")
    log_summary(results)

    write_txt_report(
        results,
        run_at=run_at,
        modules_run=list(modules_to_run.keys()),
        filter_expr=args.k,
    )

    return 0 if all(r["returncode"] == 0 for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
