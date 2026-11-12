"""Print the heat pump's live readings, its tank and every thermostat.

ENCA_TOKEN=enca_pat_... python examples/live_status.py
"""

from enca import Client

with Client() as c:
    for site in c.sites():
        print(f"== {site.name}")
        for hp in site.heat_pumps:
            now = hp.now()
            print(f"{hp.id}: {now.mode or 'unknown mode'}, COP {now.cop}, {now.power} kW")
            print(now)
        tank = site.tank
        if tank is not None:
            t = tank.now()
            print(f"{t.name}: {t.temp} °F (target {t.target} °F)")
        for th in site.thermostats:
            s = th.now()
            print(f"{s.name}: {s.temp} °F → {s.target} °F, {s.mode}, calling={s.calling}")
