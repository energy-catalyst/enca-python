# enca-python

Friendly Python access to your Energy Catalyst heat pump's cloud data.

Ask for things the way the app names them (`cop`, `ground_loop_in`,
`tank`, `low_side_pressure`) and get numbers in everyday units. You never
need to know about Flux, InfluxDB, storage tiers, entities or sign-in tokens.

```python
from enca import Client

c = Client(token="enca_pat_...")
print(c.site().heat_pump.now().cop)
```

Read-only: the library reads your data and never changes a setting.

## Install

From a checkout of this repository (Python 3.10 or newer):

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .             # just the library (needs only httpx)
pip install -e '.[pandas]'   # adds .to_dataframe()
```

## Sign in

### Personal access token (recommended)

1. In the Energy Catalyst app, turn on **Über Geek Mode** and open **API access**.
2. Create a token. Give it a name and an expiry (30, 90 or 365 days), then copy
   it. The app shows it only once.
3. Pass it in, or set it in your environment:

```python
Client(token="enca_pat_...")
```
```bash
export ENCA_TOKEN=enca_pat_...
```

Tokens are **read-only**. A token can read your sites, devices and history,
but it can't change settings or manage other tokens. To list or revoke
tokens, use the app. When a token is revoked or expires, you get an
`AuthError` that says so.

### Browser sign-in (fallback)

```bash
enca login                     # or: enca login --provider google | apple
```
```python
import enca
enca.login()                   # identity_provider="Google" / "SignInWithApple"
c = enca.Client()              # uses the cached sign-in
```

This is a **stopgap**. It uses the sign-in client's developer callback
`http://localhost:3000/`, so port 3000 must be free while you sign in. On a
headless machine, run `enca login --manual`, sign in from any browser, and
paste the address the browser ends up on. Personal access tokens are the
supported path.

- Tokens are cached in `~/.config/enca/tokens.json` on Linux
  (`$XDG_CONFIG_HOME` is honoured), `~/Library/Application Support/enca/`
  on macOS, or `%APPDATA%\enca\` on Windows. Set `ENCA_CONFIG_DIR` to put
  the cache somewhere else. The file is created with mode 0600.
- The hourly ID token is refreshed automatically. After the 30-day refresh
  token expires, you are asked to sign in again (from a terminal, the browser
  opens; otherwise you get `AuthError`).
- **Log out:** run `enca logout` or `enca.logout()`, or delete the cache file.

Credentials are tried in this order: `Client(token=...)`, then `ENCA_TOKEN`,
then the cached browser sign-in.

## 60-second quickstart

```python
from enca import Client

c = Client()                                  # ENCA_TOKEN, or the cached login
for site in c.sites():
    print(site.id, [f"{d.id} ({d.kind})" for d in site.devices])

site = c.site()                               # your only site (or c.site("s_..."))
hp = site.heat_pump                           # site.heat_pumps when there are several

now = hp.now()
print(f"COP {now.cop:.2f}, {now.power:.2f} kW, {now.mode}")
print(now)                                    # every reading, with the app's labels

loop = hp.history(["ground_loop_in", "ground_loop_out"], last="24h")
df = loop.to_dataframe()                      # needs the pandas extra
print(df.describe())

