"""GUI smoke test. Needs PyGObject, GTK 4.10+, libadwaita 1.3+ and a display.

    xvfb-run -a python3 tests/test_gui.py

Constructs the real window and drives the parts that would otherwise only fail at
runtime: the colour round-trip, and the rows that hide themselves based on which
arguments the chosen effect takes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logilight import core, gui  # noqa: E402


def main():
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk  # noqa: E402

    Adw.init()
    print(f"ok  GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}")

    for value in ("00a8ff", "ffffff", "000000", "8000ff"):
        assert gui.rgba_to_hex(gui.hex_to_rgba(value)) == value, value
    # Exhaustive: Gdk.RGBA stores float32, so a sloppy round() loses a step on
    # some values (0xa8 came back as 0xaa before this was fixed).
    for v in range(256):
        grey = f"{v:02x}{v:02x}{v:02x}"
        assert gui.rgba_to_hex(gui.hex_to_rgba(grey)) == grey, grey
    print("ok  colour survives a Gdk.RGBA round-trip (all 256 values per channel)")

    # Constructed without an application: this is a widget test, and adding a
    # window to a GApplication before its startup signal is a GTK critical.
    window = gui.Window(None)

    window.load_into_ui(
        {"effect": "breathing", "target": "logo", "color": "ff0000", "speed": 33, "enabled": True}
    )
    assert window.settings["color"] == "ff0000" and window.settings["speed"] == 33
    assert window.color_row.get_visible() and window.speed_row.get_visible()

    window.load_into_ui({**core.DEFAULTS, "effect": "cycle"})
    assert not window.color_row.get_visible(), "colour row must hide for an effect that takes no colour"
    assert window.speed_row.get_visible()

    window.load_into_ui({**core.DEFAULTS, "effect": "color"})
    assert window.color_row.get_visible()
    assert not window.speed_row.get_visible(), "speed row must hide for a solid colour"
    print("ok  rows follow the effect's arguments")

    window.show_devices([{"pid": "c32b", "name": "G910 Orion Spark", "binary": "g910-led"}])
    assert len(window._device_rows) == 1
    assert window._device_rows[0].get_title() == "G910 Orion Spark"
    window.show_devices([])
    assert len(window._device_rows) == 1  # the "none detected" placeholder
    assert window._device_rows[0].get_title() == "No keyboard detected"
    print("ok  device list renders and clears")

    window.show_presets(["aurora", "coding"])
    assert len(window._preset_rows) == 2
    window.show_presets([])
    assert window._preset_rows == []
    print("ok  presets render and clear")

    window.destroy()


if __name__ == "__main__":
    main()
