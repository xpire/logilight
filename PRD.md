# PRD — LogiLight Snap with GUI

Status: draft for review
Target: Ubuntu 24.04 LTS (core24), amd64 + arm64
Scope of this document: analysis of the existing project, the packaging/permission
decisions that follow from it, and the implementation plan for a GUI snap.

---

## 0. Decisions in one screen

| Question | Decision |
| --- | --- |
| Wrap `g810-led` directly, or keep the existing Python reimplementation? | **Wrap the CLI directly.** Delete the reimplementation's driver, systemd and YAML layers. |
| How does the app get permission to talk to the keyboard? | Three separate layers, only one of which is a real problem. Solved by **snap daemon running as root + `raw-usb` plug**. No `sudo`, no polkit, no udev rule on the host. |
| Strict or classic confinement? | **Strict.** Classic was the fallback if `raw-usb` didn't cover the device path; it does — once you build g810-led with `LIB=libusb`. |
| Does the snap need a Python venv? | **No.** The rewrite has zero pip dependencies; GTK/PyGObject come from the platform. A venv would only be needed for the non-snap install path. |
| GUI toolkit? | **GTK4 + libadwaita**, via the `gnome` snapcraft extension. Stdlib web UI is the documented fallback if the GI staging spike fails. |
| Mouse support? | **Out of scope for v1.** Piper already does this (`apt install piper`). |
| Where does "apply at boot" live? | **In the app**, not in systemd/udev — `daemon: simple` starts at boot and a 3 s poll catches hot-plug. |

---

## 1. Problem

The existing project (`ProfessorMoose74/Logitech-Gaming-Keyboard-Mouse-Controls`,
"LogiLight") is a Python CLI that wraps `g810-led` and `libratbag`. It works only
after a `sudo ./install.sh` that installs distro packages, `pip install`s into
the system Python, adds the user to `input,games`, and starts `ratbagd`. Its own
README roadmap lists "GUI application (PyQt/GTK)" as an open item.

Goal: ship the same capability as an installable Ubuntu snap with a GUI, with no
host-side setup beyond one interface connection, and no `sudo` at runtime.

---

## 2. What the existing code actually is

~1000 LOC across 6 modules, MIT, 7 commits, no tests, no CI (only issue templates).

| Module | LOC | Verdict |
| --- | --- | --- |
| `logilight/devices/detector.py` | 111 | Keep the idea, rewrite the implementation |
| `logilight/devices/keyboard.py` | 120 | Keep, fix |
| `logilight/devices/mouse.py` | 135 | Broken; drop for v1 |
| `logilight/utils/config.py` | 119 | Keep, drop the YAML dependency |
| `logilight/utils/systemd.py` | 185 | **Delete** — duplicates g810-led's own udev rule |
| `logilight/cli.py` | 310 | Keep as reference for the GUI's feature set |

The README links to `docs/INSTALLATION.md`, `docs/USAGE.md`,
`docs/SUPPORTED_DEVICES.md` and `docs/TROUBLESHOOTING.md`. **The `docs/`
directory does not exist.** Four of the seven documentation links are dead.

### 2.1 Defects found

These are not style notes; each one breaks a feature.

1. **`enable-startup` never applies a colour.** `keyboard.py:89` builds the boot
   script with `"\\nexit 0\\n"` — an escaped backslash in a non-raw string, so the
   generated file contains literal `\n` instead of newlines. Verified output:

   ```
   /usr/bin/g910-led -a ff0000\n\nexit 0\n
   ```

   The whole thing is one line, so g810-led receives `ff0000\n\nexit` and `0\n`
   as arguments and the boot service silently fails. `systemd.py:90` has the same
   bug in the unit's `After=`/`Wants=` lines.

2. **Mouse control never works.** `mouse.py:38` parses `ratbagctl list` with
   `^([a-z-]+):`. Real device IDs contain digits (`logitech-g502`), so the regex
   fails, `device_id` stays `None`, and `set_color()` returns `False` without
   doing anything. Every mouse feature in the README is dead code.

3. **Duplicate PID key.** `detector.py:32-33` maps `c087` twice (G604 then G703).
   The G604 is silently clobbered and can never be detected.

