"""``enca`` command line: login, logout, sites, now, history, energy, readings."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import _auth, _config
from .client import Client
from .errors import EncaError
from .models import HeatPump, Site
from .readings import HEAT_PUMP_SOURCES, REGISTRY

PROVIDERS = {"google": "Google", "apple": "SignInWithApple"}


def _site(c: Client, site_id: str | None) -> Site:
    return c.site(site_id)


def _hp(site: Site, device: str | None) -> HeatPump:
    if device:
        d = site.device(device)
        if not isinstance(d, HeatPump):
            raise EncaError(f"{device} is a {d.kind.replace('_', ' ')}, not a heat pump.")
        return d
    return site.heat_pump


def cmd_login(a: argparse.Namespace) -> int:
    email = _auth.login(
        PROVIDERS.get(a.provider or ""),
        open_browser=not a.no_browser,
        manual=a.manual,
    )
    print(
        f"Signed in{' as ' + email if email else ''}. Tokens cached in {_config.token_cache_path()}"
    )
    return 0


def cmd_logout(a: argparse.Namespace) -> int:
    gone = _auth.logout()
    print("Signed out." if gone else "Wasn't signed in.")
    return 0


def cmd_sites(a: argparse.Namespace) -> int:
    with Client() as c:
        for s in c.sites():
            print(s.id if s.name == s.id else f"{s.id}  {s.name}")
            for d in s.devices:
                print(f"  {d.id:<16} {d.kind.replace('_', ' ')}")
    return 0


def cmd_now(a: argparse.Namespace) -> int:
    with Client() as c:
        site = _site(c, a.site)
        hp = _hp(site, a.device)
        snap = hp.now()
        print(
            f"{site.id} / {hp.id}"
            + (f"  (as of {snap.time:%Y-%m-%d %H:%M} UTC)" if snap.time else "")
        )
        print(snap)
        for t in site.thermostats:
            ts = t.now()
            if ts.temp is not None:
                print(
                    f"{ts.name}: {ts.temp:.1f} °F" + (f" → {ts.target:.1f} °F" if ts.target else "")
                )
    return 0


def cmd_history(a: argparse.Namespace) -> int:
    with Client() as c:
        hp = _hp(_site(c, a.site), a.device)
        frame = hp.history(
            a.names, last=None if a.start else a.last, start=a.start, end=a.end, every=a.every
        )
        if a.csv:
            sys.stdout.write(frame.to_csv())
            return 0
        for name in frame:
            s = frame[name]
            r = REGISTRY[name]
            print(
                f"{s.label}: {len(s)} points, mean {r.format(s.mean())}, "
                f"min {r.format(s.min())}, max {r.format(s.max())}"
            )
    return 0


def cmd_energy(a: argparse.Namespace) -> int:
    with Client() as c:
        site = _site(c, a.site)
        target = _hp(site, a.device) if a.device else site
        rep = target.energy(a.period, price=a.price)
        print(rep)
        if a.buckets:
            for b in rep:
                cop = "—" if b.cop is None else f"{b.cop:.2f}"
                print(
                    f"  {b.start:%Y-%m-%d %H:%M}  {b.electricity_kwh:7.2f} kWh  COP {cop}"
                    + ("" if b.exact else "  (estimated)")
                )
    return 0


def cmd_readings(a: argparse.Namespace) -> int:
    for r in REGISTRY.values():
        if r.source in HEAT_PUMP_SOURCES:
            print(f"{r.name:<22} {r.unit or '-':<5} {r.label}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="enca", description="Your Energy Catalyst heat pump, from the terminal."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help="sign in through the browser")
    s.add_argument("--provider", choices=sorted(PROVIDERS), help="skip the chooser")
    s.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")
    s.add_argument(
        "--manual", action="store_true", help="paste the redirected address (headless machines)"
    )
    s.set_defaults(fn=cmd_login)

    s = sub.add_parser("logout", help="forget the cached sign-in")
    s.set_defaults(fn=cmd_logout)

    s = sub.add_parser("sites", help="list your sites and devices")
    s.set_defaults(fn=cmd_sites)

    def target(x: argparse.ArgumentParser) -> None:
        x.add_argument("--site", help="site id (default: your only site)")
        x.add_argument("--device", help="heat pump id (default: the site's only heat pump)")

    s = sub.add_parser("now", help="the heat pump's latest readings")
    target(s)
    s.set_defaults(fn=cmd_now)

    s = sub.add_parser("history", help="history of one or more readings")
    s.add_argument("names", nargs="+", help="reading names, see `enca readings`")
    s.add_argument("--last", default="24h", help="how far back, e.g. 6h, 7d (default 24h)")
    s.add_argument("--start", help="ISO start time instead of --last")
    s.add_argument("--end", help="ISO end time (default now)")
    s.add_argument("--every", help="bucket size, e.g. 10m, 1h")
    s.add_argument("--csv", action="store_true", help="print CSV")
    target(s)
    s.set_defaults(fn=cmd_history)

    s = sub.add_parser("energy", help="electricity, heat delivered, COP and cost")
    s.add_argument("--period", choices=["day", "week", "month", "year"], default="month")
    s.add_argument("--price", type=float, help="electricity price per kWh")
    s.add_argument("--buckets", action="store_true", help="list each bucket")
    target(s)
    s.set_defaults(fn=cmd_energy)

    s = sub.add_parser("readings", help="list reading names")
    s.set_defaults(fn=cmd_readings)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rc: int = args.fn(args)
        return rc
    except EncaError as e:
        print(f"enca: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
