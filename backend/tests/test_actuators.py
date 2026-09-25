"""
Tests de CONF-05 — Register actuators.

Criterios de aceptación:
  1. Un Administrador registra un actuador con tipo (pump / fan / shade /
     light), vivero-área y canal de control → queda disponible en las
     pantallas de control.
  2. Un actuador no registrado → no aparece en la pantalla de control y no
     puede comandarse.
  3. Un actuador con historial se desactiva → desaparece de la pantalla de
     control pero su historial se conserva.
"""

import pytest
from fastapi import HTTPException

from app.routers.actuators import get_active_actuator_or_404
from tests.conftest import GATEWAY_ID, GREENHOUSE_ID, OTHER_GREENHOUSE_ID, bearer

URL = "/api/v1/actuators"


def payload(**overrides) -> dict:
    base = {
        "actuator_id": "bomba-riego-01",
        "name": "Bomba de riego",
        "type": "pump",
        "greenhouse_id": GREENHOUSE_ID,
        "area": "general",
        "control_channel": "L298N-B/D5",
        "gateway_device_id": GATEWAY_ID,
        "model": "Bomba sumergible 12 V",
    }
    base.update(overrides)
    return base


async def register(api, token, **overrides):
    return await api.post(URL, json=payload(**overrides), headers=bearer(token))


async def count_actuators(db) -> int:
    cursor = await db.execute("SELECT COUNT(*) AS n FROM actuators")
    return (await cursor.fetchone())["n"]


def ids(resp) -> set:
    return {a["actuator_id"] for a in resp.json()}


# ── Criterio 1: registrar y quedar disponible ──────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "super_admin"])
async def test_register_actuator_ok(api, db, tokens, role):
    """Caso 1: Admin y Super Admin registran → 201 con los datos guardados."""
    resp = await register(api, tokens[role])
    assert resp.status_code == 201
    data = resp.json()
    assert data["actuator_id"] == "bomba-riego-01"
    assert data["type"] == "pump"
    assert data["area"] == "general"
    assert data["control_channel"] == "L298N-B/D5"
    assert data["status"] == "active"
    assert data["deactivated_at"] is None
    cursor = await db.execute("SELECT id FROM users WHERE role = ?", (role,))
    assert data["registered_by"] == (await cursor.fetchone())["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("actuator_type", ["pump", "fan", "shade", "light"])
async def test_all_actuator_types_accepted(api, tokens, actuator_type):
    """Caso 2: los cuatro tipos del criterio se aceptan."""
    resp = await register(
        api, tokens["admin"],
        actuator_id=f"{actuator_type}-01",
        type=actuator_type,
        control_channel=f"CH-{actuator_type}",
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_registered_actuator_listed_for_control_screens(api, tokens):
    """Caso 3: lo registrado aparece en la lista que usan las pantallas de control."""
    await register(api, tokens["admin"])
    for role in ("producer", "admin", "super_admin"):
        resp = await api.get(URL, headers=bearer(tokens[role]))
        assert resp.status_code == 200
        assert ids(resp) == {"bomba-riego-01"}


@pytest.mark.asyncio
async def test_list_filters(api, tokens):
    """Caso 4: filtros por tipo y área."""
    await register(api, tokens["admin"])
    await register(api, tokens["admin"], actuator_id="vent-01", type="fan",
                   control_channel="L298N-A/D3", area="cama-2")
    admin = bearer(tokens["admin"])
    assert ids(await api.get(URL, params={"type": "fan"}, headers=admin)) == {"vent-01"}
    assert ids(await api.get(URL, params={"area": "general"}, headers=admin)) == {"bomba-riego-01"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"type": "calefactor"},             # tipo fuera del catálogo
        {"area": None},                     # falta el área
        {"greenhouse_id": None},            # falta el vivero
        {"control_channel": None},          # falta el canal
        {"actuator_id": "Bomba Riego"},     # id que no es slug
        {"name": "   "},                    # nombre vacío
    ],
)
async def test_invalid_payload_422(api, db, tokens, overrides):
    """Caso 5: datos inválidos → 422 y no se guarda nada."""
    resp = await register(api, tokens["admin"], **overrides)
    assert resp.status_code == 422
    assert await count_actuators(db) == 0


@pytest.mark.asyncio
async def test_duplicate_id_409(api, tokens):
    """Caso 6: registrar el mismo id dos veces → 409 de duplicado."""
    assert (await register(api, tokens["admin"])).status_code == 201
    resp = await register(api, tokens["admin"], control_channel="OTRO")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "actuator_already_exists"