4. **Missing devices.** The keyboard table omits `c331`, `c335`, `c338` and
   `c33f` — that is the G810 Orion Spark, G910 Orion Spectrum, a second G610
   variant and the G815.

5. **Wrong effect name.** `keyboard.py:75` sends `-fx waves`. g810-led has no
   `waves` effect; the real names are `hwave`, `vwave` and `cwave`.

6. **`load` is a stub.** `cli.py:293` is `# Implementation would apply saved
   settings`. Profiles can be saved and listed, never applied.

7. **`sudo` on every device write.** `keyboard.py:49,68,72,75`. Unnecessary (see
   §4) and impossible inside a snap.

8. **Install script is broken on modern Ubuntu.** `scripts/install.sh` uses
   `pip3 install` (fails under PEP 668 on 23.04+) and `python3 setup.py install`
   (removed in setuptools 80+).

9. **`detector.py:97` probes for `g213-led`** and reports the result as
   `g810-led`, so tool-availability results are wrong for every non-G213 user.

10. **`check_status()` never matches.** `systemd.py:154` runs
    `systemctl list-units 'logilight-*'` without `--all` or `glob` expansion
    through a shell, so the pattern is passed literally and matches nothing.

### 2.2 The architectural mistake

`logilight/utils/systemd.py` (185 lines) generates per-device shell scripts, writes
them to `/usr/local/bin`, generates `/etc/systemd/system/logilight-*.service`
units, and calls `sudo systemctl enable` on each. All of this exists to re-apply
lighting after a reboot.

**g810-led already ships that.** From its own `udev/g810-led.rules`:

```
ACTION=="add", SUBSYSTEMS=="usb", ATTRS{idVendor}=="046d", ATTRS{idProduct}=="c32b",
  MODE="660", TAG+="uaccess", RUN+="/usr/bin/g910-led -p /etc/g810-led/profile"
```

One udev rule does persistence, permissions and per-device dispatch. The 185-line
systemd module is a worse duplicate that additionally requires root at runtime.

---

## 3. Should we wrap the original CLI directly?

**Yes.** `g810-led` is not a competitor to be abstracted away, it is the driver.
It already exposes:

- `-a <color>` all keys, `-k <key> <color>` one key, `-g <group> <color>` a group
- `-fx <effect> <target> [color] <speed>` for `color`, `breathing`, `cycle`,
  `hwave`, `vwave`, `cwave`, `random`
- `-p <file>` and `-pp` to load a profile, where a profile is a plain text file
  (`a ffffff` / `k w ff0000` / `c` to commit)
- `-s color` for the power-on effect
- `-d` to pick a device when several are attached

A GUI needs to *emit those arguments*, not to reimplement them. What the wrapper
should keep is the thin layer the CLI genuinely lacks:

1. **Turning a PID into the right argv[0].** g810-led selects the keyboard layout
   from the program name (`g910-led` vs `g810-led`), so something must map
   `046d:c32b` → `g910-led`. That is a data table, not a driver.
2. **Persistence across reboots** in a way a confined snap can use.
3. **A settings store** and a UI.

Everything else in the existing `keyboard.py` is avoidable, and `systemd.py` is
actively harmful. The rewrite deletes the `pyyaml`, `click` and `rich`
dependencies in the process: profiles become JSON (stdlib), and the GUI replaces
the CLI.

**Consequence:** the snap has **zero pip dependencies**. This is what removes the
need for a venv (§8) and cuts the snap size to base + g810-led + PyGObject.

---

## 4. "Permission to use the g910-led CLI internally"

This is three separate mechanisms that are easy to conflate. Only the middle one
was the project's actual problem.

### 4.1 AppArmor / snap interface — the real constraint

A `g810-led` built the default way (hidapi) opens **`/dev/hidraw*`**. I checked
snapd's interface definitions:

- **`raw-usb`** tags `SUBSYSTEM=="usb"`, `SUBSYSTEM=="usbmisc"`, and USB `tty`
  only. Its AppArmor grants `/dev/bus/usb/***/** rw` and some `/sys` paths.
  **It does not grant `/dev/hidraw*` at all.**
- **`hidraw`** exists and does grant `/dev/hidraw*`, but it is per-path on the
  plug side and validates `usb-vendor`/`usb-product` on the **slot** side. Those
  slot attributes come from a gadget snap. On Ubuntu Desktop there is no such
  gadget, so **the `hidraw` interface is not usable here.**
