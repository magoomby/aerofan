"""
Diagnose "OBF never set". Reads only; writes only to the EC command/data
mailbox, never a register value.

Four questions, in order, each decisive:

  1. Does the port read work at all, or are we getting a constant?
     0xFF means nothing is decoding the port. A constant 0x00 that never
     changes means either an idle EC or a read that isn't really happening.

  2. Do our writes land? Writing 0x80 to the command port must make IBF
     (bit 1) set for at least a moment. If IBF never twitches, the write is
     going nowhere and everything downstream is meaningless.

  3. Is something else eating our answer? Windows' ACPI driver shares this
     mailbox. If OBF sets and is gone before we look, we lose every read.
     Polling as fast as possible tells us whether the byte appears at all.

  4. Can we get the Access_EC mutant by any route? OpenMutexW resolves names
     per-session; NtOpenMutant can name the object directly, which works when
     the Global\\ prefix does not.

Run elevated:  python tools\\diag_ec.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aerofan.pawnio import PawnIO, PawnIOUnavailable, is_elevated  # noqa: E402

CMD, DATA = 0x66, 0x62
OBF, IBF = 0x01, 0x02


def decode(status: int) -> str:
    flags = []
    if status & 0x01:
        flags.append("OBF")
    if status & 0x02:
        flags.append("IBF")
    if status & 0x04:
        flags.append("CMD")
    if status & 0x08:
        flags.append("BURST")
    if status & 0x10:
        flags.append("SCI_EVT")
    if status & 0x20:
        flags.append("SMI_EVT")
    return "|".join(flags) or "idle"


# --- question 4: the mutant ---------------------------------------------------

ntdll = ctypes.WinDLL("ntdll")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ushort),
                ("MaximumLength", ctypes.c_ushort),
                ("Buffer", wt.LPWSTR)]


class OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Length", wt.ULONG),
                ("RootDirectory", wt.HANDLE),
                ("ObjectName", ctypes.POINTER(UNICODE_STRING)),
                ("Attributes", wt.ULONG),
                ("SecurityDescriptor", ctypes.c_void_p),
                ("SecurityQualityOfService", ctypes.c_void_p)]


def try_mutants() -> None:
    print("\n== 4. Access_EC mutant ==")
    SYNCHRONIZE, MODIFY = 0x00100000, 0x0001
    for name in ("Global\\Access_EC", "Access_EC", "Local\\Access_EC"):
        handle = kernel32.OpenMutexW(SYNCHRONIZE | MODIFY, False, name)
        error = ctypes.get_last_error()
        state = "OPENED" if handle else f"failed (win32 {error})"
        print(f"    OpenMutexW  {name:<20} {state}")
        if handle:
            kernel32.CloseHandle(handle)

    for path in ("\\BaseNamedObjects\\Access_EC", "\\Access_EC"):
        unicode_string = UNICODE_STRING()
        ntdll.RtlInitUnicodeString(ctypes.byref(unicode_string), path)
        attrs = OBJECT_ATTRIBUTES()
        attrs.Length = ctypes.sizeof(OBJECT_ATTRIBUTES)
        attrs.RootDirectory = None
        attrs.ObjectName = ctypes.pointer(unicode_string)
        attrs.Attributes = 0x00000040  # OBJ_CASE_INSENSITIVE
        attrs.SecurityDescriptor = None
        attrs.SecurityQualityOfService = None
        handle = wt.HANDLE()
        status = ntdll.NtOpenMutant(ctypes.byref(handle),
                                    SYNCHRONIZE | MODIFY,
                                    ctypes.byref(attrs))
        value = ctypes.c_ulong(status).value
        state = "OPENED" if value == 0 else f"failed (NTSTATUS 0x{value:08X})"
        print(f"    NtOpenMutant {path:<28} {state}")
        if value == 0:
            kernel32.CloseHandle(handle)
    print("    (0xC0000034 = the object does not exist on this machine.)")


def main() -> int:
    if not is_elevated():
        print("Run elevated.")
        return 1
    try:
        io = PawnIO()
    except PawnIOUnavailable as exc:
        print(exc)
        return 1

    print(f"PawnIOLib version: {'.'.join(str(p) for p in io.version())}")
    io.open()
    try:
        print("\n== 1. raw port reads, 20 samples ==")
        statuses, datas = [], []
        for _ in range(20):
            statuses.append(io.port_read(CMD))
            datas.append(io.port_read(DATA))
            time.sleep(0.01)
        print(f"    0x66 status : {' '.join(f'{v:02X}' for v in statuses)}")
        print(f"    0x62 data   : {' '.join(f'{v:02X}' for v in datas)}")
        unique = set(statuses)
        print(f"    distinct status values: {sorted(f'0x{v:02X}' for v in unique)}"
              f"  -> {decode(statuses[0])}")
        if unique == {0xFF}:
            print("    VERDICT: 0xFF constant - nothing is decoding this port.")
            return 1

        print("\n== 2. does a command write land? (writing 0x80 = READ cmd) ==")
        print("    Watching IBF immediately after the write. IBF must set,")
        print("    even briefly, or our write never reached the EC.")
        io.port_write(CMD, 0x80)
        trace = [io.port_read(CMD) for _ in range(12)]
        print(f"    status after cmd: {' '.join(f'{v:02X}' for v in trace)}")
        saw_ibf = any(v & IBF for v in trace)
        print(f"    IBF observed: {saw_ibf}")
        if not saw_ibf:
            print("    Either the write is not landing, or the EC consumed it")
            print("    faster than one IOCTL round trip. Continue to step 3.")

        print("\n== 3. full read of 0xB0, polling flat out ==")
        # Complete the transaction we started above by sending the address.
        io.port_write(DATA, 0xB0)
        deadline = time.monotonic() + 1.0
        polls, first_obf, value = 0, None, None
        while time.monotonic() < deadline:
            status = io.port_read(CMD)
            polls += 1
            if status & OBF:
                first_obf = polls
                value = io.port_read(DATA)
                break
        rate = polls / max(time.monotonic() - (deadline - 1.0), 1e-9)
        print(f"    polls: {polls}  ({rate:.0f}/s, i.e. one every "
              f"{1e6 / max(rate, 1e-9):.0f} us)")
        if value is not None:
            print(f"    OBF set after {first_obf} polls; 0xB0 = {value} "
                  f"(0x{value:02X})")
            print("    VERDICT: the transaction works. The original failure was")
            print("    a timing or draining bug, not a transport problem.")
        else:
            print("    OBF never set in a full second of flat-out polling.")
            print("    VERDICT: the answer is being consumed before we see it,")
            print("    or this EC does not use the 0x80 command protocol.")

        try_mutants()
        return 0
    finally:
        io.close()


if __name__ == "__main__":
    raise SystemExit(main())
