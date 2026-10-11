"""
El espejo de umbrales del backend (app/firmware.py) coincide con el firmware.

Los umbrales viven en vivero_sensoresV2.ino. Si alguien cambia uno ahí, este
test falla y dice qué corregir en app/firmware.py, para que las alertas
(ALERT-01) anoten el mismo número que usó el Arduino.

El test solo LEE el .ino; nunca lo modifica.
"""

import re
from pathlib import Path

import pytest

from app.firmware import (
    ALERT_METRICS,
    CONSTANTS,
    FAULT_CODES,
    FIRMWARE_PATH,
    LIMIT_ALERTS,
)
from app.models import AlertaEnum, Reading

REPO = Path(__file__).resolve().parents[2]

# const float TEMP_LIMITE_MAX = 30.0;   const unsigned long X = 30UL * 60000;
CONST_RE = re.compile(
    r"^\s*const\s+(?:unsigned\s+)?(?:int|float|long|byte)\s+(\w+)\s*=\s*([^;]+);", re.M
)
# else if (t > TEMP_LIMITE_MAX)
#   alerta(F("TEMP_ALTA"), ALERTA);
CHECK_RE = re.compile(
    r"if\s*\(\s*\w+\s*([<>]=?)\s*([A-Z_]+)\s*\)\s*alerta\(F\(\"([A-Z_]+)\"\),\s*(ALERTA|AVISO)\)"
)


def number(expression: str) -> float:
    """Evalúa una constante simple del .ino: 30.0, 150, 30UL * 60000."""
    value = 1.0
    for factor in expression.split("*"):
        factor = re.sub(r"(?i)[ul]+$", "", factor.strip())
        value *= float(factor)
    return value


def read(path: Path) -> str:
    if not path.exists():
        pytest.skip(f"No está el firmware en {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def firmware_constants(text: str) -> dict:
    """Constantes numéricas del .ino. Las que no son un número simple (por
    ejemplo `const byte OK = 0, AVISO = 1, ALERTA = 2;`) se saltan."""
    found = {}
    for name, expression in CONST_RE.findall(text):
        try:
            found[name] = number(expression)
        except ValueError:
            continue
    return found


def firmware_checks(text: str) -> dict:
    """Código → (comparación, constante, nivel) tal como lo escribe el .ino."""
    return {code: (op, constant, level) for op, constant, code, level in CHECK_RE.findall(text)}


def test_constants_match_firmware():
    found = firmware_constants(read(REPO / FIRMWARE_PATH))
    wrong = {
        name: (expected, found.get(name))
        for name, expected in CONSTANTS.items()
        if found.get(name) != expected
    }
    assert not wrong, (
        "El firmware cambió. Actualiza CONSTANTS en backend/app/firmware.py "
        f"(nombre: (espejo, .ino)): {wrong}"
    )


def test_limit_codes_compare_like_the_firmware():
    """Cada código de límite usa la misma constante, comparación y nivel ALERTA."""
    checks = firmware_checks(read(REPO / FIRMWARE_PATH))
    for code, alert in LIMIT_ALERTS.items():
        assert code in checks, f"{code} ya no aparece en el firmware"
        assert checks[code] == (alert.comparison, alert.constant, "ALERTA"), code


def test_avisos_are_not_limit_alerts():
    """D1: los códigos de nivel AVISO no crean alerta."""
    checks = firmware_checks(read(REPO / FIRMWARE_PATH))
    avisos = {code for code, (_, _, level) in checks.items() if level == "AVISO"}
    assert avisos and not (avisos & set(LIMIT_ALERTS))


def test_mirror_is_consistent_with_the_contract():
    fields = set(Reading.__fields__)
    contract = {code.value for code in AlertaEnum}
    assert set(LIMIT_ALERTS) <= contract
    assert set(FAULT_CODES) <= contract
    assert set(ALERT_METRICS) <= fields
    assert {alert.metric for alert in LIMIT_ALERTS.values()} == set(ALERT_METRICS)
    assert all(alert.direction in ("high", "low") for alert in LIMIT_ALERTS.values())


def test_mirror_test_detects_a_changed_threshold(tmp_path):
    """El test sí falla si alguien cambia un número en el .ino."""
    text = read(REPO / FIRMWARE_PATH).replace(
        "const float TEMP_LIMITE_MAX = 30.0;", "const float TEMP_LIMITE_MAX = 32.0;")
    assert firmware_constants(text)["TEMP_LIMITE_MAX"] == 32.0
    assert firmware_constants(text)["TEMP_LIMITE_MAX"] != CONSTANTS["TEMP_LIMITE_MAX"]
