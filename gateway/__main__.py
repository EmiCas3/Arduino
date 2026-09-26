"""
Gateway de la Raspberry Pi (MON-03).

Uso (desde la carpeta raíz del repo, la que contiene gateway/):

    python -m gateway run                 lee el Arduino y envía al backend
    python -m gateway status              pendientes, enviadas, reloj, errores
    python -m gateway simulate            igual que run, con un Arduino simulado
    python -m gateway simulate --print    solo imprime líneas simuladas

La configuración vive en gateway/.env (ver .env.example).
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from typing import Callable, Optional

from gateway import __version__
from gateway.buffer import Buffer
from gateway.clock import Clock, utc_ms
from gateway.config import Config, ConfigError, load_config
from gateway.sender import Dispatcher
from gateway.serial_reader import SerialReader, find_port, open_serial

log = logging.getLogger("gateway")

HEARTBEAT_SECONDS = 15.0
PURGE_EVERY_SECONDS = 3600.0
LOOP_SECONDS = 0.5


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def _utf8_console() -> None:
    """La consola de Windows (cp1252) no imprime bien los acentos ni '·'."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


# ── run / simulate ─────────────────────────────────────────────────────

def write_heartbeat(buf: Buffer, clock: Clock, reader: Optional[SerialReader]) -> None:
    """Deja en el buffer lo que `status` necesita saber del proceso `run`.

    Los momentos se guardan como 'boot_id ms_desde_el_arranque' porque la hora
    del sistema puede no ser confiable (reloj sin sincronizar).
    """
    last_line = None
    if reader is not None and reader.last_line_at is not None:
        age_ms = int((time.monotonic() - reader.last_line_at) * 1000)
        last_line = f"{clock.boot_id} {clock.boot_ms() - age_ms}"
    buf.set_meta(
        latido=f"{clock.boot_id} {clock.boot_ms()}",
        lineas_validas=str(reader.valid_lines if reader else 0),
        lineas_invalidas=str(reader.invalid_lines if reader else 0),
        ultima_linea=last_line,
    )


