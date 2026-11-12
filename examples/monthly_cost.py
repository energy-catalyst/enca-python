"""This year's electricity, heat delivered, COP and cost, month by month.

python examples/monthly_cost.py 0.16      # electricity price per kWh
"""

import sys

from enca import Client

price = float(sys.argv[1]) if len(sys.argv) > 1 else 0.15

with Client() as c:
    year = c.site().energy("year", price=price)
    for b in year:
        if b.electricity_kwh == 0:
            continue
        heat = "—" if b.heating_kwh is None else f"{b.heating_kwh:7.0f}"
        cop = "—" if b.cop is None else f"{b.cop:.2f}"
        note = "" if b.exact else "  (estimated)"
        print(
            f"{b.start:%b}: {b.electricity_kwh:6.0f} kWh in, {heat} kWh heat, "
            f"COP {cop}, cost {b.cost:.2f}{note}"
        )
    print("Year:", year)
