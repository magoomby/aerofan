"""
aerofan - fan control for the Gigabyte AERO 15 on Windows 11.

Layers, bottom up:

    pawnio.py     ctypes binding to the PawnIO signed driver
    ec.py         ACPI EC mailbox protocol, serialised on the system mutex
    registers.py  the AERO 15 register map and the safety floor
    control.py    mode switching, verified writes, restore-on-exit
    curve.py      temperature -> duty, with asymmetric response
    sensors.py    where temperatures come from, and how they fail
    supervisor.py the control loop: switchable profile, telemetry, watchdog
    probe.py      read-only investigation (phase 0)

and above that, three front ends onto the same supervisor:

    winservice.py the Windows service. SYSTEM, from boot, owns the driver
    ipc.py        the named pipe everything else talks to it through
    tray.py       the notification icon, unelevated, in your logon session
    daemon.py     the loop in a terminal, for watching it work
    cli.py        one-shot commands, through the service if it is running

Exactly one process holds the driver at a time. That is not a style choice:
this machine has no Access_EC mutant, so two processes on the EC mailbox
interleave and corrupt each other's transactions. The service is normally that
process, and the pipe is how everything else reaches it.

Nothing outside control.py writes to the embedded controller, and nothing
writes to a register that is not in registers.WRITE_WHITELIST.
"""

__version__ = "0.2.0"
