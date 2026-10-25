"""
Tests del contrato: la app y `openapi.yaml` no se pueden desfasar.

`openapi.yaml` es la fuente oficial (y la copia de SwaggerHub sale de ahí).
Si alguien sube la versión del spec o agrega un endpoint y se le olvida el
otro lado, estos tests fallan. El spec se lee con expresiones regulares
porque el backend no depende de PyYAML.
"""

import re
from pathlib import Path

from fastapi.routing import APIRoute

from app.main import app

SPEC = (Path(__file__).resolve().parents[2] / "openapi.yaml").read_text(
    encoding="utf-8"
)
PREFIX = "/api/v1"


def _spec_version() -> str:
    match = re.search(r"^info:\n(?:  .*\n)*?  version:\s*['\"]?([\w.\-]+)", SPEC, re.M)
    assert match, "openapi.yaml no tiene info.version"
    return match.group(1)


def _spec_operations() -> set:
    paths = SPEC.split("\npaths:\n", 1)[1]
    paths = re.split(r"\n(?=\S)", paths, maxsplit=1)[0]
    ops, current = set(), None
    for line in paths.splitlines():
        path = re.match(r"^  (/\S*):\s*$", line)
        if path:
            current = path.group(1)
            continue
        method = re.match(r"^    (get|post|put|patch|delete):", line)
        if method and current:
            ops.add((method.group(1).upper(), current))
    return ops


def _app_operations() -> set:
    ops = set()
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith(PREFIX):
            for method in route.methods - {"HEAD"}:
                ops.add((method, route.path[len(PREFIX):]))
    return ops


def test_app_version_matches_spec():
    """La versión que reporta FastAPI es la misma que la de openapi.yaml."""
    assert app.version == _spec_version()


def test_every_endpoint_is_documented():
    """Todo endpoint de /api/v1 que existe en la app está en openapi.yaml."""
    missing = _app_operations() - _spec_operations()
    assert not missing, f"Endpoints sin documentar en openapi.yaml: {sorted(missing)}"
