"""
aerofan - fan control for the Gigabyte AERO 15 on Windows 11.

Layers, bottom up:

    pawnio.py     ctypes binding to the PawnIO signed driver
    ec.py         ACPI EC mailbox protocol, serialised on the system mutex
    registers.py  the AERO 15 register map and the safety floor
    probe.py      read-only investigation (phase 0)

Nothing above registers.py writes to the embedded controller yet. That is
deliberate: the register map is inherited from two Linux projects targeting
neighbouring models, and it gets confirmed on this machine before it gets used.
"""

__version__ = "0.1.0"
