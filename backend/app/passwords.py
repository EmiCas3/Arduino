"""
Revisión de la migración de contraseñas (NFR-03).

Muestra con qué esquema está guardada la contraseña de cada cuenta y cuáles
faltan por migrar al preferido de esta instalación. No cambia nada: una
cuenta se migra sola la siguiente vez que inicia sesión (ver app/security.py).

Uso:
    cd backend
    python -m app.passwords

Sale con código 1 si alguna cuenta tiene algo que NO es un hash con salt
conocido (por ejemplo, una contraseña en texto plano metida a mano en la BD).
"""

import asyncio
import sys
from typing import List, Tuple

import aiosqlite

from app.database import close_db, init_db
from app.security import needs_rehash, preferred_scheme, scheme_of


async def review(db: aiosqlite.Connection) -> List[Tuple[str, str, str]]:
    """[(usuario, esquema o 'invalid', 'ok' | 'pending' | 'invalid')]."""
    cursor = await db.execute("SELECT username, password_hash FROM users ORDER BY username")
    result = []
    for row in await cursor.fetchall():
        scheme = scheme_of(row["password_hash"])
        if scheme is None:
            result.append((row["username"], "invalid", "invalid"))
        else:
            state = "pending" if needs_rehash(row["password_hash"]) else "ok"
            result.append((row["username"], scheme, state))
    return result


async def main() -> int:
    db = await init_db()
    try:
        rows = await review(db)
    finally:
        await close_db()

    labels = {
        "ok": "al día",
        "pending": "se migra en su siguiente inicio de sesión",
        "invalid": "NO es un hash válido: hay que recrear la cuenta",
    }
    print(f"Esquema preferido en esta instalación: {preferred_scheme()}")
    for username, scheme, state in rows:
        print(f"  {username:<16} {scheme:<14} {labels[state]}")
    pending = sum(1 for row in rows if row[2] == "pending")
    invalid = sum(1 for row in rows if row[2] == "invalid")
    print(f"{len(rows)} cuentas: {len(rows) - pending - invalid} al día, "
          f"{pending} por migrar, {invalid} inválidas.")
    return 1 if invalid else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