def run_gateway(
    cfg: Config,
    *,
    open_port: Callable = open_serial,
    find: Callable[[str], Optional[str]] = find_port,
    clock: Optional[Clock] = None,
    stop: Optional[threading.Event] = None,
) -> int:
    cfg.require_api_key()
    clock = clock or Clock(cfg.clock)
    stop = stop or threading.Event()

    log.info("Gateway SmartGreenAI %s → %s (buffer: %s, reloj: %s)",
             __version__, cfg.ingest_url, cfg.buffer_db, clock.mode)
    if clock.is_synced(force=True):
        log.info("Reloj sincronizado: %s", utc_ms(clock.time()))
    else:
        log.warning("El reloj NO está sincronizado (¿sin internet o sin NTP?). Las lecturas "
                    "se guardan sin hora y se envían en cuanto sincronice.")

    # El buffer se crea aquí, antes de arrancar el hilo del Serial.
    buf = Buffer(cfg.buffer_db)
    dispatcher = Dispatcher(cfg, buf, clock)
    reader_state = {}

    def read_serial() -> None:
        reader_buf = None
        try:
            reader_buf = Buffer(cfg.buffer_db)   # conexión propia para este hilo

            def on_reading(payload: dict) -> None:
                ts, now_boot = clock.stamp()
                reader_buf.add(payload, ts, clock.boot_id, now_boot)

            reader = SerialReader(on_reading, port=cfg.serial_port, baud=cfg.baud,
                                  open_port=open_port, find=find, stop=stop)
            reader_state["reader"] = reader
            reader.run()
        except Exception:
            log.exception("El lector del Serial se detuvo por un error inesperado.")
            reader_state["crashed"] = True
            stop.set()
        finally:
            if reader_buf is not None:
                reader_buf.close()

    thread = threading.Thread(target=read_serial, name="serial", daemon=True)
    thread.start()
    last_heartbeat = last_purge = float("-inf")
    try:
        while not stop.is_set():
            now = time.monotonic()
            if clock.is_synced():
                timed = buf.resolve_untimed(clock.boot_id, clock.time(), clock.boot_ms())
                if timed:
                    log.info("Se calculó la hora de %d lecturas que llegaron sin hora.", timed)
                if now - last_purge >= PURGE_EVERY_SECONDS:
                    purged = buf.purge_sent(cfg.purge_days)
                    if purged:
                        log.info("Se purgaron %d lecturas enviadas hace más de %d días.",
                                 purged, cfg.purge_days)
                    last_purge = now
            dispatcher.tick()
            if now - last_heartbeat >= HEARTBEAT_SECONDS:
                write_heartbeat(buf, clock, reader_state.get("reader"))
                last_heartbeat = now
            stop.wait(LOOP_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        thread.join(timeout=5)
        counts = buf.stats()["counts"]
        write_heartbeat(buf, clock, reader_state.get("reader"))
        buf.set_meta(latido=None)   # `status` dirá "detenido"
        buf.close()
        log.info("Gateway detenido. Pendientes: %d, sin hora: %d (se conservan en el buffer).",
                 counts["pendiente"], counts["sin_hora"])
    # Código 1 si el lector falló: systemd reinicia el servicio.
    return 1 if reader_state.get("crashed") else 0


# ── status ─────────────────────────────────────────────────────────────

def _age(meta_value: Optional[str], clock: Clock) -> Optional[float]:
    """Segundos desde un momento guardado como 'boot_id ms'. None si es de otro arranque."""
    if not meta_value:
        return None
    try:
        saved_boot, saved_ms = meta_value.split()
        saved_ms = int(saved_ms)
    except ValueError:
        return None
    if saved_boot != clock.boot_id:
        return None
    return max(0.0, (clock.boot_ms() - saved_ms) / 1000)


def print_status(cfg: Config, clock: Optional[Clock] = None, out=print) -> int:
    clock = clock or Clock(cfg.clock)
    row = "  {:<21}{}".format
    out(f"SmartGreenAI · gateway {__version__} (MON-03)")
    out(row("Backend", f"{cfg.api_url} · device {cfg.device_id}"))
    out(row("Buffer", str(cfg.buffer_db)))
    if not cfg.buffer_db.exists():
        out(row("Estado", "todavía no hay buffer: el gateway no ha corrido."))
        return 0

    buf = Buffer(cfg.buffer_db)
    try:
        stats = buf.stats(clock.boot_id)
        meta = buf.get_meta()
    finally:
        buf.close()

    latido = _age(meta.get("latido"), clock)
    if latido is not None and latido < HEARTBEAT_SECONDS * 3:
        servicio = f"corriendo (latido hace {latido:.0f} s)"
    else:
        servicio = "detenido o sin responder"
    out(row("Servicio", servicio))

    if clock.is_synced(force=True):
        out(row("Reloj", f"sincronizado ({clock.mode}) · {utc_ms(clock.time())}"))
    else:
        out(row("Reloj", "SIN sincronizar: las lecturas esperan hora antes de enviarse"))

    ultima = _age(meta.get("ultima_linea"), clock)
    out(row("Última línea", "—" if ultima is None else f"hace {ultima:.0f} s"))
    lineas = meta.get("lineas_validas") or "0"
    invalidas = meta.get("lineas_invalidas") or "0"
    out(row("Líneas del Arduino", f"{lineas} válidas, {invalidas} ignoradas (desde que arrancó el servicio)"))

    counts = stats["counts"]
    oldest = stats["oldest_pending"]
    out(row("Pendientes", f"{counts['pendiente']}" + (f" (la más vieja: {oldest})" if oldest else "")))
    sin_hora = f"{counts['sin_hora']}"
    if stats["untimed_other_boot"]:
        sin_hora += (f" ({stats['untimed_other_boot']} de un arranque anterior: no se pueden "
                     "fechar sin inventar la hora, se conservan)")
    out(row("Sin hora", sin_hora))
    out(row("Enviadas", f"{counts['enviada']} (se guardan {cfg.purge_days} días)"))
    out(row("Rechazadas", f"{counts['rechazada']}"))
    for ts, motivo in stats["last_rejected"]:
        out(row("", f"· {ts}: {motivo}"))

    ok = meta.get("ultimo_envio_ok")
    resumen = meta.get("ultimo_envio_resumen")
    out(row("Último envío OK", (ok or "—") + (f" ({resumen})" if ok and resumen else "")))
    error = meta.get("ultimo_error")
    if error:
        cuando = meta.get("ultimo_error_en") or "hora desconocida"
        out(row("Último error", f"{cuando}: {error}"))
    else:
        out(row("Último error", "—"))
    return 0


# ── CLI ────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m gateway",
        description="Gateway SmartGreenAI (MON-03): Arduino → buffer SQLite → backend.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Lee el Arduino y envía las lecturas al backend")
    sub.add_parser("status", help="Muestra pendientes, enviadas, rechazadas y el reloj")
    sim = sub.add_parser("simulate", help="Como run, pero con un Arduino simulado")
    sim.add_argument("--print", action="store_true", dest="only_print",
                     help="Solo imprime las líneas simuladas; no envía nada")
    sim.add_argument("--interval", type=float, default=2.0,
                     help="Segundos entre lecturas (default 2, como el firmware)")
    sim.add_argument("--fallas", action="store_true",
                     help="De vez en cuando el DHT11 simulado manda null")
    return parser


def main(argv=None) -> int:
    _utf8_console()
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Error de configuración: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg.log_level)

    stop = threading.Event()

    def _handle_signal(signum, frame):
        log.info("Señal %s recibida: deteniendo el gateway…", signum)
        stop.set()

    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _handle_signal)

    try:
        if args.command == "status":
            return print_status(cfg)
        if args.command == "run":
            return run_gateway(cfg, stop=stop)

        from gateway.simulator import SimulatedArduino

        if args.only_print:
            arduino = SimulatedArduino(interval=args.interval, failures=args.fallas,
                                       sleep=lambda s: stop.wait(s))
            while not stop.is_set():
                line = arduino.readline()
                if not stop.is_set():
                    print(line.decode().rstrip(), flush=True)
            return 0
        log.info("Modo simulador: no se usa el puerto Serial.")
        return run_gateway(
            cfg,
            open_port=lambda port, baud: SimulatedArduino(
                interval=args.interval, failures=args.fallas, sleep=lambda s: stop.wait(s)),
            find=lambda setting: "arduino-simulado",
            stop=stop,
        )
    except ConfigError as exc:
        print(f"Error de configuración: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
