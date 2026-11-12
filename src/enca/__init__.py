"""enca: friendly access to your Energy Catalyst heat pump's cloud data.

import enca
enca.login()                       # once; opens the browser
site = enca.Client().site()
print(site.heat_pump.now().cop)
"""

from ._auth import login, logout
from .client import Client, __version__
from .energy import EnergyBucket, EnergyReport
from .errors import ApiError, AuthError, EncaError, NotFound, QueryError
from .models import (
    Controller,
    Device,
    HeatPump,
    HeatPumpSnapshot,
    Site,
    Tank,
    TankSnapshot,
    Thermostat,
    ThermostatSnapshot,
)
from .readings import REGISTRY as READINGS
from .readings import Reading, names, reading
from .series import Frame, Point, Series

__all__ = [
    "READINGS",
    "ApiError",
    "AuthError",
    "Client",
    "Controller",
    "Device",
    "EncaError",
    "EnergyBucket",
    "EnergyReport",
    "Frame",
    "HeatPump",
    "HeatPumpSnapshot",
    "NotFound",
    "Point",
    "QueryError",
    "Reading",
    "Series",
    "Site",
    "Tank",
    "TankSnapshot",
    "Thermostat",
    "ThermostatSnapshot",
    "__version__",
    "login",
    "logout",
    "names",
    "reading",
]