@pytest.mark.asyncio
async def test_channel_in_use_409(api, tokens):
    """Caso 7: dos actuadores activos no comparten canal en el mismo vivero."""
    await register(api, tokens["admin"])
    resp = await register(api, tokens["admin"], actuator_id="bomba-riego-02")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "control_channel_in_use"


@pytest.mark.asyncio
async def test_same_channel_other_greenhouse_ok(api, tokens):
    """Caso 8: el mismo canal en OTRO vivero sí se permite."""
    await register(api, tokens["admin"])
    resp = await register(api, tokens["admin"], actuator_id="bomba-otro-01",
                          greenhouse_id=OTHER_GREENHOUSE_ID, gateway_device_id=None)
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_unknown_gateway_404(api, tokens):
    """Caso 9: un gateway que no está registrado → 404."""
    resp = await register(api, tokens["admin"], gateway_device_id="pi-fantasma-99")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "gateway_not_registered"


@pytest.mark.asyncio
async def test_gateway_from_other_greenhouse_422(api, tokens):
    """Caso 10: el gateway debe pertenecer al mismo vivero del actuador."""
    resp = await register(api, tokens["admin"], greenhouse_id=OTHER_GREENHOUSE_ID)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "gateway_greenhouse_mismatch"


# ── Criterio 2: no registrado → no aparece ni se comanda ───────────────

@pytest.mark.asyncio
async def test_unknown_actuator_not_listed_and_404(api, tokens):
    """Caso 11: un actuador no registrado no está en la lista y da 404."""
    admin = bearer(tokens["admin"])
    assert (await api.get(URL, headers=admin)).json() == []
    resp = await api.get(f"{URL}/bomba-fantasma-01", headers=admin)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_command_gate_rejects_unregistered_and_inactive(api, db, tokens):
    """Caso 12: la puerta de comandos (ACT-02/04) rechaza lo no registrado o inactivo."""
    with pytest.raises(HTTPException) as exc:
        await get_active_actuator_or_404(db, "bomba-fantasma-01")
    assert exc.value.status_code == 404

    await register(api, tokens["admin"])
    assert (await get_active_actuator_or_404(db, "bomba-riego-01"))["type"] == "pump"

    await api.patch(f"{URL}/bomba-riego-01", json={"status": "inactive"},
                    headers=bearer(tokens["admin"]))
    with pytest.raises(HTTPException) as exc:
        await get_active_actuator_or_404(db, "bomba-riego-01")
    assert exc.value.detail["error"] == "actuator_not_available"


# ── Criterio 3: desactivar sin perder historial ────────────────────────

@pytest.mark.asyncio
async def test_deactivate_hides_but_preserves(api, db, tokens):
    """Caso 13: desactivado → fuera de la lista por defecto, pero el registro sigue."""
    await register(api, tokens["admin"])
    admin = bearer(tokens["admin"])

    resp = await api.patch(f"{URL}/bomba-riego-01", json={"status": "inactive"}, headers=admin)
    assert resp.status_code == 200
    assert resp.json()["status"] == "inactive"
    assert resp.json()["deactivated_at"] is not None

    # Ya no aparece en la pantalla de control
    assert (await api.get(URL, headers=admin)).json() == []
    assert (await api.get(URL, headers=bearer(tokens["producer"]))).json() == []

    # Pero se conserva: sigue en la BD y un admin puede consultarlo
    assert await count_actuators(db) == 1
    inactive = await api.get(URL, params={"status": "inactive"}, headers=admin)
    assert ids(inactive) == {"bomba-riego-01"}
    assert (await api.get(f"{URL}/bomba-riego-01", headers=admin)).status_code == 200


@pytest.mark.asyncio
async def test_no_delete_endpoint(api, tokens):
    """Caso 14: no existe DELETE: los actuadores nunca se borran físicamente."""
    await register(api, tokens["admin"])
    resp = await api.delete(f"{URL}/bomba-riego-01", headers=bearer(tokens["super_admin"]))
    assert resp.status_code == 405


@pytest.mark.asyncio
async def test_deactivation_frees_channel_and_reactivation_checks_it(api, tokens):
    """Caso 15: al desactivar se libera el canal; reactivar valida que siga libre."""
    admin = bearer(tokens["admin"])
    await register(api, tokens["admin"])
    await api.patch(f"{URL}/bomba-riego-01", json={"status": "inactive"}, headers=admin)

    # El canal quedó libre para un equipo nuevo
    assert (await register(api, tokens["admin"], actuator_id="bomba-nueva-01")).status_code == 201

    # Reactivar la vieja choca con la nueva
    resp = await api.patch(f"{URL}/bomba-riego-01", json={"status": "active"}, headers=admin)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "control_channel_in_use"


