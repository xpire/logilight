# LogiLight

RGB lighting control for Logitech G-series keyboards on Ubuntu. A snap with a GUI
**and** a CLI, built on top of [`g810-led`](https://github.com/MatMoul/g810-led).

Nothing needs to be installed on the host: no `sudo` at runtime, no udev rule, no
systemd unit, no `pip install`, no group membership.

## Install

Grab `logilight_*.snap` from the latest passing
[build](../../actions/workflows/build.yml) (open a run → **Artifacts** →
`logilight-snap-amd64`), then:

```bash
sudo snap install --dangerous logilight_*.snap
sudo snap connect logilight:raw-usb
```

`raw-usb` is not auto-connected because it grants raw USB access. Without it the
app can detect the keyboard but not write to it.

## Use

**GUI** — launch *LogiLight* from your app grid, or:

```bash
logilight
```

Pick an effect and a colour. Changes apply as you make them; presets and
"apply on boot" are saved automatically.

**CLI** — same functionality, for scripts and headless boxes:

```bash
logilight.logilight-cli set --color 8000ff
logilight.logilight-cli set --effect breathing --color ff0000 --speed 12
logilight.logilight-cli set --effect hwave --boot
logilight.logilight-cli status --json
```

| | |
| --- | --- |
| Effects | `color` `breathing` `cycle` `hwave` `vwave` `cwave` `random` |
| Targets | `all` `keys` `logo` |
| Speed | `1`–`100` |
| Colour | `RRGGBB`, with or without `#` |

Supported keyboards: **G213, G410, G413, G512, G513, G610, G815, G810, G910,
G Pro**. Mice are out of scope — use [Piper](https://github.com/libratbag/piper)
(`sudo apt install piper`) for those.

## How it works

```
      desktop user                              root
┌───────────────────────┐            ┌──────────────────────────┐
│ logilight    (GUI)    │   unix     │ logilight-daemon         │
│ logilight-cli (CLI)   │   socket   │ · applies settings       │
│ no device privileges  │ ─────────► │ · re-applies on hot-plug │
│                       │   JSON     │ · applies at boot        │
└───────────────────────┘            └────────────┬─────────────┘
                                                  │ exec
                                    /snap/logilight/current/usr/bin/g910-led
```

The daemon is the only process that touches hardware, and snapd runs it as root.
That is what removes the need for `sudo`, a polkit helper, or a host udev rule.

Two packaging details are load-bearing and documented in [`PRD.md`](PRD.md):

- **g810-led is built with `LIB=libusb`.** The default hidapi build writes to
  `/dev/hidraw*`, which no connectable snap interface grants on Ubuntu Desktop.
  libusb uses `/dev/bus/usb/*`, which `raw-usb` does cover.
- **The `g*-led` symlinks are relative.** g810-led picks the keyboard layout from
  `argv[0]`, and upstream's `make setup` creates absolute `/usr/bin` links that
  would resolve outside the snap.

## Building

```bash
snapcraft          # requires snapcraft and an LXD or container backend
```

## Layout

| Path | |
| --- | --- |
| `logilight/core.py` | Device discovery, g810-led argument building, profiles, socket client |
| `logilight/daemon.py` | Hardware access, boot/hot-plug apply, unix socket server |
| `logilight/gui.py` | GTK4 / libadwaita window |
| `logilight/cli.py` | Command line front end |
| `snap/snapcraft.yaml` | Snap recipe |
| `tests/` | Test suites |
| `PRD.md` | Analysis of the original project and the design decisions |

## Development

Both suites run without a keyboard, root, or GTK:

```bash
python3 tests/test_core.py         # 15 unit tests: argv building, validation, profiles
python3 tests/test_integration.py  # CLI -> socket -> daemon -> stub g810-led
```

To run outside a snap you need `python3-gi` and GTK 4.10+ for the GUI, and a
`g810-led` that your user can write to:

```bash
sudo python3 -m logilight.daemon &
python3 -m logilight.cli set --color 00ff00
python3 -m logilight.gui
```

## Acknowledgements

[`g810-led`](https://github.com/MatMoul/g810-led) by MatMoul does the actual
device work — this project is a wrapper. Device IDs and profile semantics come
from its udev rules. Prompted by ProfessorMoose74's *LogiLight*; `PRD.md`
documents the bugs found in that implementation and why several of its layers are
unnecessary.

## License

MIT — see [LICENSE](LICENSE). `g810-led` is GPL-3.0 and is built at package time,
not vendored here.
