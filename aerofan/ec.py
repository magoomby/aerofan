"""
ACPI embedded-controller transport over ports 0x62 / 0x66.

The EC is a separate microcontroller with its own address space. You do not read
it directly; you hand it a command and an address through a two-port mailbox and
wait for it to answer. The protocol is in the ACPI spec, section 12.

    read  0xNN:  cmd <- 0x80, data <- NN,        then wait OBF, data -> value
    write 0xNN:  cmd <- 0x81, data <- NN, data <- value

MEASURED ON THIS MACHINE (AERO 15 Studio XB, BIOS HFB07, PawnIO 2.0.0):

  * A full read transaction works and is fast: OBF was set on the very first
    poll after the address write.
  * One PawnIO IOCTL round trip costs ~67 us, so the poll loop is naturally
    rate-limited to ~15k/s. There is no point sleeping between polls; the
    driver call *is* the delay.
  * IBF stayed set for at least 800 us after a command byte. The EC is not in
    a hurry to consume it, so the IBF wait needs real headroom.
  * **There is no Access_EC mutant.** OpenMutexW returns ERROR_FILE_NOT_FOUND
    for Global\\Access_EC, Access_EC and Local\\Access_EC. Windows only creates
    it when the ACPI namespace asks for global-lock arbitration on the EC, and
    this firmware does not.

That last point drives the design. We cannot serialise against Windows' own
acpi.sys, which drives the same mailbox for battery, thermal, lid and hotkey
traffic. So we assume interference rather than pretend it away:

  * every transaction is retried, re-draining first, before it is called failed
  * transactions are kept as short as possible
  * writes are read back and verified by the caller

A lost read is a normal event here, not an error. A lost read that we report as
a value would be a bug, which is why a timeout raises rather than returning 0.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time

from .pawnio import PawnIO
from .registers import EC_CMD_PORT, EC_DATA_PORT

# Status register (0x66) bits, per ACPI spec table 12-3.
STATUS_OBF = 0x01      # output buffer full - the EC has a byte for us
STATUS_IBF = 0x02      # input buffer full - the EC has not taken our byte
STATUS_CMD = 0x08      # last write was a command, not data
STATUS_BURST = 0x10
STATUS_SCI_EVT = 0x20
STATUS_SMI_EVT = 0x40

_STATUS_NAMES = (
    (STATUS_OBF, "OBF"),
    (STATUS_IBF, "IBF"),
    (STATUS_CMD, "CMD"),
    (STATUS_BURST, "BURST"),
    (STATUS_SCI_EVT, "SCI_EVT"),
    (STATUS_SMI_EVT, "SMI_EVT"),
)

CMD_READ = 0x80
CMD_WRITE = 0x81
CMD_BURST_ENABLE = 0x82
CMD_BURST_DISABLE = 0x83
BURST_ACK = 0x90

# Measured: a successful transaction completes in 1-3 ms, and OBF is typically
# set on the *first* poll after the address write. So a read that has not
# answered in 20 ms has been lost to acpi.sys and will never answer - waiting
# longer is pure cost. The first version used 250 ms here, and with a ~13% loss
# rate the timeouts alone accounted for essentially the whole 9-second dump.
# Fail fast and retry instead: a lost read costs 20 ms, not 250 ms.
IBF_TIMEOUT = 0.10   # the EC has been seen sitting on a command byte for ~1 ms
OBF_TIMEOUT = 0.02

# More attempts, because each one is now cheap. Five consecutive losses is a
# real fault; one or two is Tuesday on a machine with no EC mutex.
ATTEMPTS = 8
RETRY_BACKOFF = 0.001

# A write can be lost exactly like a read, and losing one looks identical to the
# EC refusing it. Observed directly: 0x0C rejected 0x33 on one run and accepted
# it on the next, with nothing changed. So a write is not "refused" until it has
# failed verification this many times.
WRITE_ATTEMPTS = 6


def describe_status(status: int) -> str:
    flags = [name for bit, name in _STATUS_NAMES if status & bit]
    return "|".join(flags) or "idle"


class ECError(RuntimeError):
    pass


class ECTimeout(ECError):
    pass


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
_kernel32.OpenMutexW.restype = wt.HANDLE
_kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
_kernel32.WaitForSingleObject.restype = wt.DWORD
_kernel32.ReleaseMutex.argtypes = [wt.HANDLE]
_kernel32.ReleaseMutex.restype = wt.BOOL
_kernel32.CloseHandle.argtypes = [wt.HANDLE]
_kernel32.CloseHandle.restype = wt.BOOL

SYNCHRONIZE = 0x00100000
MUTEX_MODIFY_STATE = 0x0001
WAIT_TIMEOUT = 0x00000102


class ECMutex:
    """
    The system EC lock, if this firmware has one. On the AERO 15 it does not.

    Absent is not the same as broken. Plenty of machines never create this
    object; NBFC and LibreHardwareMonitor both cope by simply not having it.
    What it costs us is arbitration, which is why the transport retries.
    """

    NAMES = ("Global\\Access_EC", "Access_EC")

    def __init__(self, timeout_ms: int = 500):
        self.timeout_ms = timeout_ms
        self.handle = None
        self.name = None
        self._fallback = threading.RLock()
        for name in self.NAMES:
            handle = _kernel32.OpenMutexW(SYNCHRONIZE | MUTEX_MODIFY_STATE,
                                          False, name)
            if handle:
                self.handle = handle
                self.name = name
                return

    @property
    def available(self) -> bool:
        return self.handle is not None

    def __enter__(self) -> "ECMutex":
        if self.handle is None:
            self._fallback.acquire()
            return self
        if _kernel32.WaitForSingleObject(self.handle, self.timeout_ms) == WAIT_TIMEOUT:
            raise ECTimeout(f"timed out waiting for {self.name}")
        return self

    def __exit__(self, *exc) -> None:
        if self.handle is None:
            self._fallback.release()
        else:
            _kernel32.ReleaseMutex(self.handle)

    def close(self) -> None:
        if self.handle:
            _kernel32.CloseHandle(self.handle)
            self.handle = None


class EmbeddedController:
    """Byte-addressed access to the EC, with retry."""

    def __init__(self, io: PawnIO, mutex: ECMutex | None = None):
        self.io = io
        self.mutex = mutex if mutex is not None else ECMutex()
        self._local = threading.RLock()
        # Cheap health signals: how often we are losing races to acpi.sys, and
        # how often we catch its traffic in our mailbox.
        self.attempts = 0
        self.retries = 0
        self.foreign = 0
        self.disagreements = 0

    # -- low level -----------------------------------------------------------

    def status(self) -> int:
        return self.io.port_read(EC_CMD_PORT)

    def _spin(self, predicate, timeout: float, what: str) -> None:
        """
        Poll flat out until `predicate(status)`.

        No sleep. Each status read is a driver round trip costing ~67 us, which
        is the rate limit; adding a sleep on top only widens the window for
        acpi.sys to take the byte we are waiting for.
        """
        deadline = time.monotonic() + timeout
        last = 0
        while time.monotonic() < deadline:
            last = self.status()
            if predicate(last):
                return
        raise ECTimeout(f"{what} (last status 0x{last:02X} = {describe_status(last)})")

    def _wait_input_clear(self) -> None:
        self._spin(lambda s: not s & STATUS_IBF, IBF_TIMEOUT,
                   "EC never accepted our byte")

    def _wait_output_full(self) -> None:
        self._spin(lambda s: s & STATUS_OBF, OBF_TIMEOUT,
                   "EC produced no answer")

    def _drain(self) -> None:
        """
        Clear anything the EC left in the output buffer.

        Matters more here than on a machine with the mutex: an interrupted
        transaction - ours or the OS's - leaves a byte behind, and reading it
        is what stops our next read returning somebody else's data.
        """
        for _ in range(8):
            if not self.status() & STATUS_OBF:
                return
            self.io.port_read(EC_DATA_PORT)
        raise ECError("EC output buffer will not drain")

    # -- transactions --------------------------------------------------------

    def _attempt_read(self, register: int) -> int:
        self._drain()
        self._wait_input_clear()
        self.io.port_write(EC_CMD_PORT, CMD_READ)
        self._wait_input_clear()
        # If a byte has appeared before we have even asked for one, it belongs
        # to somebody else's transaction. Taking it would return their data as
        # our register's value.
        if self.status() & STATUS_OBF:
            self.foreign += 1
            raise ECTimeout("foreign byte in the output buffer before our read")
        self.io.port_write(EC_DATA_PORT, register)
        self._wait_output_full()
        return self.io.port_read(EC_DATA_PORT) & 0xFF

    def _attempt_write(self, register: int, value: int) -> None:
        self._drain()
        self._wait_input_clear()
        self.io.port_write(EC_CMD_PORT, CMD_WRITE)
        self._wait_input_clear()
        self.io.port_write(EC_DATA_PORT, register)
        self._wait_input_clear()
        self.io.port_write(EC_DATA_PORT, value)
        self._wait_input_clear()

    def _recover(self) -> None:
        """
        Unstick an EC that has stopped answering.

        Seen in the wild: status pinned at 0x08 (CMD set, no OBF, no IBF) with
        every read timing out. That is the EC holding a half-finished command -
        ours or the OS's. Reading the data port a few times and giving it a
        breath clears it, where retrying the same read never does.
        """
        try:
            for _ in range(4):
                self.io.port_read(EC_DATA_PORT)
                if not self.status() & (STATUS_OBF | STATUS_IBF):
                    break
        except Exception:
            pass
        time.sleep(0.02)

    def _retrying(self, operation, what: str):
        last: Exception | None = None
        for attempt in range(ATTEMPTS):
            self.attempts += 1
            try:
                with self._local, self.mutex:
                    return operation()
            except ECTimeout as exc:
                last = exc
                self.retries += 1
                # Halfway through, stop assuming it is contention and treat the
                # EC as stuck.
                if attempt == ATTEMPTS // 2:
                    with self._local:
                        self._recover()
                # Give acpi.sys room to finish whatever it was doing.
                time.sleep(RETRY_BACKOFF * (attempt + 1))
        raise ECTimeout(f"{what} failed after {ATTEMPTS} attempts: {last}")

    def read(self, register: int) -> int:
        if not 0 <= register <= 0xFF:
            raise ValueError(f"register out of range: {register}")
        return self._retrying(lambda: self._attempt_read(register),
                              f"read of 0x{register:02X}")

    def write(self, register: int, value: int) -> None:
        if not 0 <= register <= 0xFF:
            raise ValueError(f"register out of range: {register}")
        if not 0 <= value <= 0xFF:
            raise ValueError(f"value out of range: {value}")
        self._retrying(lambda: self._attempt_write(register, value),
                       f"write of 0x{register:02X}")

    def write_verified(self, register: int, value: int) -> int:
        """
        Write, then read back. Returns what the EC actually holds.

        Without arbitration a write can be lost the same way a read can, and a
        silently-lost fan-speed write is exactly the failure we cannot tolerate.
        The caller decides what to do if the readback disagrees - the EC is
        entitled to clamp or ignore a value, and that is not the same as a lost
        write.

        The retry matters more than it looks. 0x0C refused 0x33 on one run and
        accepted the identical write on the next with nothing changed, so a
        single failed verification proves nothing on this machine. Only a value
        that will not stick after several honest attempts is being refused.
        """
        actual: int | None = None
        for attempt in range(WRITE_ATTEMPTS):
            try:
                self.write(register, value)
                actual = self.read(register)
                if actual == value:
                    return actual
                # A single disagreeing read is not evidence - it may itself be
                # a foreign byte. Confirm before spending another write.
                actual = self.read_stable(register)
                if actual == value:
                    return actual
            except ECTimeout:
                pass
            time.sleep(0.004 * (attempt + 1))
        return actual if actual is not None else self.read_stable(register)

    # -- burst mode ----------------------------------------------------------
    #
    # ACPI 12.3.3/12.3.4. Burst mode asks the EC to stop its own housekeeping
    # and give the host a window of uninterrupted access. It is the standard
    # answer to "my write gets reverted": if the EC's firmware is refreshing a
    # byte from internal state between our write and our readback, burst is
    # what stops it doing so.
    #
    # The EC will drop out of burst by itself if the host goes quiet for ~1 ms,
    # so anything done inside the window must be short.

    def burst_enable(self) -> bool:
        with self._local, self.mutex:
            self._drain()
            self._wait_input_clear()
            self.io.port_write(EC_CMD_PORT, CMD_BURST_ENABLE)
            try:
                self._wait_output_full()
            except ECTimeout:
                return False
            ack = self.io.port_read(EC_DATA_PORT) & 0xFF
            return ack == BURST_ACK and bool(self.status() & STATUS_BURST)

    def burst_disable(self) -> None:
        with self._local, self.mutex:
            try:
                self._wait_input_clear()
                self.io.port_write(EC_CMD_PORT, CMD_BURST_DISABLE)
            except ECError:
                pass  # the EC drops burst on its own; this is best-effort

    def read_stable(self, register: int, samples: int = 3,
                    max_tries: int = 7) -> int:
        """
        Read until the same value comes back `samples` times. Use this for
        anything a decision is made on.

        We have watched a read of 0x0D return 0x90, 0x17, 0x38, 0x18 and 0xF7
        in the middle of a run where the correct answer was 0x01. 0x90 is the
        EC's burst-mode acknowledgement - a byte the firmware produced for
        acpi.sys, which we consumed. A single read on this machine is a sample
        of a noisy channel, not a fact. Agreement across reads is cheap and
        turns it back into one.
        """
        counts: dict[int, int] = {}
        for _ in range(max_tries):
            value = self.read(register)
            counts[value] = counts.get(value, 0) + 1
            if counts[value] >= samples:
                if len(counts) > 1:
                    self.disagreements += 1
                return value
        self.disagreements += 1
        return max(counts, key=counts.get)

    def read_many(self, registers) -> dict[int, int]:
        """One transaction each, deliberately - a 256-register hold would starve
        the ACPI driver even if we had a mutex to hold."""
        return {reg: self.read(reg) for reg in registers}

    def dump(self, start: int = 0x00, end: int = 0xFF) -> dict[int, int]:
        return self.read_many(range(start, end + 1))

    @property
    def health(self) -> str:
        if not self.attempts:
            return "no transactions yet"
        rate = 100.0 * self.retries / self.attempts
        return (f"{self.attempts} transactions, {self.retries} retried "
                f"({rate:.1f}%), {self.foreign} foreign bytes caught, "
                f"{self.disagreements} unstable reads")
