r"""
The control loop in a terminal, for when you want to watch it work.

    python -m aerofan.daemon --profile aggressive
    python -m aerofan.daemon --profile aggressive --dry-run
    python -m aerofan.daemon --config C:\path\to\aerofan.json

Elevated. Ctrl-C hands the fans back to the EC on the way out.

This is now a thin front end on ``supervisor.Supervisor`` - the same engine the
service runs, with the profile fixed at the command line and the log going to
the console instead of a file. Keeping them the same object is the point: a
curve that behaves differently depending on how it was started is a curve you
cannot debug here and trust there.

For day to day use the service and the tray icon are better; this is for
watching a profile behave, or for running without installing anything.

    tools\install.ps1                       install the service and tray
    python -m aerofan.winservice run        the service engine, in a console

IT WILL NOT FIGHT THE SERVICE

If the service is running, it already owns the driver and the EC mailbox, and
a second process driving that mailbox is the exact interleaving this project
goes out of its way to avoid - there is no Access_EC mutant on this machine to
arbitrate. So this refuses to start, and tells you how to change the profile
without an elevated shell at all.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from pathlib import Path

from . import ipc
from .control import Controller
from .curve import PROFILES
from .ec import EmbeddedController
from .pawnio import PawnIO, PawnIOUnavailable, is_elevated
from .state import SERVICE_NAME, normalise_profile
from .supervisor import DEFAULT_CONFIG, Supervisor


def load_config(path: Path | None) -> dict:
    config = dict(DEFAULT_CONFIG)
    if path and path.is_file():
        # utf-8-sig: see the note in winservice.load_config. A BOM from
        # Notepad must not be the reason a curve does not load.
        config.update(json.loads(path.read_text(encoding="utf-8-sig")))
    return config


def console_logger(verbose: bool = False) -> logging.Logger:
    log = logging.getLogger("aerofan.daemon")
    log.handlers.clear()
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("  %(asctime)s  %(message)s",
                                           "%H:%M:%S"))
    log.addHandler(handler)
    log.propagate = False
    return log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="aerofan curve daemon.")
    parser.add_argument("--profile", choices=sorted(PROFILES), default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--poll", type=float, default=None)
    parser.add_argument("--force", action="store_true",
                        help="run even though the service is running. Two "
                             "processes on the EC mailbox; you have been told.")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        return print("Windows only.") or 1

    if ipc.is_running() and not args.force:
        print(f"\n  The {SERVICE_NAME} service is running and already owns the"
              f" EC.")
        print("  Two processes on that mailbox corrupt each other's"
              " transactions, so this")
        print("  will not start alongside it. To change the profile - from any"
              " shell, no")
        print("  elevation needed:\n")
        print("      python -m aerofan.cli profile aggressive")
        print("      python -m aerofan.cli auto\n")
        print("  Or use the tray icon. To run this anyway:  sc stop"
              f" {SERVICE_NAME}\n")
        return 1

    if not is_elevated():
        return print("Run elevated - the PawnIO driver is admin-only.") or 1

    config = load_config(args.config)
    if args.profile:
        config["profile"] = normalise_profile(args.profile)
    if args.poll:
        config["poll_seconds"] = args.poll

    try:
        io = PawnIO().open()
    except PawnIOUnavailable as exc:
        return print(f"PawnIO unavailable: {exc}") or 1

    log = console_logger()
    controller = Controller(EmbeddedController(io), dry_run=args.dry_run)
    supervisor = Supervisor(controller, config, log, dry_run=args.dry_run)

    def stop(*_):
        log.info("stopping")
        supervisor.stop()

    signal.signal(signal.SIGINT, stop)
    try:
        signal.signal(signal.SIGTERM, stop)
    except (AttributeError, ValueError):
        pass

    try:
        return supervisor.run()
    finally:
        log.info("EC transport health: %s", controller.ec.health)
        io.close()


if __name__ == "__main__":
    raise SystemExit(main())