- Both interfaces set `deny-auto-connection: true`, so either way the user runs
  `sudo snap connect logilight:raw-usb` once.

**Therefore the snap must build g810-led with `LIB=libusb`.** That switches it
from `/dev/hidraw*` to libusb's usbfs backend at `/dev/bus/usb/*`, which
`raw-usb` covers. The upstream docs frame libusb as a fallback ("hidapi is more
responsive, ~20 ms vs ~150 ms"); for lighting that difference is irrelevant
because effects are programmed into the keyboard's firmware with a single control
transfer, not streamed. This is the crux of the whole packaging design: **the
distro's `g810-led` package cannot simply be staged, because it is built with
hidapi.**

### 4.2 Unix device permissions — already solved, and never needed `sudo`

Snapcraft's own `raw-usb` documentation is explicit: the interface "will NOT
supersede the classic Unix file permission model, so the user still needs to have
sufficient r or w permission to the device node by either run the snap command as
root or have a designated udev rule."

Two ways to satisfy it, and we take both ends of the simplest one:

- **Run as root.** A snap `daemon: simple` app runs as root unless a `user:` is
  set. Our daemon is the only process that touches hardware, so this is one line
  in `snapcraft.yaml` and no privilege gymnastics anywhere else.
- *Or* ship a udev rule — impossible for a strictly confined snap to install on
  the host, and unnecessary once the daemon is root.

### 4.3 Why the original project's `sudo` was never needed

g810-led's shipped udev rule sets `MODE="660"` and `TAG+="uaccess"`. `uaccess`
makes systemd-logind grant an ACL on the device node to the **logged-in desktop
user**. So once the distro package is installed, an ordinary user can already run
`g910-led` without `sudo`. Every `['sudo', ...]` in `keyboard.py` is solving a
problem that was solved by the package they told users to install.

### 4.4 What we deliberately do *not* do

- **No polkit helper.** Only needed if a privileged action had to be triggered by
  the unprivileged GUI, which the socket + root daemon avoids entirely.
- **No `sudo` anywhere.** `/usr/bin/sudo` is not in the snap and AppArmor blocks
  executing host binaries, so it is not merely undesirable but impossible.
- **No host udev rule, no `input`/`games` group membership, no re-login.**

---

## 5. Snap design

### 5.1 Confinement

`confinement: strict`, `base: core24`. Strict is viable precisely because of the
libusb decision in §4.1. If that ever regresses, `confinement: classic` is the
one-line escape hatch (at the cost of store review and Ubuntu Core support).

### 5.2 Apps

```yaml
apps:
  logilight:                 # GUI, runs as the user, no device privileges
    command: bin/logilight-gui
    extensions: [gnome]
    desktop: snap/gui/logilight.desktop

  logilight-daemon:          # runs as root, owns all hardware access
    command: bin/logilight-daemon
    daemon: simple
    restart-condition: on-failure
    plugs: [raw-usb]
```

The privilege split is the point: the GUI needs no `raw-usb`, no sysfs access and
no elevation. It speaks one small socket protocol. A bug in the toolkit cannot
reach the hardware directly.

### 5.3 Parts

**`g810-led`** — built from source, not staged from the archive:

```
make bin LIB=libusb
install -Dm755 bin/g810-led "$CRAFT_PART_INSTALL/usr/bin/g810-led"
for name in g213 g410 g413 g512 g513 g610 g815 g910 gpro; do
  ln -sf g810-led "$CRAFT_PART_INSTALL/usr/bin/${name}-led"
done
```

Two traps here:
- Upstream's `make setup` installs **absolute** symlinks (`/usr/bin/g810-led`).
  Inside a snap that resolves to the host, which is outside the sandbox. The
  symlinks must be relative. We therefore do not use `make setup` — it also
  writes `/etc/udev/rules.d` and a systemd unit, both meaningless in a snap.
- The symlinks are not optional: g810-led chooses the layout from `argv[0]`, and
  there is a single binary. `g910-led` is a symlink, never a separate build.

**`logilight`** — the Python package, dumped into `$SNAP/lib/logilight`, plus
`python3-gi` staged from the 24.04 archive for the GUI.

### 5.4 Why no `hardware-observe`

Device discovery reads `/sys/bus/usb/devices/*/idVendor`. That path is a symlink
which AppArmor resolves to `/sys/devices/pci0000:00/.../usb1/1-11/idVendor` —
matching `raw-usb`'s existing `/sys/devices/pci**/usb[0-9]** r,` rule. Verified
against this machine's real sysfs. No extra interface needed, and no `lsusb`
binary either (it is not in the snap and only formats these files).

---

## 6. Runtime architecture

```
        user                                    root
┌───────────────────────┐            ┌───────────────────────────┐
│ logilight (GUI)       │  unix      │ logilight-daemon          │
│ GTK4 + libadwaita     │  socket    │ · applies settings        │
│ no device privileges  │ ─────────► │ · watches for hot-plug    │
│                       │  JSON      │ · persists the profile    │
└───────────────────────┘            └────────────┬──────────────┘
                                                  │ exec
                                     /snap/logilight/current/usr/bin/g910-led
```

**Socket**: an **abstract** AF_UNIX socket named `@snap.logilight.daemon`, one JSON
object per line, one reply per connection. Commands: `status`, `apply`, `save`,
`load`, `delete`.

Abstract, not a pathname in `$SNAP_COMMON`, and that is not a preference. Two
independent gates both have to be open, and missing either one produces the same
bare `EPERM` (errno 1) from `bind`:

**AppArmor** grants abstract addresses only:

```
unix (bind, listen) addr="@snap.@{SNAP_INSTANCE_NAME}.**",
unix peer=(label=snap.@{SNAP_INSTANCE_NAME}.*),
```

**seccomp** does not allow `bind` at all. The base profile permits
`socket AF_UNIX` but `bind`/`accept`/`listen` come from the `network-bind`
interface, whose seccomp snippet is exactly `accept`, `accept4`, `bind`,
`listen`. snapd's seccomp template even comments that a `bind` syscall is only
added to the allowlist as a workaround *when AppArmor is disabled*.

So the daemon needs **both** `network-bind` and an abstract address. The
interface auto-connects: its base declaration carries no
`deny-auto-connection`.

Abstract sockets are a good fit anyway: no filesystem permissions to set, and
nothing is left behind when the daemon dies, so there is no stale socket to
clean up. The peer rule keeps other snaps from connecting, which is tighter than
a world-writable socket file.

**Boot**: snapd starts the daemon at boot; it applies the saved profile and
starts a 3 s poll that re-applies when a new keyboard appears. This replaces the
entire systemd module. A confined snap cannot receive udev events, and polling
five sysfs files is cheaper than the machinery to avoid it.

**Security posture.** The socket's entire capability is "change LED colours".
Input validation is strict regardless, because the daemon is root and the
arguments end up in an `argv`: colour must match `^[0-9a-f]{6}$`, effect and
target come from fixed allow-lists, speed is clamped to 1–100, and profile names
are sanitised before being used as filenames. Every one of these has a test.

---

## 7. GUI specification

Single `Adw.PreferencesPage` in a window, four groups:

**Devices** — one row per detected keyboard (`G910 Orion Spark`, `USB 046d:c32b`),
`Rescan` in the group header, "No keyboard detected" when empty.

**Lighting** — these write straight through, debounced 200 ms:
- Effect: combo of the seven real g810-led effects
- Apply to: all keys / keys / logo
- Colour: `Gtk.ColorDialogButton` (the native picker)
- Speed: 1–100 slider, converted to the hex byte g810-led wants

Colour and speed rows hide themselves for effects that don't take them
(`cycle`, `hwave`, `vwave`, `cwave`, `random`) — driven by `core.uses()`.

**Startup** — a single "Apply on boot" switch.

**Presets** — name field + Save, then a row per preset with Load and Delete.

Errors surface in an `Adw.Banner` (daemon unreachable, no device present) rather
than a toast, so a persistent failure stays visible while dragging a slider.

---

## 8. venv or not

The brief suggested a venv for dependencies. **The rewrite makes this
unnecessary**, which is the desirable outcome:

- Runtime dependencies are now zero (JSON replaced PyYAML, the GUI replaced
  Click/Rich). The only third-party module is `gi`, which cannot be pip-installed
  meaningfully anyway — it is a binding to the system's GTK.
- In the snap, GTK4/libadwaita come from the `gnome` extension, and `python3-gi`
  is staged from the archive because it must match the base's Python version.
- A runtime venv would add startup cost, a second Python on disk, and a new class
  of "which interpreter am I in" bugs.

A venv remains correct for a **non-snap** install, where the user's distro GTK
may not match. That path is two lines in the README (`python3-gi` from apt, no
pip) and is not a supported artifact.

---

## 9. Scope

**In v1**: keyboard discovery; the seven effects; colour/target/speed; apply on
boot; hot-plug re-apply; presets; snap packaging for amd64/arm64.

**Out of v1**: mice (use Piper); per-key colour editor; audio-reactive lighting;
syncing across devices; OpenRGB backend; Ubuntu Core support; store publication.

---

## 10. Milestones

**M0 — de-risk (do first, ~half a day).** Build a hello-world core24 snap with
`extensions: [gnome]` that stages `python3-gi` and opens a window. This is the
single riskiest unknown in the plan; everything else is known-good. If GI staging
fights back, switch the GUI to the stdlib web UI fallback (§11) and lose a day,
not a week.

**M1 — g810-led in a snap.** Build with `LIB=libusb`, create the symlinks, verify
`g910-led -a 8000ff` works inside the sandbox once `raw-usb` is connected. Confirm
`-fx` effects. This validates §4.1 on real hardware. A G910 (`046d:c32b`) is the
reference device.

**M2 — daemon.** Socket protocol, boot apply, hot-plug poll, profile store.

**M3 — GUI.** The window above, against a running daemon.

**M4 — packaging and polish.** Desktop entry, icon, `snap connect` instructions,
README, `snapcraft lint`, arm64 build.

---

## 11. Risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| PyGObject + gnome extension staging mismatch | GUI won't start | **M0 spike.** Fallback: stdlib `http.server` UI + `<input type="color">`, ~80 lines, no GUI dependencies at all |
| libusb backend behaves differently from hidapi on some models | Some keyboards fail | Tested in M1 before the GUI exists. Upstream treats libusb as a supported fallback, and adds a `LIB=libusb` build path |
| `raw-usb` is broad and non-autoconnecting | Manual `snap connect`, store review friction | Documented in the install instructions. `hidraw` per-path plugs are tighter but unusable on Desktop (§4.1) |
| g810-led built from an unpinned `master` | Build breaks or behaviour drifts | Pin to a commit SHA before publishing (marked in the recipe) |
| GTK 4.10+ / libadwaita 1.3+ APIs (`ColorDialogButton`, `Banner`) | Unavailable on older desktops | Fine for core24 + gnome-46; the non-snap path is best-effort only |
| GUI code is unverified by me | Widget-API typos | Phase 1 of implementation is a GTK smoke run on a real desktop; the pure logic already has tests |

---

## 12. Acceptance criteria

1. `snapcraft` produces a `logilight_*.snap` for amd64 and arm64 with no linter errors.
2. `snap install --dangerous` then `snap connect logilight:raw-usb` is the complete setup. No `sudo`, no pip, no group changes, no reboot.
3. Launching LogiLight shows the attached G910 by name.
4. Changing effect, colour, target and speed changes the keyboard within ~1 s.
5. "Apply on boot" set → unplug, replug → settings return without user action.
6. Presets save, load and delete; survive a reboot.
7. `python3 tests/test_core.py` passes (11 checks, currently green).
8. No `sudo` anywhere in the snap's runtime path, and no writes outside
   `$SNAP_COMMON` and `$SNAP_DATA` (profiles only; the socket is abstract and
   touches no filesystem).
9. `journalctl -u snap.logilight.logilight-daemon` shows no errors on a normal boot.

---

## 13. Deferred / v2

- Mice via a bundled `ratbag-command` (direct hidraw, no daemon) — needs
  verification that Ubuntu ships it.
- Per-key colour editing: g810-led's `-k` and profile format already support it.
- Import existing g810-led profiles from `/etc/g810-led/profile`.
- A CLI app in the snap for scriptability (the daemon already has `--once` and
  `--status` for this).
- Report the upstream bugs from §2.1 back to the original project — items 1, 2
  and 3 are each a one-line fix and are user-visible.
