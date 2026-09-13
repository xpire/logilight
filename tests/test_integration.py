"""End-to-end check: CLI -> unix socket -> daemon -> g810-led argv.

Runs the real daemon on a real socket with a stub g810-led, so it needs neither a
keyboard nor root.  This is the test that proves the MVP actually does something.

    python3 tests/test_integration.py
"""

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logilight import cli, core, daemon  # noqa: E402


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
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp)
        core.os.environ["LOGILIGHT_STATE"] = str(state)
        core.os.environ["LOGILIGHT_SOCKET"] = str(state / "test.sock")

        # Stub out the two things that need real hardware.
        argv_log = state / "argv"
        stub = state / "g910-led"
        stub.write_text(f'#!/bin/sh\necho "$@" >> {argv_log}\nexit 0\n')
        stub.chmod(0o755)

        core.detect = lambda: [{"pid": "c32b", "name": "G910 Orion Spark", "binary": "g910-led"}]
        core.resolve = lambda binary: str(stub)

        stop = threading.Event()
        server = threading.Thread(target=daemon.serve, args=(stop,), daemon=True)
        server.start()
        for _ in range(100):
            if Path(core.os.environ["LOGILIGHT_SOCKET"]).exists():
                break
            time.sleep(0.05)
        else:
            raise AssertionError("daemon never created its socket")

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

    print(f"ok  3 commands reached g810-led with the expected arguments")
    print("ok  bad colour rejected without contacting the daemon")
    print("ok  settings persisted across invocations")
    print("ok  daemon shut down cleanly")


if __name__ == "__main__":
    main()
