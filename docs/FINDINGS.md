# aerofan — feasibility findings

Target: **GIGABYTE AERO 15 Studio XB**, BIOS `HFB07` (2020-05-27), Windows 11 Pro,
Python 3.14.6 (`C:\Users\admin\AppData\Local\Python\pythoncore-3.14-64`).

Probes run on the machine are in `tools/probe_wmi.ps1` and `tools/probe_security.ps1`
(both read-only).

---

## 1. What fusion-kbd gives us

Structurally: a lot. Functionally: nothing reusable.

Reusable — the *shape* of the project:

- `fusionkbd_brightness.py` — the read-state / modify-one-byte / write-state-back
  pattern, with a lock, an `expected` echo-suppression field, and a `--apply`
  escape hatch when the inferred write format is wrong.
- `fn_probe.py` — the probe-first discipline: enumerate outcomes A/B/C *before*
  writing code, dump raw observations to JSON, print an explicit verdict. This is
  exactly the right approach for the fan registers, where a wrong write is worse
  than a wrong read.
- `install-hotkeys-task.ps1` + the PyInstaller spec — packaging and autostart.

Not reusable — the transport. The keyboard is a USB HID device at `1044:7a3f`
reached through Windows' native HID stack with zero privilege. The fans are on the
**embedded controller**, which is not a HID device and is not reachable from user
mode at all. Different problem, different plumbing.

---

## 2. There is no vendor API on this machine

Checked and came back empty:

| Surface | Result |
|---|---|
| `root\wmi` classes matching `GB_*` / Gigabyte | **none** |
| ACPI→WMI mapper device (`PNP0C14`) | **not present** |
| `MSAcpi_ThermalZoneTemperature` | **returns nothing** |
| `Win32_Fan` | 3 stub "Cooling Device" entries, all properties null |
| Gigabyte Control Center / Smart Manager service | **not installed** |
| Existing ring-0 IO driver (WinRing0, inpout, RwDrv, GLCKIo…) | **none** |

So there is no supported path. Everything goes through the EC.

---

## 3. The EC register map (corroborated by two independent sources)

