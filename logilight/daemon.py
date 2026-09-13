"""LogiLight daemon: applies lighting, remembers it, and serves the GUI.

Inside the snap this is the `logilight-daemon` app.  snapd starts it at boot as
root, and running as root is what grants write access to the keyboard's USB
device *without* sudo, without a udev rule and without group membership -- the
three things the original project's `sudo` calls were papering over.

It is also the only process that talks to hardware, so the GUI needs no device
privileges at all.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

from . import core

# How often to look for a keyboard being plugged in.  There is no udev-triggered
# wakeup available to a confined snap, and polling 5 sysfs files is free.
POLL_SECONDS = 3.0

_LOCK = threading.Lock()


# ---------------------------------------------------------------- hardware

def apply(settings: dict) -> dict:
    """Push settings to every attached keyboard. Returns a JSON-able result."""
    settings = core.clean(settings)
    devices = core.detect()
    if not devices:
        return {"ok": False, "error": "no Logitech RGB keyboard detected", "applied": []}

    applied, errors = [], []
    with _LOCK:
        for dev in devices:
            argv = [core.resolve(dev["binary"])] + core.effect_args(
                settings["effect"], settings["target"], settings["color"], settings["speed"]
            )
            proc = subprocess.run(argv, capture_output=True, text=True)
            if proc.returncode == 0:
                applied.append(dev["name"])
            else:
                errors.append(f"{dev['name']}: {(proc.stderr or proc.stdout).strip()}")

    return {"ok": not errors, "applied": applied, "errors": errors}


def watch_hotplug(stop: threading.Event) -> None:
    """Re-apply the saved profile whenever a new keyboard shows up."""
    seen = {d["pid"] for d in core.detect()}
    while not stop.wait(POLL_SECONDS):
        now = {d["pid"] for d in core.detect()}
        if now - seen:
            active = core.load_profile()
            if active["enabled"]:
                apply(active)
        seen = now


# ---------------------------------------------------------------- protocol

def handle(cmd: dict) -> dict:
    """One request -> one response. Never raises."""
    name = cmd.get("cmd")
    if name == "apply":
        saved = core.save_profile(cmd.get("settings") or core.DEFAULTS)
        result = apply(saved)
        result["settings"] = saved
        return result
    if name == "status":
        return {
            "ok": True,
            "devices": core.detect(),
            "settings": core.load_profile(),
            "profiles": core.list_profiles(),
        }
    if name == "save":
        saved = core.save_profile(cmd.get("settings") or {}, str(cmd.get("name", core.ACTIVE)))
        return {"ok": True, "settings": saved, "profiles": core.list_profiles()}
    if name == "load":
        saved = core.load_profile(str(cmd.get("name", core.ACTIVE)))
        result = apply(saved)
        result["settings"] = saved
        return result
    if name == "delete":
        core.delete_profile(str(cmd.get("name", "")))
        return {"ok": True, "profiles": core.list_profiles()}
    return {"ok": False, "error": f"unknown command: {name!r}"}


def serve(stop: threading.Event) -> None:
    path = core.socket_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    # ponytail: any local user can drive the LEDs. That is the whole privilege
    # the socket carries -- no shell, no file access, no escalation -- so the
    # ceiling is acceptable. Per-uid SO_PEERCRED checks if that ever changes.
    os.chmod(path, 0o666)
    server.listen(8)
    server.settimeout(1.0)

    try:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            with conn, conn.makefile("rwb") as stream:
                try:
                    reply = handle(json.loads(stream.readline() or b"{}"))
                except Exception as exc:  # noqa: BLE001 - one bad client must not kill the daemon
                    reply = {"ok": False, "error": str(exc)}
                stream.write(json.dumps(reply).encode() + b"\n")
    finally:
        server.close()
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------- entry point

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="logilight-daemon", description=__doc__)
    parser.add_argument("--once", action="store_true", help="apply the saved profile and exit")
    parser.add_argument("--status", action="store_true", help="print devices and settings and exit")
    args = parser.parse_args(argv)

    if args.status:
        print(json.dumps({"devices": core.detect(), "settings": core.load_profile()}, indent=2))
        return 0
    if args.once:
        print(json.dumps(apply(core.load_profile()), indent=2))
        return 0

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    threading.Thread(target=watch_hotplug, args=(stop,), daemon=True).start()

    active = core.load_profile()
    if active["enabled"]:
        print(json.dumps(apply(active)), flush=True)

    serve(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