month = hp.energy("month", price=0.16)        # price per kWh
print(month)                                  # Electricity · Heating · COP · Cost
print(month.electricity_kwh, month.heating_kwh, month.cop, month.cost)
```

More examples:

- `hp.history("cop", start="2026-11-01", end="2026-11-08", every="1h")` returns a
  `Series` with `.points`, `.values`, `.mean()`, `.min()`, `.max()` and
  `.to_dataframe()`.
- A list of names returns a `Frame`. Use `frame["cop"]`, `.points` (rows by
  time), `.mean()`, `.to_dataframe()` or `.to_csv()`.
- `site.tank.now()` gives the tank's temperature, target and whether it is
  calling. `site.tank.name` is "Hot water tank" or "Heating water tank",
  depending on how the controller is plumbed.
- `for t in site.thermostats: print(t.name, t.temp, t.target, t.mode, t.calling, t.humidity)`
- `site.energy(start=..., end=..., every="1d")` adds up every heat pump on the
  site. `every` can be `1h`, `1d`, `1w` or `1mo`.

See [`examples/`](examples/).

### Command line

```bash
enca login | logout
enca sites
enca now [--site s_...] [--device hp_...]
enca history cop --last 24h --csv
enca history ground_loop_in ground_loop_out --last 7d --every 1h
enca energy --period month --price 0.16 --buckets
enca readings                                 # the names below
```

## Readings

Each name is the snake_case form of the label the app shows. Values come in
the units listed, already converted. For example, power is stored in W but
reported in kW, and BTU/h values are reported in kW. Readings marked **tank**
are published by the tank the heat pump is linked to (its `assign/tank`), and
the library reads them from there for you.

| Name | App label | Meaning | Unit | Published by |
|---|---|---|---|---|
| `cop` | COP | Coefficient of performance: heat moved per unit of electricity | – | heat pump |
| `power` | Power in | Electrical power the heat pump draws (stored in W) | kW | heat pump |
| `heat_delivered` | Heat delivered | All heat delivered to the house (stored in BTU/h) | kW | heat pump |
| `heat_from_ground` | Heat from ground | Heat drawn from the ground loop (stored in BTU/h) | kW | heat pump |
| `ground_loop_in` | Ground loop, from ground | Ground loop fluid arriving from the ground | °F | heat pump |
| `ground_loop_out` | Ground loop, back to ground | Ground loop fluid returning to the ground | °F | heat pump |
| `ground_loop_flow` | Ground loop flow | Ground loop flow rate | GPM | heat pump |
| `ground_loop_pump` | Ground loop pump | Ground loop pump speed | % | heat pump |
| `air_intake` | Air intake | Return air entering the air handler | °F | heat pump |
| `air_supply` | Air supply | Supply air leaving the air handler | °F | heat pump |
| `fan` | Fan speed | Blower speed | % | heat pump |
| `tank` | Water tank | Tank temperature (published by the tank, not the heat pump) | °F | tank |
| `tank_target` | Tank target | Tank target temperature | °F | tank |
| `outdoor_air` | Outdoor air | Outdoor air, where the tank controller has a sensor wired | °F | tank |
| `water_from_tank` | Water back from tank | Water coming back from the tank into the heat pump | °F | heat pump |
| `water_to_tank` | Water to tank | Water leaving the heat pump for the tank | °F | heat pump |
| `tank_loop_flow` | Tank loop flow | Flow between heat pump and tank | GPM | heat pump |
| `tank_loop_pump` | Tank loop pump | The heat pump's tank loop pump speed | % | heat pump |
| `heat_into_water` | Heat into water | Heat going into the tank water (stored in BTU/h) | kW | heat pump |
| `low_side_pressure` | Low-side pressure | Suction pressure (gauge) | psi | heat pump |
| `high_side_pressure` | High-side pressure | Discharge pressure (gauge) | psi | heat pump |
| `suction_saturation` | Suction saturation | Saturation temperature at suction pressure | °F | heat pump |
| `condensing_saturation` | Condensing saturation | Saturation temperature at discharge pressure | °F | heat pump |
| `discharge_line` | Discharge line | Compressor discharge line temperature | °F | heat pump |
| `compressor_inlet` | Compressor inlet | Refrigerant entering the compressor | °F | heat pump |
| `superheat` | Superheat | Superheat at the compressor (only while it runs) | °F | heat pump |
| `subcooled_liquid` | Subcooled liquid | Liquid refrigerant leaving the condenser | °F | heat pump |
| `subcooling` | Subcooling | Condensing saturation minus subcooled liquid (derived) | °F | heat pump |
| `compressor_speed` | Compressor speed | Compressor speed reported by the inverter | RPM | heat pump |
| `compressor_load` | Compressor load | Compressor load factor | % | heat pump |
| `temp` | Room | Room temperature at a thermostat | °F | thermostat |
| `target` | Target | Thermostat target temperature | °F | thermostat |
| `humidity` | Humidity | Room humidity at a thermostat | % | thermostat |

A typo gets a suggestion and the list of valid names:
`NotFound: Unknown reading 'ground_loop_inn'. Did you mean 'ground_loop_in'? Valid names: ...`.

`hp.now()` also returns `mode` in plain words ("Forced air", "Hot water",
"Hybrid", "Cooling", ...), `mode_raw`, `current_action`, `compressor_running`
and `time` (the newest reading). It also has the aliases `power_kw`,
`compressor_rpm` and `ground_flow`. Superheat is `None` while the compressor
is stopped. Subcooling is calculated as condensing saturation minus the
subcooled liquid temperature, because the heat pump doesn't publish it.

## Energy

`hp.energy(period)` takes `"day"` (by hour), `"week"` or `"month"` (by day),
or `"year"` (by month), in your local time zone. Pass `at=date(...)` for
another period, or use `start=`/`end=`/`every=`. Each bucket has
`electricity_kwh`, `heating_kwh`, `cooling_kwh`, `ground_kwh`, `cop`, `cost`
and `exact`.

- **Exact:** the result comes from the heat pump's energy counters, whenever
  a counter reading exists before the bucket starts.
- **Estimated (`exact=False`):** used for older data or units without
  counters. Electricity is the sum of the 1-minute power means. Heating is
  estimated hour by hour as electricity × that hour's COP, counting only hours
  when the unit was heating.
  *TODO:* estimate cooling delivered (for now these buckets report
  `cooling_kwh=None`), and split the `cooling_dhw` mode between cooling and
  tank heating (for now it counts as cooling).

## Advanced: your own queries

```python
rows = c.query('''
telemetry_1h(start: 2026-10-01T00:00:00Z, stop: 2026-11-01T00:00:00Z)
  |> filter(fn: (r) => r.entity == "data/cop" and r._field == "mean")
''')   # list of dicts, column -> text
```

The API limits every query to your own sites. Start a query from
`telemetry(start:, stop:)` (raw data; values are strings, `_field == "value"`)
or from `telemetry_1m`, `telemetry_10m` or `telemetry_1h` (`_field` is
`mean`, `min`, `max`, `first` or `last`). The API refuses any query that
contains the words *from*, *import*, *to*, *buckets*, *cardinality* or
*wideTo* anywhere, comments included. The library checks for these words
before sending and raises `QueryError` if it finds one.

## Limits and etiquette

Many people share the API, so the library is careful by default:

- **Concurrency:** at most 2 requests at a time with a personal access token,
  which matches the server's per-token limit. With the browser sign-in the
  default is 4. You can change it with `Client(max_concurrency=...)`.
- **Rate:** with a personal access token, the library stays under 60 requests
  per minute.
- **Retries:** 429, 5xx responses and network timeouts are retried up to 4
  times with exponential backoff and jitter. A `Retry-After` header is honoured.
- **Timeouts:** 10 s to connect and 60 s to read. The server stops a query
  after 50 s.
- **Size:** the server allows 1 MB per query and 20 MB per result. History
  refuses more than 20,000 points per reading, so pick a larger `every=`.
- `now()` uses one query for the whole site and reuses the result for 15
  seconds. Device settings are read once per `Site`; call `site.refresh()` to
  read them again.
- Storage tiers are picked for you, the same way the app picks them:
  - raw data for spans up to 3 h that end now;
  - 1-minute data up to 50 h;
  - 10-minute data up to 35 days;
  - hourly data beyond that.

  If you ask for a finer `every=`, the library steps down to a finer tier.
- The library has no telemetry. It never logs or prints tokens. It logs
  through the standard `logging` module under the name `"enca"`, and only
  records method, path and status.

Errors all derive from `enca.EncaError`: `AuthError`, `NotFound`,
`QueryError` and `ApiError` (which has `.status`).

## Assumptions

- The default API URL is `https://api.energycatalysttech.net`. This is an
  assumption; set `ENCA_API_URL` or `Client(api_url=...)` to change it.
- Device kinds come from the `subtypes` field of `/sites/{site}/devices`.
  If that is missing, the library uses the device's `info/subtype`, and
  failing that its id prefix (`hp_`, `tk_`, `tst_`, `controller`).
- "Now" means the last value from the past 24 h. Change this with
  `hp.now(max_age="1h")`. Device settings come from the past 7 days. If a
  heat pump's `assign/tank` setting wasn't seen and the site has exactly one
  tank, that tank is used.

## Development

```bash
pip install -e '.[dev]'
pytest && ruff check . && ruff format --check . && mypy
```

The tests use `httpx.MockTransport` and never contact a real service.
