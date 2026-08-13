# aerofan

Fan control for the **Gigabyte AERO 15** on Windows 11, in Python.

Written for an AERO 15 Studio XB on BIOS `HFB07`. It will probably suit other
Aero 15 / Aero 14 / Aorus 15 machines, but the register map is confirmed
per-machine before anything is written — see [Phase 0](#phase-0-probe-first).

Sibling project: [fusion-kbd](../fusion-kbd), which does the same job for the
RGB keyboard. This borrows its structure and its probe-before-you-write habit,
but none of its code — the keyboard is a USB HID device reachable with no
privilege at all, and the fans are not.

---

## Why this is harder than the keyboard

The fans hang off the **embedded controller**, a separate microcontroller
addressed through a two-byte mailbox on I/O ports `0x62` / `0x66`. Three
consequences:

1. **There is no vendor API.** This machine has no `GB_*` WMI classes, no ACPI
   WMI mapper device (`PNP0C14`), no working `MSAcpi_ThermalZoneTemperature`,
   and `Win32_Fan` returns three empty stubs. Gigabyte Control Center is not
   installed. Everything has to go through the EC directly.

2. **Port I/O needs ring 0.** That means a kernel driver.

3. **Windows is already using that mailbox** for battery, thermal, lid and
   hotkey traffic. Interleaving with it corrupts transactions.

### The driver

The traditional answer — **WinRing0**, used by NBFC, LibreHardwareMonitor and
FanControl — is dead on this machine. It went onto Microsoft's vulnerable-driver
blocklist in March 2025 and is detected as `VulnerableDriver:WinNT/Winring0`.
This laptop runs with Memory Integrity (HVCI) enabled, VBS running, and the
blocklist on, so it will not load. `inpoutx64` and `RwDrv` are in the same
position.

aerofan uses **[PawnIO](https://pawnio.eu/)** instead: a current, signed driver
that executes sandboxed Pawn bytecode modules in ring 0. It is a better fit than
a straight port-I/O primitive, because the module we load
([`LpcACPIEC`](https://github.com/namazso/PawnIO.Modules/blob/main/LpcACPIEC.p))
**refuses every port except `0x62` and `0x66`**. A typo in this repo cannot
reach the rest of the I/O space.

You install PawnIO yourself, knowingly. `tools/install-pawnio.ps1` checks it and
fetches the module, but deliberately does not install a kernel driver on your
behalf.

### The mutex

PawnIO's own documentation says to hold `\BaseNamedObjects\Access_EC` before
touching those ports. That is the mutant Windows' ACPI driver serialises on.
`aerofan/ec.py` takes it for the whole of every transaction, and if it cannot
open it, says so and refuses to be trusted for writes. Skipping this is how you
get an EC wedged mid-transaction, and that ends in a hard power cycle.

---

## Register map

From [tangalbert919/p37-ec-aero-15](https://github.com/tangalbert919/p37-ec-aero-15)
and the NBFC `Gigabyte Aero15x v8` config, which agree:

| Reg | Purpose |
|---|---|
| `0x06` bit 4 | custom mode type — 0 = auto-max, 1 = fixed speed |
| `0x08` bit 6 | quiet mode |
| `0x0C` bit 4 | gaming mode |
| `0x0D` bit 0 | custom mode master switch |
| `0x0D` bit 7 | "deep control" curve mode (poorly documented) |
| `0xB0` | fan 1 (CPU) duty |
| `0xB1` | fan 2 (GPU) duty |
| `0xFC` / `0xFE` | fan 1 / fan 2 speed readout, 0–22 scale (Aero 15 position) |
| `0xB3` / `0xB4` | same, Aero 16 position — probed as an alternative |

Duty is `0x00`–`0xE5` (0–229) for 0–100 %.

> **`0x44` (≈30 %) is a hard floor.** Below that the PWM duty will not reliably
> keep the blades turning. A stalled fan the EC believes is spinning is the
> failure mode that cooks the machine. The floor is enforced in the write path
> and is not configurable.

### Confirmed on this machine

| Finding | Evidence |
|---|---|
| All six control registers accept and hold writes | `0x06`, `0x08`, `0x0C`, `0x0D`, `0xB0`, `0xB1` all still set 1 s later |
| **Writes get lost, they do not get refused** | `0x0C` refused `0x33` on one run and accepted the identical write on the next. Every write now retries until verified — that alone took the failure rate from "blocked" to 1 % |
| The two fans are independently reported | `0xFC`/`0xFE` diverged (`13/14`, `14/15`) once runs were long enough |
| `0xB0` is ignored until custom mode is on | It sat at `57` for an entire run while the fans ramped `10 → 15` |
| Gaming mode works, but only nudges the curve | idle `0xB3` 75 → 80; load peak 107 → 119 |
| Burst mode is granted | `0x82` acknowledged with `0x90`, `BURST` set. Not needed now that writes retry, but available |
| There is no `Access_EC` mutant | `OpenMutexW` → `ERROR_FILE_NOT_FOUND` for all three names |

The missing gale is explained: with Gigabyte Control Center gone, nothing has
ever switched this EC off its default curve, and `0xB0` is the register that
does it.

Still open: `0x0D` bit 7 versus bit 0 — both hold, so which one actually grants
custom mode is decided by `tools/test_fixed_speed.py` watching the tachometer,
not by the readback.

---

## Phase 0: probe first

Read-only. It cannot change your fan speed.

```powershell
# elevated PowerShell, once
.\tools\install-pawnio.ps1

# then
python -m aerofan.probe             # baseline + idle stability
python -m aerofan.probe --load 60   # 60s all-core load, sampled throughout
```

The probe dumps all 256 EC registers, samples repeatedly at idle to learn what
drifts on its own, applies CPU load, and diffs. It prints a verdict — which
registers track load, whether one or two fans report independently — and writes
every raw sample to `probe_results.json` so a wrong verdict can be re-read
without re-running.

**Run the `--load 60` pass with the laptop somewhere quiet and listen.** The
readout registers are a 0–22 counter, not RPM; correlating them with what you
can actually hear is what turns them from "a byte that moves" into "the CPU fan".

---

## Roadmap

| Phase | State |
|---|---|
| 0 — read-only probe, confirm the map | **done** |
| 1 — first guarded write (gaming mode), restore | **done** — works, modest effect |
| 2 — custom mode + fixed speed | **ready to run** |
| 3 — SYSTEM daemon + named pipe, curve, watchdog, resume handling | |
| 4 — PyInstaller exe + scheduled task, per fusion-kbd | |

Phase 3 puts the driver handle and the `Access_EC` mutex in a SYSTEM-side
daemon behind a named pipe with a whitelisted API, so the day-to-day CLI runs
unelevated and a bug in the policy layer cannot write to an arbitrary EC byte.

---

## Layout

```
aerofan/
  registers.py   register map, percent<->raw, the 30% floor, write whitelist
  pawnio.py      ctypes binding to PawnIOLib, module discovery
  ec.py          ACPI EC mailbox protocol, Access_EC mutex, timeouts
  probe.py       phase 0 - reads only
tools/
  install-pawnio.ps1  driver check + module fetch
  probe_wmi.ps1       what vendor surfaces exist (they don't)
  probe_security.ps1  HVCI / blocklist status, i.e. why not WinRing0
```

No third-party Python dependencies. `ctypes` only, same as fusion-kbd.

---

## Credit

The register work is not mine. It comes from
[tangalbert919/p37-ec-aero-15](https://github.com/tangalbert919/p37-ec-aero-15),
[jertel/p37-ec](https://github.com/jertel/p37-ec),
[CommitThis/aero15x-fand](https://github.com/CommitThis/aero15x-fand) and the
[NBFC](https://github.com/nbfc-linux/nbfc-linux) config collection. The
[aero15x-fand](https://committhis.github.io/2020/07/26/aero15x.html) writeup is
also the source of the most useful warning in this repo: its author had both
fans fail within a month of running them hard.
