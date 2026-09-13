#!/bin/bash
# LogiLight self-check. Run: python3 tests/test_core.py
#
# Covers the parts that fail silently and expensively: the argv handed to a
# root-owned binary, and the device table.

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logilight import cli, core  # noqa: E402


def test_effect_args_match_g810_led_samples():
    # Shapes copied from the g810-led README.
    assert core.effect_args("color", "keys", "00ff00", 10) == ["-fx", "color", "keys", "00ff00"]
    assert core.effect_args("breathing", "logo", "ff0000", 10) == ["-fx", "breathing", "logo", "ff0000", "0a"]
    assert core.effect_args("cycle", "all", "ffffff", 10) == ["-fx", "cycle", "all", "0a"]
    assert core.effect_args("hwave", "keys", "ffffff", 10) == ["-fx", "hwave", "keys", "0a"]
    assert core.effect_args("cwave", "all", "ffffff", 5) == ["-fx", "cwave", "all", "05"]
    assert core.effect_args("random", "logo", "ffffff", 1) == ["-fx", "random", "logo", "01"]


def test_every_effect_is_buildable():
    for effect in core.EFFECTS:
        argv = core.effect_args(effect)
        assert argv[0] == "-fx" and argv[1] == effect, argv


def test_effect_names_have_no_typo_waves():
    assert "waves" not in core.EFFECTS
    assert {"hwave", "vwave", "cwave"} <= set(core.EFFECTS)


def test_speed_is_clamped_and_hexed():
    assert core.effect_args("cycle", "all", "ffffff", 10)[-1] == "0a"
    assert core.effect_args("cycle", "all", "ffffff", 100)[-1] == "64"
    assert core.effect_args("cycle", "all", "ffffff", 0)[-1] == "01"
    assert core.effect_args("cycle", "all", "ffffff", 9999)[-1] == "64"


def test_rejects_shell_metacharacters_and_junk():
    for bad in ["8000ff; rm -rf /", "8000ff\n-a 000000", "$(id)", "zzzzzz", "8000f", ""]:
        try:
            core.effect_args("color", "all", bad, 10)
        except ValueError:
            continue
        raise AssertionError(f"accepted unsafe colour {bad!r}")


def test_rejects_unknown_effect_and_target():
    for effect, target in [("wave", "all"), ("solid", "all"), ("color", "everywhere"), ("color", "")]:
        try:
            core.effect_args(effect, target, "ffffff", 10)
        except ValueError:
            continue
        raise AssertionError(f"accepted effect={effect!r} target={target!r}")


def test_colourless_effects_are_known():
    assert core.COLORLESS == {"cycle", "hwave", "vwave", "cwave", "random"}


def test_uses_reports_which_arguments_an_effect_takes():
    # Drives which GUI controls are shown.
    assert core.uses("color", "color") and not core.uses("color", "speed")
    assert core.uses("breathing", "color") and core.uses("breathing", "speed")
    assert not core.uses("cycle", "color") and core.uses("cycle", "speed")
    assert not core.uses("nonsense", "speed")


def test_device_table_has_no_duplicate_pids():
    # The original repo's MOUSE_PIDS listed c087 twice, silently clobbering the G604.
    pids = list(core.KEYBOARDS)
    assert len(pids) == len(set(pids))
    for pid, (name, binary) in core.KEYBOARDS.items():
        assert len(pid) == 4 and pid == pid.lower(), pid
        assert binary.endswith("-led"), binary
        assert name, pid


def test_g910_is_mapped():
    # The device this whole project exists for.
    assert core.KEYBOARDS["c32b"][1] == "g910-led"
    assert core.KEYBOARDS["c335"][1] == "g910-led"


