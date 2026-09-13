"""End-to-end check: CLI -> unix socket -> daemon -> g810-led argv.

Runs the real daemon with a stub g810-led, so it needs neither a keyboard nor
root.

It runs the whole scenario twice. Once over a pathname socket, and once over the
abstract socket the snap has to use: snapd's AppArmor template grants
`unix (bind, listen) addr="@snap.<instance>.**"` and nothing for a pathname
bind, so binding a socket in $SNAP_COMMON fails with EPERM.

    python3 tests/test_integration.py
"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logilight import cli, core, daemon  # noqa: E402


def wait_for_daemon(deadline=5.0):
    """Retry until the daemon answers.

    A pathname socket could be polled for with Path.exists(), but an abstract one
    has no path, so the only transport-agnostic readiness check is to connect.
    """
    end = time.time() + deadline
    last = None
    while time.time() < end:
        try:
            core.request({"cmd": "status"}, timeout=0.5)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.05)
    raise AssertionError(f"daemon never answered on {core.socket_address()!r}: {last}")


def run_scenario(label, abstract=False):
    original_address = core.socket_address
    original_detect, original_resolve = core.detect, core.resolve

    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp)
        core.os.environ["LOGILIGHT_STATE"] = str(state)
        if abstract:
            # An env var cannot carry this: os.environ rejects embedded NULs,
            # and an abstract AF_UNIX address is a leading NUL plus the name.
            core.os.environ.pop("LOGILIGHT_SOCKET", None)
            core.socket_address = lambda: f"\0snap.logilight.mvp{os.getpid()}"
        else:
            core.os.environ["LOGILIGHT_SOCKET"] = str(state / "test.sock")

        argv_log = state / "argv"
        stub = state / "g910-led"
        stub.write_text(f'#!/bin/sh\necho "$@" >> {argv_log}\nexit 0\n')
        stub.chmod(0o755)

        core.detect = lambda: [{"pid": "c32b", "name": "G910 Orion Spark", "binary": "g910-led"}]
        core.resolve = lambda binary: str(stub)

        stop = threading.Event()
        server = threading.Thread(target=daemon.serve, args=(stop,), daemon=True)
        server.start()
        wait_for_daemon()

        assert cli.main(["set", "--color", "ff0000"]) == 0
        assert cli.main(["set", "--effect", "breathing", "--color", "00ff00", "--speed", "12"]) == 0
        assert cli.main(["set", "--effect", "hwave"]) == 0
        assert cli.main(["status", "--json"]) == 0
        assert cli.main(["set", "--color", "not-a-colour"]) == 1, "bad colour must fail loudly"

        called = argv_log.read_text().strip().splitlines()
        expected = [
            "-fx color all ff0000",
            "-fx breathing all 00ff00 0c",
            "-fx hwave all 0c",  # colour dropped; speed carried over
        ]
        assert called == expected, f"\n got: {called}\nwant: {expected}"

        saved = core.load_profile()
        assert saved["effect"] == "hwave" and saved["color"] == "00ff00", saved

        stop.set()
        server.join(timeout=5)
        assert not server.is_alive(), "daemon did not shut down"

    core.socket_address = original_address
    core.detect, core.resolve = original_detect, original_resolve
    print(f"ok  [{label}] 3 commands reached g810-led with the expected arguments")
    print(f"ok  [{label}] bad colour rejected without contacting the daemon")
    print(f"ok  [{label}] settings persisted across invocations")
    print(f"ok  [{label}] daemon shut down cleanly")


def check_boot_apply_never_kills_the_daemon():
    """Regression: the boot apply used to run before the socket was bound, so an
    exception there killed the process and every client saw only "cannot reach
    the service", with nothing to explain why."""

    def boom():
        raise RuntimeError("sysfs exploded")

    original = core.detect
    core.detect = boom
    try:
        daemon.boot_apply()  # must not raise
    finally:
        core.detect = original
    print("ok  a failing boot apply is logged, not fatal")


def main():
    check_boot_apply_never_kills_the_daemon()
    run_scenario("pathname")
    run_scenario("abstract", abstract=True)


if __name__ == "__main__":
    main()