@pytest.mark.asyncio
async def test_reactivate_ok(api, tokens):
    """Caso 16: reactivar un actuador con canal libre → vuelve a la lista."""
    admin = bearer(tokens["admin"])
    await register(api, tokens["admin"])
    await api.patch(f"{URL}/bomba-riego-01", json={"status": "inactive"}, headers=admin)
    resp = await api.patch(f"{URL}/bomba-riego-01", json={"status": "active"}, headers=admin)
    assert resp.status_code == 200
    assert resp.json()["deactivated_at"] is None
    assert ids(await api.get(URL, headers=admin)) == {"bomba-riego-01"}


@pytest.mark.asyncio
async def test_patch_edit_fields(api, tokens):
    """Caso 17: se pueden editar nombre, área, canal y modelo."""
    await register(api, tokens["admin"])
    resp = await api.patch(
        f"{URL}/bomba-riego-01",
        json={"name": "Bomba principal", "area": "cama-1", "control_channel": "L298N-B/D6"},
        headers=bearer(tokens["admin"]),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert (data["name"], data["area"], data["control_channel"]) == (
        "Bomba principal", "cama-1", "L298N-B/D6")
    assert data["updated_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,error",
    [
        ({"type": "fan"}, None),                         # type no es editable
        ({"greenhouse_id": OTHER_GREENHOUSE_ID}, None),  # vivero no es editable
        ({}, "nothing_to_update"),
        ({"name": None}, "invalid_null"),
    ],
)
async def test_patch_rejections_422(api, tokens, body, error):
    """Caso 18: PATCH rechaza campos no editables, cuerpo vacío y nulls."""
    await register(api, tokens["admin"])
    resp = await api.patch(f"{URL}/bomba-riego-01", json=body,
                           headers=bearer(tokens["admin"]))
    assert resp.status_code == 422
    if error:
        assert resp.json()["detail"]["error"] == error


@pytest.mark.asyncio
async def test_patch_unknown_404(api, tokens):
    """Caso 19: editar un actuador inexistente → 404."""
    resp = await api.patch(f"{URL}/no-existe-01", json={"name": "x"},
                           headers=bearer(tokens["admin"]))
    assert resp.status_code == 404


# ── AUTH-02 aplicado a rutas reales de CONF-05 ─────────────────────────

@pytest.mark.asyncio
async def test_producer_cannot_register_actuator(api, db, tokens):
    """Caso 20: un productor → POST de admin → 403 y NO se guarda nada."""
    resp = await register(api, tokens["producer"])
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "forbidden"
    assert await count_actuators(db) == 0


@pytest.mark.asyncio
async def test_producer_cannot_patch_actuator(api, db, tokens):
    """Caso 21: un productor → PATCH → 403 y el actuador queda igual."""
    await register(api, tokens["admin"])
    resp = await api.patch(f"{URL}/bomba-riego-01", json={"status": "inactive"},
                           headers=bearer(tokens["producer"]))
    assert resp.status_code == 403
    cursor = await db.execute("SELECT status FROM actuators")
    assert (await cursor.fetchone())["status"] == "active"


@pytest.mark.asyncio
async def test_producer_cannot_list_inactive(api, tokens):
    """Caso 22: un productor no puede pedir actuadores desactivados."""
    resp = await api.get(URL, params={"status": "inactive"},
                         headers=bearer(tokens["producer"]))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_producer_only_sees_own_greenhouse(api, tokens):
    """Caso 23: un productor no ve actuadores de otro vivero."""
    await register(api, tokens["admin"])
    await register(api, tokens["admin"], actuator_id="bomba-otro-01",
                   greenhouse_id=OTHER_GREENHOUSE_ID, gateway_device_id=None)
    producer = bearer(tokens["producer"])

    assert ids(await api.get(URL, headers=producer)) == {"bomba-riego-01"}
    assert (await api.get(f"{URL}/bomba-otro-01", headers=producer)).status_code == 404
    resp = await api.get(URL, params={"greenhouse_id": OTHER_GREENHOUSE_ID}, headers=producer)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_producer_gets_404_for_inactive(api, tokens):
    """Caso 24: para un productor, un actuador desactivado no existe."""
    await register(api, tokens["admin"])
    await api.patch(f"{URL}/bomba-riego-01", json={"status": "inactive"},
                    headers=bearer(tokens["admin"]))
    resp = await api.get(f"{URL}/bomba-riego-01", headers=bearer(tokens["producer"]))
    assert resp.status_code == 404
