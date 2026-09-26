"""
Gateway de la Raspberry Pi (MON-03).

Lee las líneas JSON del Arduino por Serial, les pone la hora UTC de la Pi,
las guarda en un buffer SQLite y las manda por lotes al endpoint de MON-04
(POST /devices/{device_id}/readings). Nada se borra hasta que el backend
responde 202.
"""

__version__ = "1.0.0"
