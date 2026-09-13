# aerofan

Fan control for the **Gigabyte AERO 15** on Windows 11, in Python.

Written for an AERO 15 Studio XB on BIOS `HFB07`. It will probably suit other
Aero 15 / Aero 14 / Aorus 15 machines, but the register map is confirmed
per-machine before anything is written — see [Phase 0](#phase-0-probe-first).

It runs as a Windows service that starts at boot with whichever profile you
last chose, and a tray icon to change it. [Install](#install),
[Using it](#using-it).

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
| `0x60` | **CPU temperature**, plain °C — found here, not in any source below |
| `0x61` | **GPU temperature**, plain °C — same |

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
| **`0x61` is the GPU temperature, in plain °C** | Tracks `nvidia-smi` to within 1 °C across a whole load/cool run, and matched it exactly on a live re-check (47 = 47) |
| **`0x60` is the CPU temperature, in plain °C** | Idles ~48 °C, spikes to 90 °C the instant an all-core load starts, settles to 76 °C at equilibrium, falls back to 61 °C on cooldown. A CPU-only load barely moves `0x61`, so the two are independent sensors rather than mirrors |
| `0x62` and `0x65` mirror `0x60` | Byte-identical across all 54 samples of a load run — shadow copies, usable as alternates |
| Only seven registers are even candidates | Across the full 256-byte map, only `0x16`, `0x60`, `0x61`, `0x62`, `0x65`, `0xB3`, `0xB4` both stay inside 25–105 and move at all; `0x16` ignores load entirely |

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
| 2 — custom mode + fixed speed | **done** |
| 3 — SYSTEM service + named pipe, curve, watchdog, resume handling | **done** |
| 4 — tray icon, install/uninstall scripts | **done** |
| 5 — find the real CPU temperature register | **done** — `0x60` and `0x61` |

Phase 3 put the driver handle in a SYSTEM-side service behind a named pipe with
a whitelisted API, so the day-to-day CLI runs unelevated and a bug in the policy
layer cannot write to an arbitrary EC byte.

Phase 5 closed the last real gap. Until it did, the CPU side fell back to the
ACPI chassis zone, which on this machine reads a constant 301 K and is not
measuring anything — the stuck-sensor detector in `sensors.py` exists because
of it — so the CPU fan only ever followed the GPU through the cross-coupling
floor. Both fans now run on their own sensors and their duties diverge, which
is what you want to see.

---

## Layout

```
aerofan/
  registers.py   register map, percent<->raw, the 30% floor, write whitelist
  pawnio.py      ctypes binding to PawnIOLib, module discovery
  ec.py          ACPI EC mailbox protocol, Access_EC mutex, timeouts
  control.py     mode switching, verified writes, restore-on-exit
  curve.py       temperature -> duty, asymmetric response, the profiles
  sensors.py     where temperatures come from, and how they fail
  supervisor.py  the control loop: switchable profile, telemetry, watchdog
  state.py       %ProgramData%\aerofan - the remembered profile, config, log
  cpufreq.py     the CPU max-frequency cap, via powrprof. Nothing to do with the EC
  gpu.py         switching the discrete GPU off. Also nothing to do with the EC
  winservice.py  the Windows service. SYSTEM, from boot, owns the driver
  ipc.py         the named pipe everything else talks to it through
  icons.py       the tray icon, drawn in Python. No image files
  trayicon.py    Shell_NotifyIcon, the hidden window, the popup menu
  tray.py        the tray application: profile menu, live tooltip
  daemon.py      the loop in a terminal, for watching it work
  cli.py         one-shot commands, through the service if it is running
  probe.py       phase 0 - reads only
tools/
  install.ps1         create the service, add the tray to logon, start both
  uninstall.ps1       stop, release the fans, remove everything
  install-pawnio.ps1  driver check + module fetch
  find_temps.py       hunt for the CPU/GPU temperature registers
  battery_bench.ps1   measure what each CPU cap costs, in milliwatts
  probe_wmi.ps1       what vendor surfaces exist (they don't)
  probe_security.ps1  HVCI / blocklist status, i.e. why not WinRing0
aerofan.example.json  a documented config to copy into %ProgramData%\aerofan\
```

No third-party Python dependencies. `ctypes` only, same as fusion-kbd — the
service, the named pipe and the tray icon are all direct Win32 calls. That is
not purism: it means the install script works on a friend's laptop with nothing
on it but Python and PawnIO.

---

## Install

Once, from an **elevated** PowerShell, in the repo:

```powershell
.\tools\install-pawnio.ps1     # if you have not already
.\tools\install.ps1
```

That checks Windows, Python and PawnIO, creates `%ProgramData%\aerofan`,
registers the `AeroFan` service to start at boot as SYSTEM, adds the tray icon
to logon, and starts both. It installs **in place** — the service runs the
files where they are, so `git pull` followed by `Restart-Service AeroFan` is a
complete update. Nothing is copied into Program Files.

```powershell
.\tools\install.ps1 -Profile aggressive   # start on a curve rather than auto
.\tools\install.ps1 -NoTray               # service only
.\tools\uninstall.ps1                     # removes all of it
.\tools\uninstall.ps1 -RemoveData         # ... and the saved profile and log
.\tools\uninstall.ps1 -ResetCpuLimit      # ... and any CPU frequency cap
```

Uninstall stops the service and waits for it, because stopping is what hands
the fans back to the EC. Then it checks that custom mode really is off.

Requirements: 64-bit Windows, Python 3.10+, PawnIO, and `LpcACPIEC.bin` in
`modules\`.

---

## Using it

**The tray icon** is the everyday interface. It sits by the clock as a
pinwheel, coloured by profile — slate for auto, blue quiet, green balanced,
amber aggressive, red max, grey if the service is not running. Hover it:

```
aerofan - aggressive
CPU   72C  fan  90% (20/23)
GPU   68C  fan  90% (21/23)
CPU max 2.3 GHz
```

The percentages are the *applied* duty out of `0xB3`/`0xB4`, so in auto they
show the firmware's own decision rather than a command nobody is issuing. The
bracketed numbers are the real tachometers on their 0–23 scale. The fourth line
appears only when the CPU is actually capped.

Click for the menu. It is four blocks with lines between them: the fan
profiles, [the CPU frequency cap](#the-cpu-frequency-cap),
[the discrete GPU](#turning-the-discrete-gpu-off), then the log folder and Exit. **Exit sets the profile to auto before it closes**, so the fans are
never left on a curve with no tray watching over them. It deliberately does
*not* touch the CPU cap — see below for why.

**From any shell**, with no elevation at all, because the service does the work:

```powershell
python -m aerofan.cli status               # temperatures, duties, tachometers
python -m aerofan.cli profile aggressive   # switch curve
python -m aerofan.cli profile              # which one is active
python -m aerofan.cli auto                 # back to the EC's own curve
python -m aerofan.cli max                  # both fans to 100%
python -m aerofan.cli set 70               # both fans held at 70%
python -m aerofan.cli cpu                  # the current CPU frequency cap
python -m aerofan.cli cpu 2.3ghz           # cap it (2300 and 2.3 both work)
python -m aerofan.cli cpu unlimited        # remove the cap
```

The chosen profile is written to `%ProgramData%\aerofan\state.json` and applied
again at the next boot, before anyone logs in. That is the difference between
this and running the daemon by hand.

**Without the service**, the same commands drive the EC directly and need an
elevated shell, which is what the whole CLI used to be. They are never both
active: two processes on the EC mailbox interleave and corrupt each other's
transactions — there is no `Access_EC` mutant on this machine to serialise them
— so when the service is up, direct access is refused rather than raced.

```powershell
python -m aerofan.daemon --profile aggressive   # the loop, in a terminal
python -m aerofan.winservice run                # the service engine, in a terminal
sc query AeroFan                                # is it running
Get-Content $env:ProgramData\aerofan\aerofan.log -Tail 40
Get-Content $env:LOCALAPPDATA\aerofan\tray.log -Tail 40
```

### When the tray misbehaves

Two logs, because there are two processes. The service writes
`%ProgramData%\aerofan\aerofan.log`; the tray writes
`%LOCALAPPDATA%\aerofan\tray.log`, in your profile rather than ProgramData
because it runs unelevated.

The tray log is there because `pythonw.exe` gives a tray icon no console and no
stderr, so without it every failure inside the tray is silent — which is
exactly how a bug where *clicking a menu item did nothing at all* survived a
full round of testing. It records menu opens, what each click resolved to, and
whether the resulting call to the service succeeded:

```
00:48:09  INFO  opening the menu (NIN_SELECT)
00:48:11  INFO  menu closed: id=1003 -> ('profile', 'quiet')
00:48:11  INFO  command: ('profile', 'quiet')
00:48:11  INFO  profile quiet: applied
```

If a click does nothing, that four-line group says which of the four steps is
missing. Add `--verbose` to the tray's command line to log every notification
the shell sends the icon as well.

> **The bug that log was written for.** A notification icon at
> `NOTIFYICON_VERSION_4` reports one click *twice*: the version 4 notification
> (`WM_CONTEXTMENU` for right, `NIN_SELECT` for left) **and** the raw button
> message (`WM_RBUTTONUP`, `WM_LBUTTONUP`). Acting on both opened the menu
> twice, nested, because `TrackPopupMenu` is modal and pumps messages itself —
> and the inner call reset the identifier-to-action map that the outer call was
> still going to look its result up in. The click resolved to nothing and the
> menu appeared inert. Now only the version 4 notifications open a menu, the
> map is a local rather than shared state, and a re-entrant open is refused.

### Profiles

| Name | What it does |
|---|---|
| `auto` | The EC's own firmware curve. aerofan does not touch the fans. The resting state, and where every failure path lands. |
| `quiet` | Stays off as long as it safely can. |
| `balanced` | Ramps with the work. Quiet at idle. |
| `aggressive` | Loud and early. Big thermal headroom for long sessions. |
| `max` | Both fans at 100%. The pre-game button. |
| `fixed:NN` | Both fans held at NN%. What `aerofan set NN` becomes. |

The three curve profiles live in `curve.py` and are yours to edit. Tuning knobs
— poll interval, temperature registers, cross-coupling — go in
`%ProgramData%\aerofan\aerofan.json`, which is never written by aerofan itself.
`aerofan.example.json` in the repo is a documented starting point; copy it
there and restart the service. Anything you leave out keeps its default from
`supervisor.DEFAULT_CONFIG`.

A BOM is fine. Notepad and PowerShell's `Set-Content -Encoding UTF8` both write
one, and `json.loads` rejects it outright, so the config and the saved profile
are read as `utf-8-sig`. Getting this wrong is silent by nature — the service
logs the parse failure and carries on with defaults, which looks exactly like
the file having no effect.

### The CPU frequency cap

The other half of making a laptop quieter: don't let the CPU generate the heat
in the first place. The tray's middle block caps the maximum processor
frequency, which is the same setting as

```powershell
powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCFREQMAX 2300
powercfg /setdcvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCFREQMAX 2300
powercfg /setactive SCHEME_CURRENT
```

reached through `powrprof.dll` directly rather than by spawning `powercfg`.
Defaults are 2.3 GHz, 3 GHz, 4 GHz and unlimited; change them with
`cpu_max_choices` in `aerofan.json`.

**That third line is the one everybody forgets.** `setacvalueindex` writes the
value into the power scheme and nothing more — until the scheme is made active
again the CPU carries on exactly as before. It is the usual reason a hand-typed
cap appears to do nothing.

**Both AC and DC are written.** A menu item saying "CPU max 2.3 GHz" should
mean 2.3 GHz; setting only the AC index means the cap silently disappears when
you unplug while the tick mark goes on claiming it is there. Set
`cpu_max_power_sources` to `["ac"]` if you want the old behaviour.

**Nothing here is remembered or re-applied by aerofan**, unlike the fan
profile. The value lives in the power scheme and survives reboots on its own,
which is also why Exit and uninstall leave it alone — you set it deliberately
and it is not aerofan's to revert. The consequence worth knowing is that it
belongs to *a* scheme, so if something switches schemes the cap does not
follow. Gigabyte's Smart Manager does exactly that, and the active scheme on
this machine is called "Smartmanager High performance". Everything reads the
live value rather than a remembered one, so when that happens the menu tells
you the truth instead of a comfortable fiction.

`uninstall.ps1` warns if it is about to leave a cap behind, and clears it with
`-ResetCpuLimit`. A throttled laptop with nothing left on it to explain why is
a bad thing to hand someone.

Choices are 1.4, 2.3, 3 and 4 GHz plus unlimited. 1.4 is deliberately well
under the 2300 MHz base clock — a "taking notes in a lecture" setting rather
than a performance one.

Which cap is actually cheapest is not a question worth guessing at, so
`tools\battery_bench.ps1` measures it. Unplug, run it, and it sets each cap in
turn and reads the battery's own discharge rate in milliwatts. Two things it
exists to settle: a frequency cap only bites when something asks for
performance, and note-taking leaves the CPU idle most of the time — so the gap
between caps is usually smaller than people expect. And **race to idle** cuts
the other way: a slower CPU can use *more* energy for a burst of work because
it stays awake longer doing it. Measure, then pick.

### Turning the discrete GPU off

| | |
|---|---|
| `python -m aerofan.cli gpu` | is it on |
| `python -m aerofan.cli gpu off` | switch it off — no reboot, about 5 W back |
| `python -m aerofan.cli gpu on` | switch it back |

Also one item in the tray, whose wording is the state: "Disable discrete GPU"
when it is on, "Enable discrete GPU" when it is off. No UAC prompt, because
the service already runs as SYSTEM.

Measured on this laptop: the RTX 2070 Super Max-Q idles at P8, 300 MHz, **5.3 W
and never lower**. It is not reaching RTD3 deep sleep, because a dozen ordinary
processes — explorer, Spotify, PowerToys, the terminal — each hold a handle on
it just by enumerating DXGI adapters. Short of closing all of them, taking the
device away is the only way to get that back.

It goes through `pnputil /disable-device`, which takes about seven seconds and
needs no reboot. Reading the state does **not** go through pnputil: its output
is localised, so parsing it would work here and quietly fail on a German
install. The state comes from `ConfigFlags` bit 0 in the registry instead,
which is not translated.

Two things to know. It **refuses to disable the last display adapter** — on
this machine the Intel UHD drives the panel and the NVIDIA drives nothing
(`nvidia-smi` reports `display_active: Disabled`), so there is a real fallback;
without one the menu item is greyed out rather than left to fail. And the
setting **persists across reboots**, because it lives in the device rather than
in aerofan — the same trap as the CPU cap, so `uninstall.ps1` warns about it
and `-ResetGpu` puts it back.

> **This used to blind aerofan.** When the only working temperature source was
> `nvidia-smi`, disabling the GPU meant no readings at all: the stuck detector
> fired, the error budget drained, and the fans went back to the EC every
> sixty seconds forever. Finding `0x60` and `0x61` is what made this safe, and
> it cuts the other way too — with an EC register configured, `nvidia-smi` is
> not called at all. It was costing a process launch every two seconds, and
> measured, the first call after an idle spell took the GPU from P8 at 5 W to
> P0 at 25 W. Polling a GPU to ask its temperature is what keeps it awake. Set
> `nvidia_smi_fallback` if you want it back as a second source.

> **On `PROCFREQMAX1`.** That is the same setting for efficiency class 1 — the
> E-cores on a hybrid CPU — at the same GUID with the last byte incremented.
> This machine is an **i7-10875H**: Comet Lake, eight identical cores, no
> E-cores, and `PROCFREQMAX1` does not appear in `SUB_PROCESSOR` at all. So
> setting it here has never done anything. aerofan writes it anyway on the
> machines that have it, and treats its absence as normal rather than as a
> failure.

### How it fails

Everything ends in the same place: the fans go back to the EC. The firmware's
own curve is conservative and always available, and it is a far better fallback
than a duty nobody is managing. That happens on no usable temperature reading,
on repeated *write* failures, on the watchdog not seeing a tick, on suspend, on
service stop, and on Exit from the tray.

Read failures are treated differently, and deliberately: this EC drops reads
regularly under normal load. A failed tachometer read costs the tooltip four
seconds of freshness and nothing else, and an unreadable custom-mode switch
means "assume nothing changed" rather than "throw away this tick", because the
tick contains the duty write that actually matters. Only failed writes and a
total loss of temperature spend the error budget. When that budget goes, the
service releases the fans, waits a minute, and tries the profile again — a
service cannot exit the way the daemon could.

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
