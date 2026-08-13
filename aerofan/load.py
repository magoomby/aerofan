"""
Load generation and out-of-band monitoring.

The first --load run died with "read of 0x15 failed after 3 attempts". The cause
was not the EC: the probe spawns one busy process per core at normal priority,
which starves its own sampler. The EC poll loop then misses its window and every
read looks lost.

Fix is scheduling, not timeouts. Burners run at IDLE priority - they still
saturate the CPU, because nothing else wants it, but they yield instantly to the
sampler. The sampler process runs at HIGH. Leaving two cores unclaimed also
gives Windows somewhere to put acpi.sys.

GPU: nvidia-smi reports temperature but has no way to generate load, and torch
is not installed. Rather than pull down a CUDA stack for one experiment, we run
CPU load and *monitor* GPU temperature. That is arguably the more useful
experiment anyway - if fan 2 tracks a rising CPU while the GPU stays at idle
temperature, the two fans are linked, which is precisely the open question.
"""

from __future__ import annotations

import ctypes
import multiprocessing
import subprocess
import time

IDLE_PRIORITY_CLASS = 0x00000040
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
NORMAL_PRIORITY_CLASS = 0x00000020
HIGH_PRIORITY_CLASS = 0x00000080


def set_priority(priority: int) -> bool:
    """Set the current process's priority class."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetCurrentProcess()
        return bool(kernel32.SetPriorityClass(handle, priority))
    except Exception:
        return False


def _burn(stop_at: float) -> None:
    set_priority(IDLE_PRIORITY_CLASS)
    value = 1.0
    while time.time() < stop_at:
        for _ in range(100000):
            value = value * 1.0000001 + 1.0
        if value > 1e300:
            value = 1.0


class CpuLoad:
    """
    All-core load that deliberately gets out of the sampler's way.

    Context manager: workers start on entry and are joined on exit, including
    on an exception, so a failed run never leaves burners behind.
    """

    def __init__(self, seconds: int, spare_cores: int = 2):
        self.seconds = seconds
        cores = multiprocessing.cpu_count()
        self.workers = max(1, cores - spare_cores)
        self._procs: list[multiprocessing.Process] = []

    def __enter__(self) -> "CpuLoad":
        stop_at = time.time() + self.seconds + 5
        for _ in range(self.workers):
            proc = multiprocessing.Process(target=_burn, args=(stop_at,),
                                           daemon=True)
            proc.start()
            self._procs.append(proc)
        return self

    def __exit__(self, *exc) -> None:
        for proc in self._procs:
            if proc.is_alive():
                proc.terminate()
        for proc in self._procs:
            proc.join(timeout=3)
        self._procs.clear()


def gpu_status() -> dict[str, object] | None:
    """Temperature and utilisation from nvidia-smi, or None if unavailable."""
    try:
        output = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=temperature.gpu,utilization.gpu,clocks.sm",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip().splitlines()[0]
        temp, util, clock = (part.strip() for part in output.split(","))
        return {"temp_c": int(temp), "util_pct": int(util), "sm_mhz": int(clock)}
    except Exception:
        return None
