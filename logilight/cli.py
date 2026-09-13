"""LogiLight command line - drives the daemon over its socket.

Same functionality as the GUI for scripts, headless boxes and as a fallback if
the window fails to start.  Like the GUI it runs unprivileged and never touches
hardware itself: the daemon does that.

    logilight-cli set --color 8000ff
    logilight-cli set --effect breathing --color ff0000 --speed 12
    logilight-cli status
"""

from __future__ import annotations

import argparse
import json
import sys

from . import core


def merge_settings(current: dict, args: argparse.Namespace) -> dict:
    """Overlay only the flags the user actually passed onto the live settings.

    Pure.  Anything invalid raises ValueError here rather than being silently
    replaced with a default, which `core.clean` would do.
    """
    settings = core.clean(current)
    if args.color is not None:
        settings["color"] = core.normalise_color(args.color)
    if args.effect is not None:
        settings["effect"] = args.effect
    if args.target is not None:
        settings["target"] = args.target
    if args.speed is not None:
        settings["speed"] = args.speed
    if args.boot is not None:
        settings["enabled"] = args.boot
    return core.clean(settings)


def _allow_json_after_subcommand(parser: argparse.ArgumentParser) -> None:
    """Accept `status --json` as well as `--json status`.

    SUPPRESS as the default is the trick: if the flag is absent after the
    subcommand, argparse leaves the value the top-level parser already set.
    """
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="logilight-cli", description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    setter = sub.add_parser("set", help="change the lighting")
    setter.add_argument("-c", "--color", help="hex colour, e.g. 8000ff")
    # choices come straight from the tables, so argparse rejects bad values for free
    setter.add_argument("-e", "--effect", choices=sorted(core.EFFECTS), help="lighting effect")
    setter.add_argument("-t", "--target", choices=sorted(core.TARGETS), help="what to light up")
    setter.add_argument("-s", "--speed", type=int, choices=range(1, 101), metavar="1-100")
    setter.add_argument("--boot", action=argparse.BooleanOptionalAction, help="re-apply at boot / on hot-plug")
    _allow_json_after_subcommand(setter)

    _allow_json_after_subcommand(sub.add_parser("status", help="show devices and current settings"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.command == "set" and args.color is not None:
            core.normalise_color(args.color)  # fail loudly before touching the socket

        if args.command == "set":
            current = core.request({"cmd": "status"}).get("settings") or core.DEFAULTS
            reply = core.request({"cmd": "apply", "settings": merge_settings(current, args)})
        else:
            reply = core.request({"cmd": "status"})
    except OSError as exc:
        # The usual cause is the daemon not being up yet, just after install.
        print(f"logilight: cannot reach the LogiLight service ({exc.strerror}).", file=sys.stderr)
        print("          Start it with: sudo snap start logilight.logilight-daemon", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"logilight: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(reply, indent=2))
    elif reply.get("ok"):
        if args.command == "set":
            print("Applied to " + ", ".join(reply.get("applied") or ["nothing"]))
        else:
            for device in reply.get("devices", []):
                print(f"{device['name']}  (USB 046d:{device['pid']})")
            if not reply.get("devices"):
                print("No keyboard detected")
            settings = reply.get("settings") or {}
            print(f"{settings.get('effect')} {settings.get('color')} speed={settings.get('speed')}")
    else:
        print(f"logilight: {reply.get('error') or '; '.join(reply.get('errors') or [])}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
