"""LogiLight core: device discovery, g810-led driver, profile store, daemon protocol.

This module is imported by both the root daemon and the unprivileged GUI, so it
must never need root itself.  Only `daemon.apply()` touches hardware.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
from pathlib import Path

VENDOR = "046d"  # Logitech

# Overridable so the discovery path can be exercised without a keyboard.
USB_DEVICES = Path("/sys/bus/usb/devices")

# PID -> (display name, g*-led argv[0]).
# Source of truth: g810-led's own udev/g810-led.rules. The original LogiLight
# table was missing c331/c335/c338/c33f and mapped c337 to the wrong binary.
KEYBOARDS: dict[str, tuple[str, str]] = {
    "c336": ("G213 Prodigy", "g213-led"),
    "c330": ("G410 Atlas Spectrum", "g410-led"),
    "c33a": ("G413 Carbon", "g413-led"),
    "c342": ("G512 Carbon", "g512-led"),
    "c33c": ("G513 Carbon", "g513-led"),
    "c333": ("G610 Orion", "g610-led"),
    "c338": ("G610 Orion", "g610-led"),
    "c331": ("G810 Orion Spectrum", "g810-led"),
    "c337": ("G810 Orion Spectrum", "g810-led"),
    "c33f": ("G815 LIGHTSYNC", "g815-led"),
    "c32b": ("G910 Orion Spark", "g910-led"),
    "c335": ("G910 Orion Spectrum", "g910-led"),
    "c339": ("G Pro Keyboard", "gpro-led"),
}

# effect -> (label, positional argument shape). Verified against g810-led README
# samples: `-fx breathing all ff0000 0a`, `-fx hwave keys 0a`, `-fx color keys 00ff00`.
# NOTE: the effects are cwave/hwave/vwave historically -- there is no "waves".
_EFORMS: dict[str, tuple[str, ...]] = {
    "color": ("target", "color"),
    "breathing": ("target", "color", "speed"),
    "cycle": ("target", "speed"),
    "hwave": ("target", "speed"),
    "vwave": ("target", "speed"),
    "cwave": ("target", "speed"),
    "random": ("target", "speed"),
}

EFFECTS: dict[str, str] = {
    "color": "Solid colour",
    "breathing": "Breathing",
    "cycle": "Colour cycle",
    "hwave": "Horizontal wave",
    "vwave": "Vertical wave",
    "cwave": "Centre wave",
    "random": "Random",
}

# Effects that ignore the colour argument; the GUI greys the picker out for these.
COLORLESS = frozenset(e for e, form in _EFORMS.items() if "color" not in form)

TARGETS: dict[str, str] = {
    "all": "All keys",
    "keys": "Keys only",
    "logo": "Logo only",
}

DEFAULTS = {"effect": "color", "target": "all", "color": "8000ff", "speed": 10, "enabled": True}

_HEX = re.compile(r"\A[0-9a-f]{6}\Z")


# ---------------------------------------------------------------- validation

def normalise_color(value: object) -> str:
    """Lower-case 6-digit hex with no '#'. Raises ValueError on anything else.

    Shared by the argument builder and the CLI so that a bad colour is a loud
    error rather than a silent fallback to the default.
    """
    color = str(value).lower().lstrip("#")
    if not _HEX.match(color):
        raise ValueError(f"colour must be 6 hex digits (RRGGBB), got {value!r}")
    return color


def effect_args(effect: str, target: str = "all", color: str = "ffffff", speed: int = 10) -> list[str]:
    """Build the g810-led argument list for one effect. Pure; no I/O.

    Everything here is validated because it ends up in an argv executed as root.
    """
    if effect not in _EFORMS:
        raise ValueError(f"unknown effect: {effect!r}")
    if target not in TARGETS:
        raise ValueError(f"unknown target: {target!r}")

    color = normalise_color(color)
    speed = max(1, min(100, int(speed)))

    fields = {"target": target, "color": color, "speed": f"{speed:02x}"}
    return ["-fx", effect, *(fields[f] for f in _EFORMS[effect])]


def uses(effect: str, field: str) -> bool:
    """Whether this effect takes `field` ("color" or "speed")."""
    return field in _EFORMS.get(effect, ())


def resolve(binary: str) -> str:
    """Find a g*-led binary on PATH, falling back to $SNAP/usr/bin."""
    return shutil.which(binary) or str(Path(os.environ.get("SNAP", "/")) / "usr" / "bin" / binary)


# ---------------------------------------------------------------- discovery

def detect() -> list[dict]:
    """Return attached Logitech RGB keyboards as [{pid, name, binary}].

    Reads sysfs instead of shelling out to lsusb: lsusb is not present inside the
    snap and only formats these same files.  /sys/bus/usb/devices/*/idVendor is a
    symlink, and AppArmor resolves it to /sys/devices/pci*/usb[0-9]*/.../idVendor
    -- the one path the raw-usb interface grants read access to.
    """
    found: dict[str, dict] = {}
    try:
        entries = list(USB_DEVICES.glob("*/idVendor"))
    except OSError:
        # Confinement without the raw-usb plug connected can deny the listing.
        # An empty result is the honest answer; the caller turns it into a
        # message that names the likely fix.
        return []

    for vendor in entries:
        try:
            if vendor.read_text().strip().lower() != VENDOR:
                continue
            pid = (vendor.parent / "idProduct").read_text().strip().lower()
        except OSError:
            continue  # unplugged mid-walk, or blocked by confinement
        if pid in KEYBOARDS and pid not in found:
            name, binary = KEYBOARDS[pid]
            found[pid] = {"pid": pid, "name": name, "binary": binary}
    return [found[pid] for pid in sorted(found)]


# ---------------------------------------------------------------- profiles

ACTIVE = "active"


def state_dir() -> Path:
    """Where profiles live. $SNAP_COMMON under snapd, XDG otherwise."""
    override = os.environ.get("LOGILIGHT_STATE")
    if override:
        return Path(override)
    base = os.environ.get("SNAP_COMMON")
    d = Path(base) / "profiles" if base else Path.home() / ".config" / "logilight" / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def clean(raw: dict) -> dict:
    """Coerce anything read off disk into a settings dict that effect_args accepts."""
    out = dict(DEFAULTS)
    out["effect"] = raw.get("effect") if raw.get("effect") in EFFECTS else DEFAULTS["effect"]
    out["target"] = raw.get("target") if raw.get("target") in TARGETS else DEFAULTS["target"]
    try:
        out["color"] = normalise_color(raw.get("color", ""))
    except ValueError:
        out["color"] = DEFAULTS["color"]
    try:
        out["speed"] = max(1, min(100, int(raw.get("speed", DEFAULTS["speed"]))))
    except (TypeError, ValueError):
        out["speed"] = DEFAULTS["speed"]
    out["enabled"] = bool(raw.get("enabled", True))
    return out


def _path(name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", name)[:64] or "unnamed"
    return state_dir() / f"{safe}.json"


def load_profile(name: str = ACTIVE) -> dict:
    try:
        return clean(json.loads(_path(name).read_text()))
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_profile(settings: dict, name: str = ACTIVE) -> dict:
    settings = clean(settings)
    _path(name).write_text(json.dumps(settings, indent=2) + "\n")
    return settings


def list_profiles() -> list[str]:
    return sorted(p.stem for p in state_dir().glob("*.json") if p.stem != ACTIVE)


def delete_profile(name: str) -> None:
    if name != ACTIVE:
        _path(name).unlink(missing_ok=True)


# ---------------------------------------------------------------- daemon link

def socket_address() -> str:
    """AF_UNIX address for the daemon socket.

    Inside a snap this MUST be an abstract socket. snapd's AppArmor template
    grants `unix (bind, listen) addr="@snap.@{SNAP_INSTANCE_NAME}.**"` and
    grants nothing for binding a *pathname* socket, which fails with EPERM.
    Abstract sockets are also a better fit: no filesystem permissions to set,
    no stale socket to clean up after a crash, and the template's peer rule
    keeps other snaps from connecting.

    Outside a snap (and in the tests) a pathname socket is used, since it is
    easier to see and to poke at with socat.
    """
    override = os.environ.get("LOGILIGHT_SOCKET")
    if override:
        return override

    instance = os.environ.get("SNAP_INSTANCE_NAME") or os.environ.get("SNAP_NAME")
    if instance:
        return f"\0snap.{instance}.daemon"

    return str(Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "logilight.sock")


def request(payload: dict, timeout: float = 5.0) -> dict:
    """Send one newline-delimited JSON command to the daemon and read the reply."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(socket_address())
        sock.sendall(json.dumps(payload).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode() or "{}")