def test_profile_round_trip_and_cleaning():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["LOGILIGHT_STATE"] = tmp
        try:
            assert core.load_profile() == core.DEFAULTS  # nothing saved yet

            core.save_profile({"effect": "breathing", "target": "logo", "color": "#FF0000", "speed": 300})
            saved = core.load_profile()
            assert saved["color"] == "ff0000" and saved["speed"] == 100, saved

            # Hand-edited / corrupted files must not produce a bad argv.
            junk = Path(tmp) / "junk.json"
            junk.write_text('{"effect": "waves", "color": "nope", "speed": "x"}')
            assert core.clean(json.loads(junk.read_text()))["effect"] == "color"
            junk.unlink()

            core.save_profile(core.DEFAULTS, "preset")
            assert core.list_profiles() == ["preset"]
            core.delete_profile("preset")
            assert core.list_profiles() == []

            # Path traversal must not escape the state dir.
            core.save_profile(core.DEFAULTS, "../../evil")
            assert not (Path(tmp).parent.parent / "evil.json").exists()
        finally:
            del os.environ["LOGILIGHT_STATE"]


def test_normalise_color_accepts_the_shapes_users_type():
    for given in ["8000ff", "#8000FF", " 8000ff ".strip(), "8000FF"]:
        assert core.normalise_color(given) == "8000ff", given
    for bad in ["0x8000ff", "red", "8000f", "8000fff", ""]:
        try:
            core.normalise_color(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_cli_rejects_bad_values_at_parse_time():
    # choices= comes from core's tables, so argparse does this for free.
    import argparse

    parser = cli.build_parser()
    for argv in [["set", "--effect", "waves"], ["set", "--target", "everywhere"], ["set", "--speed", "900"]]:
        try:
            parser.parse_args(argv)
        except SystemExit:
            continue
        raise AssertionError(f"parser accepted {argv}")


def test_cli_set_only_overrides_the_flags_given():
    parser = cli.build_parser()
    current = {"effect": "breathing", "target": "logo", "color": "00ff00", "speed": 42, "enabled": False}

    merged = cli.merge_settings(current, parser.parse_args(["set", "--color", "#FF0000"]))
    assert merged == {"effect": "breathing", "target": "logo", "color": "ff0000", "speed": 42, "enabled": False}, merged

    merged = cli.merge_settings(current, parser.parse_args(["set", "--effect", "hwave", "--boot"]))
    assert merged["effect"] == "hwave" and merged["enabled"] is True, merged
    assert merged["color"] == "00ff00" and merged["speed"] == 42, merged

    merged = cli.merge_settings(current, parser.parse_args(["set", "--effect", "breathing", "--no-boot"]))
    assert merged["enabled"] is False, merged


def test_cli_set_payload_is_accepted_by_the_daemon_validator():
    # The merged settings must survive the same cleaning the daemon applies.
    parser = cli.build_parser()
    merged = cli.merge_settings(core.DEFAULTS, parser.parse_args(["set", "--color", "abcdef", "--speed", "7"]))
    assert core.clean(merged) == merged, merged
    argv = core.effect_args(merged["effect"], merged["target"], merged["color"], merged["speed"])
    assert argv == ["-fx", "color", "all", "abcdef"], argv


def test_detect_survives_a_denied_sysfs():
    # A confined snap without raw-usb connected can be refused the listing.
    class Denied:
        def glob(self, pattern):
            raise PermissionError(13, "Permission denied")

    original = core.USB_DEVICES
    core.USB_DEVICES = Denied()
    try:
        assert core.detect() == []
    finally:
        core.USB_DEVICES = original


def test_apply_names_the_likely_fix_when_no_device_is_visible():
    from logilight import daemon

    original = core.detect
    core.detect = lambda: []
    try:
        result = daemon.apply(core.DEFAULTS)
    finally:
        core.detect = original
    assert result["ok"] is False
    assert "raw-usb" in result["error"], result


def test_apply_reports_a_missing_g810_led_instead_of_raising():
    from logilight import daemon

    detect, resolve = core.detect, core.resolve
    core.detect = lambda: [{"pid": "c32b", "name": "G910", "binary": "g910-led"}]
    core.resolve = lambda binary: "/nonexistent/g910-led"
    try:
        result = daemon.apply(core.DEFAULTS)
    finally:
        core.detect, core.resolve = detect, resolve
    assert result["ok"] is False and result["errors"], result


def test_snap_address_is_abstract_and_matches_the_apparmor_grant():
    # snapd grants: unix (bind, listen) addr="@snap.@{SNAP_INSTANCE_NAME}.**"
    # and nothing at all for a pathname bind, which fails with EPERM. So inside
    # a snap the address must be abstract and prefixed exactly like that.
    import os as _os

    saved = {k: _os.environ.pop(k, None) for k in ("LOGILIGHT_SOCKET", "SNAP_INSTANCE_NAME", "SNAP_NAME", "SNAP_COMMON")}
    try:
        _os.environ["SNAP_INSTANCE_NAME"] = "logilight"
        _os.environ["SNAP_COMMON"] = "/var/snap/logilight/common"
        address = core.socket_address()
        assert address.startswith("\0snap.logilight."), repr(address)
        assert "/" not in address, f"abstract sockets take no path: {address!r}"

        # A parallel install must use its own instance name, not the snap name.
        _os.environ["SNAP_INSTANCE_NAME"] = "logilight_foo"
        assert core.socket_address().startswith("\0snap.logilight_foo.")

        # Falls back to SNAP_NAME when SNAP_INSTANCE_NAME is absent.
        _os.environ.pop("SNAP_INSTANCE_NAME")
        _os.environ["SNAP_NAME"] = "logilight"
        assert core.socket_address().startswith("\0snap.logilight.")

        # The tests' override wins over everything, so they can use a path.
        _os.environ["LOGILIGHT_SOCKET"] = "/tmp/x.sock"
        assert core.socket_address() == "/tmp/x.sock"
    finally:
        for key, value in saved.items():
            if value is None:
                _os.environ.pop(key, None)
            else:
                _os.environ[key] = value


def test_snap_manifest_grants_bind_to_the_daemon():
    # snapd's base seccomp profile allows `socket AF_UNIX` but not `bind`;
    # network-bind supplies accept/accept4/bind/listen. Without it the daemon
    # dies with EPERM on bind(), which is what shipped in 0.1.0-0.1.2.
    # Parsed as text so the test needs no YAML library.
    from pathlib import Path

    manifest = (Path(__file__).resolve().parent.parent / "snap" / "snapcraft.yaml").read_text()
    assert "logilight-daemon:" in manifest, "daemon app missing from the manifest"
    daemon = manifest.split("logilight-daemon:")[1].split("logilight-cli:")[0]
    assert "network-bind" in daemon, "daemon must plug network-bind or bind() returns EPERM"
    assert "raw-usb" in daemon, "daemon must plug raw-usb to reach the keyboard"

    # The GUI talks to the daemon and never binds, so it should not carry either.
    gui = manifest.split("  logilight:")[1].split("logilight-daemon:")[0]
    assert "network-bind" not in gui and "raw-usb" not in gui, gui


def test_manifest_declares_the_app_id_as_a_desktop_file_id():
    # Gtk.Application owns its application_id on the session bus, and snapd
    # denies that bind unless the snap declared the name via the desktop
    # interface's desktop-file-ids. Without it the GUI starts and immediately
    # dies with AccessDenied, which is what shipped in 0.1.0-0.1.3.
    from pathlib import Path

    manifest = (Path(__file__).resolve().parent.parent / "snap" / "snapcraft.yaml").read_text()
    assert "desktop-file-ids:" in manifest, "GUI cannot own its D-Bus name"

    # Whatever is declared must be exactly the id the GUI asks for.
    import re

    declared = set(re.findall(r"^\s+-\s+([A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)+)\s*$", manifest, re.M))
    assert core.APP_ID in declared, f"{core.APP_ID} not declared, found {sorted(declared)}"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
