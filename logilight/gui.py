"""LogiLight GUI - GTK4/libadwaita front end.

This process runs as the desktop user and never touches hardware: every change
is sent to logilight-daemon, which owns the device access.  That keeps the
privileged surface to one small socket API instead of the whole toolkit.
"""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import core  # noqa: E402

APP_ID = "io.github.logilight.LogiLight"
DEBOUNCE_MS = 200


def rgba_to_hex(rgba: Gdk.RGBA) -> str:
    return "".join(f"{max(0, min(255, round(c * 255))):02x}" for c in (rgba.red, rgba.green, rgba.blue))


def hex_to_rgba(value: str) -> Gdk.RGBA:
    """Build a Gdk.RGBA from RRGGBB.

    Fields must be assigned after construction: passing them to the Gdk.RGBA
    constructor is deprecated in PyGObject and silently discards them, which
    turns every colour into black.
    """
    rgba = Gdk.RGBA()
    rgba.red = int(value[0:2], 16) / 255.0
    rgba.green = int(value[2:4], 16) / 255.0
    rgba.blue = int(value[4:6], 16) / 255.0
    rgba.alpha = 1.0
    return rgba


def _button(label: str, tooltip: str, callback) -> Gtk.Button:
    button = Gtk.Button(label=label)
    button.set_tooltip_text(tooltip)
    button.set_valign(Gtk.Align.CENTER)
    button.add_css_class("flat")
    button.connect("clicked", callback)
    return button


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="LogiLight")
        self.set_default_size(480, 660)

        self.settings = dict(core.DEFAULTS)
        self._loading = False
        self._timer = 0
        self._preset_rows: list[Adw.ActionRow] = []

        self.banner = Adw.Banner()
        self.toast = Adw.ToastOverlay()

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.add_top_bar(self.banner)
        view.set_content(self.toast)

        page = Adw.PreferencesPage()
        for build in (self._build_devices, self._build_lighting, self._build_boot, self._build_presets):
            page.add(build())
        self.toast.set_child(page)
        self.set_content(view)

    # ------------------------------------------------------------ construction

    def _build_devices(self) -> Adw.PreferencesGroup:
        self.devices_group = Adw.PreferencesGroup(
            title="Devices", description="Logitech RGB keyboards found on this machine"
        )
        self.devices_group.set_header_suffix(_button("Rescan", "Look for keyboards again", self.refresh))
        # PreferencesGroup has no public get_rows(); track what we add instead.
        self._device_rows: list[Adw.ActionRow] = []
        return self.devices_group

    def _build_lighting(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Lighting")

        self.effect_row = Adw.ComboRow(title="Effect", model=Gtk.StringList.new(list(core.EFFECTS.values())))
        self.effect_row.connect("notify::selected", self.on_effect)
        group.add(self.effect_row)

        self.target_row = Adw.ComboRow(
            title="Apply to", model=Gtk.StringList.new(list(core.TARGETS.values()))
        )
        self.target_row.connect("notify::selected", self.on_target)
        group.add(self.target_row)

        dialog = Gtk.ColorDialog()
        dialog.set_title("Keyboard colour")
        self.color_button = Gtk.ColorDialogButton()
        self.color_button.set_dialog(dialog)
        self.color_button.set_valign(Gtk.Align.CENTER)
        self.color_button.connect("notify::rgba", self.on_color)
        self.color_row = Adw.ActionRow(title="Colour")
        self.color_row.add_suffix(self.color_button)
        group.add(self.color_row)

        self.speed_scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=Gtk.Adjustment(value=core.DEFAULTS["speed"], lower=1, upper=100, step_increment=1),
        )
        self.speed_scale.set_size_request(190, -1)
        self.speed_scale.set_draw_value(True)
        self.speed_scale.set_valign(Gtk.Align.CENTER)
        self.speed_scale.connect("value-changed", self.on_speed)
        self.speed_row = Adw.ActionRow(title="Speed")
        self.speed_row.add_suffix(self.speed_scale)
        group.add(self.speed_row)

        return group

    def _build_boot(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Startup")
        self.boot_row = Adw.SwitchRow(
            title="Apply on boot",
            subtitle="Re-apply these settings whenever the keyboard is connected",
        )
        self.boot_row.connect("notify::active", self.on_boot)
        group.add(self.boot_row)
        return group

    def _build_presets(self) -> Adw.PreferencesGroup:
        self.presets_group = Adw.PreferencesGroup(title="Presets")
        self.name_row = Adw.EntryRow(title="Preset name")
        self.name_row.add_suffix(_button("Save", "Save the current settings", self.on_save_preset))
        self.presets_group.add(self.name_row)
        return self.presets_group

    # ------------------------------------------------------------ daemon calls

    def refresh(self, *_):
        try:
            reply = core.request({"cmd": "status"})
        except OSError as exc:
            self.fail(f"LogiLight service is not reachable ({exc}). Try: snap start logilight-daemon")
            return
        self.show_devices(reply.get("devices", []))
        self.load_into_ui(reply.get("settings") or core.DEFAULTS)
        self.show_presets(reply.get("profiles", []))

    def fail(self, message: str):
        self.banner.set_title(message)
        self.banner.set_revealed(True)

    def send(self, payload: dict) -> dict | None:
        try:
            reply = core.request(payload)
        except OSError as exc:
            self.fail(f"LogiLight service is not reachable ({exc}).")
            return None
        if reply.get("ok"):
            self.banner.set_revealed(False)
        else:
            self.fail(reply.get("error") or "; ".join(reply.get("errors") or ["Unknown failure"]))
        return reply

    # ------------------------------------------------------------ signals

    def on_effect(self, *_):
        if self._loading:
            return
        self.settings["effect"] = list(core.EFFECTS)[self.effect_row.get_selected()]
        self.sync_rows()
        self.changed()

    def on_target(self, *_):
        if self._loading:
            return
        self.settings["target"] = list(core.TARGETS)[self.target_row.get_selected()]
        self.changed()

    def on_color(self, *_):
        if self._loading:
            return
        self.settings["color"] = rgba_to_hex(self.color_button.get_rgba())
        self.changed()

    def on_speed(self, *_):
        if self._loading:
            return
        self.settings["speed"] = int(self.speed_scale.get_value())
        self.changed()

    def on_boot(self, *_):
        if self._loading:
            return
        self.settings["enabled"] = self.boot_row.get_active()
        self.changed()

    def on_save_preset(self, *_):
        name = self.name_row.get_text().strip()
        if not name:
            self.fail("Give the preset a name first.")
            return
        reply = self.send({"cmd": "save", "name": name, "settings": self.settings})
        if reply:
            self.name_row.set_text("")
            self.show_presets(reply.get("profiles", []))

    def on_load_preset(self, _button, name: str):
        reply = self.send({"cmd": "load", "name": name})
        if reply:
            self.load_into_ui(reply.get("settings") or core.DEFAULTS)
            self.toast.add_toast(Adw.Toast.new(f"Loaded “{name}”"))

    def on_delete_preset(self, _button, name: str):
        reply = self.send({"cmd": "delete", "name": name})
        if reply:
            self.show_presets(reply.get("profiles", []))

    # ------------------------------------------------------------ rendering

    def changed(self):
        """Coalesce rapid slider/colour drags into one round trip."""
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(DEBOUNCE_MS, self.push)

    def push(self):
        self._timer = 0
        reply = self.send({"cmd": "apply", "settings": self.settings})
        if reply and reply.get("ok"):
            self.toast.add_toast(Adw.Toast.new("Applied to " + ", ".join(reply["applied"])))
        return GLib.SOURCE_REMOVE

    def sync_rows(self):
        self.color_row.set_visible(core.uses(self.settings["effect"], "color"))
        self.speed_row.set_visible(core.uses(self.settings["effect"], "speed"))

    def load_into_ui(self, settings: dict):
        self._loading = True
        self.settings = core.clean(settings)
        self.effect_row.set_selected(list(core.EFFECTS).index(self.settings["effect"]))
        self.target_row.set_selected(list(core.TARGETS).index(self.settings["target"]))
        self.color_button.set_rgba(hex_to_rgba(self.settings["color"]))
        self.speed_scale.set_value(self.settings["speed"])
        self.boot_row.set_active(self.settings["enabled"])
        self.sync_rows()
        self._loading = False

    def show_devices(self, devices: list[dict]):
        for row in self._device_rows:
            self.devices_group.remove(row)

        if devices:
            self._device_rows = [
                Adw.ActionRow(title=device["name"], subtitle=f"USB 046d:{device['pid']}")
                for device in devices
            ]
        else:
            self._device_rows = [
                Adw.ActionRow(title="No keyboard detected", subtitle="Plug one in and rescan")
            ]

        for row in self._device_rows:
            self.devices_group.add(row)

    def show_presets(self, names: list[str]):
        for row in self._preset_rows:
            self.presets_group.remove(row)
        self._preset_rows = []
        for name in names:
            row = Adw.ActionRow(title=name)
            row.add_suffix(_button("Load", f"Apply “{name}”", lambda _b, n=name: self.on_load_preset(_b, n)))
            row.add_suffix(_button("Delete", f"Delete “{name}”", lambda _b, n=name: self.on_delete_preset(_b, n)))
            self.presets_group.add(row)
            self._preset_rows.append(row)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        window = self.props.active_window or Window(self)
        window.present()
        window.refresh()


def main(argv: list[str] | None = None) -> int:
    return App().run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    sys.exit(main())