Both [tangalbert919/p37-ec-aero-15](https://github.com/tangalbert919/p37-ec-aero-15)
and the NBFC `Gigabyte Aero15x v8` config agree:

| Reg | Purpose |
|---|---|
| `0x06` bit 4 | custom mode type — 0 = auto-max, 1 = **fixed speed** |
| `0x08` bit 6 | quiet mode (0 = off) |
| `0x0C` bit 4 | gaming mode (0 = off) |
| `0x0D` bit 0 | **custom mode on/off** |
| `0x0D` bit 7 | "deep control" / curve mode |
| `0xB0` | **fan 1 (CPU) fixed speed** |
| `0xB1` | **fan 2 (GPU) fixed speed** |
| `0xFC` | fan 1 current speed, 0–22 scale (not RPM) |
| `0xFE` | fan 2 current speed, 0–22 scale |

Speed values: `0x00`–`0xE5` (0–229) = 0–100 %. **`0x44` (68) ≈ 30 % is the floor.**
Below that the PWM duty is unsafe — the p37-ec docs say so explicitly, and NBFC's
own curves never go between 0 and ~30 %.

Two open questions on *this* BIOS specifically:

- p37-ec says `0xB0`/`0xB1` "must be set to identical values" (one shared PWM);
  the NBFC config treats them as two independent fans. On a Studio XB with a
  discrete GPU I'd expect independent, but that has to be measured, not assumed.
- RPM read register moved between generations (`0xFC`/`0xFE` on Aero 15/14,
  `0xB3`/`0xB4` on Aero 16). We read all four and see which move.

---

## 4. The actual blocker: getting to ring 0

The EC is behind port I/O at `0x62`/`0x66`. User mode cannot touch those ports, so
this needs a kernel driver. On *this* machine:

```
Memory Integrity (HVCI)          : Enabled  (WasEnabledBy = 2)
VirtualizationBasedSecurityStatus: 2  (running)
Vulnerable driver blocklist      : Enabled
```

That combination kills the traditional answer. **WinRing0** — what NBFC,
LibreHardwareMonitor and FanControl have historically used — was added to
Microsoft's vulnerable-driver blocklist in March 2025 and is now detected as
`VulnerableDriver:WinNT/Winring0`. It will not load here. `inpoutx64` and `RwDrv`
are in the same boat.

The current answer is **[PawnIO](https://pawnio.eu/)** (namazso): a properly signed
driver that executes sandboxed Pawn bytecode modules in ring 0, so the *driver*
isn't a generic read/write-anything primitive. FanControl switched to it in v238.
Its [`LpcACPIEC`](https://github.com/namazso/PawnIO.Modules/blob/main/LpcACPIEC.p)
module is exactly what we need — it exports `ioctl_pio_read` / `ioctl_pio_write`
and **restricts them to ports 0x62 and 0x66 only**, which is a meaningful safety
property. Its docs also state the requirement I'd have flagged anyway:

> You should acquire the `\BaseNamedObjects\Access_EC` mutant before calling this

That is the mutex Windows' own ACPI EC driver holds. Honouring it is the
difference between "works" and "intermittently wedges the EC while Windows is
mid-transaction", which is the failure mode that ends in a hard power cycle.

PawnIO ships a user-mode DLL (`PawnIOLib`), so Python drives it via `ctypes` —
same technique as the `user32`/`ctypes` work already in fusion-kbd. No C# needed.

PawnIO is **not currently installed** on the machine.

---

## 5. What I can and can't do from here

I can: read and write the repo, run PowerShell and Python on the laptop, and read
back output. I proved it — everything in section 2 and 4 above is live data from
`aero`, not assumption.

I can't: **run elevated.** The bridge runs unelevated and `runas` is blocked, so
installing PawnIO and any command that opens the driver handle has to be launched
by you. I also can't hear the fans.

Proposed shape that removes most of that friction:

- `aerofan` — CLI + library, runs as you, does the policy.
- `aerofan-svc` — a small Windows service running as SYSTEM that owns the PawnIO
  handle and the `Access_EC` mutex, exposing a named pipe with a *narrow* API
  (read reg, write reg from a whitelist, set speed, get speed, panic-restore).

You install the service once, elevated. After that I can drive the whole thing
from an unelevated shell and iterate without you in the loop for every command —
and the whitelist means a bug in my code can't scribble on an arbitrary EC byte.

---

## 6. Risks, ranked

1. **Fans stuck off / too slow.** The worst outcome. Mitigations: hard floor at
   `0x44` (30 %) enforced in the write path; a watchdog in the service that
   restores auto mode if the CLI stops heartbeating; restore auto mode on service
   stop, on user logoff, and on `SystemEvents` suspend/resume.
2. **EC contention.** Mitigated by the `Access_EC` mutex, short critical sections,
   and never polling faster than ~500 ms (NBFC's own `EcPollInterval`).
3. **Bearing wear.** The Aero 15X author reported both fans failing within a month
   of running them hard. Whatever curve we land on, it should not park the fans at
   high duty as a default.
4. **BIOS variant drift.** HFB07 on a *Studio* XB is not the exact machine either
   source documented. Registers are probably right; "probably" is why step one is
   a read-only probe.
5. **State does not survive.** Sleep/resume and reboot will reset the EC. Needs
   re-application on resume, same as the keyboard settings did.
6. **Windows may fight us.** Modern standby transitions and the ACPI thermal zone
   can re-assert EC state. Detectable by watching whether our written value at
   `0xB0` stays put.

---

## 7. Proposed sequence

**Phase 0 — read-only.** Install PawnIO, load `LpcACPIEC`, dump EC bytes
`0x00`–`0xFF`. Then dump again under load, and again in each Gigabyte power
profile if any are reachable. Diff them. This confirms the map without a single
write, and produces the JSON artefact `fn_probe.py` style.

**Phase 1 — one write.** Set custom+fixed mode, write one value to `0xB0`, watch
`0xFC`. You listen. Restore auto. That's the whole phase.

**Phase 2 — the pair.** Establish whether `0xB0` and `0xB1` are independent.

**Phase 3 — the tool.** Service + CLI + curve + watchdog + resume handling.

**Phase 4 — packaging.** PyInstaller and a scheduled task, reusing the fusion-kbd
patterns directly.
