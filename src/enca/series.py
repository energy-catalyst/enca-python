"""History results: :class:`Series` (one reading) and :class:`Frame` (several)."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, NamedTuple, cast

if TYPE_CHECKING:
    import pandas as pd


class Point(NamedTuple):
    """One bucket: its start time (UTC) and mean value in the reading's unit."""

    time: datetime
    value: float


def _pandas() -> Any:
    try:
        import pandas
    except ImportError as e:  # pragma: no cover - depends on environment
        raise ImportError("to_dataframe() needs pandas: pip install 'enca-python[pandas]'") from e
    return pandas


@dataclass
class Series:
    """Bucket means of one reading, oldest first."""

    name: str
    label: str
    unit: str
    points: list[Point] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.points)

    def __iter__(self) -> Iterator[Point]:
        return iter(self.points)

    @property
    def times(self) -> list[datetime]:
        return [p.time for p in self.points]

    @property
    def values(self) -> list[float]:
        return [p.value for p in self.points]

    @property
    def latest(self) -> Point | None:
        return self.points[-1] if self.points else None

    def mean(self) -> float | None:
        """Mean of the bucket means (None when empty)."""
        return sum(self.values) / len(self.points) if self.points else None

    def min(self) -> float | None:
        return min(self.values) if self.points else None

    def max(self) -> float | None:
        return max(self.values) if self.points else None

    def to_dataframe(self) -> pd.DataFrame:
        """A DataFrame indexed by UTC ``time`` with one column, :attr:`name`."""
        pdm = _pandas()
        df = pdm.DataFrame(
            {self.name: self.values}, index=pdm.DatetimeIndex(self.times, name="time")
        )
        df.attrs["units"] = {self.name: self.unit}
        return cast("pd.DataFrame", df)


class Row(NamedTuple):
    time: datetime
    values: dict[str, float | None]


@dataclass
class Frame:
    """Several readings over the same range, keyed by name."""

    series: dict[str, Series]

    def __getitem__(self, name: str) -> Series:
        return self.series[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self.series)

    def __len__(self) -> int:
        return len(self.series)

    @property
    def names(self) -> list[str]:
        return list(self.series)

    @property
    def points(self) -> list[Row]:
        """Rows by time (outer join); a reading without that bucket is None."""
        by_time: dict[datetime, dict[str, float | None]] = {}
        for name, s in self.series.items():
            for p in s.points:
                by_time.setdefault(p.time, {})[name] = p.value
        return [Row(t, {n: by_time[t].get(n) for n in self.series}) for t in sorted(by_time)]

    def mean(self) -> dict[str, float | None]:
        return {n: s.mean() for n, s in self.series.items()}

    def to_dataframe(self) -> pd.DataFrame:
        """A DataFrame indexed by UTC ``time``, one column per reading."""
        pdm = _pandas()
        rows = self.points
        df = pdm.DataFrame(
            [r.values for r in rows],
            index=pdm.DatetimeIndex([r.time for r in rows], name="time"),
            columns=self.names,
        )
        df.attrs["units"] = {n: s.unit for n, s in self.series.items()}
        return cast("pd.DataFrame", df)

    def to_csv(self) -> str:
        """CSV text: ``time`` then one column per reading (unit in header)."""
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["time", *[f"{n} ({s.unit})" if s.unit else n for n, s in self.series.items()]])
        for r in self.points:
            w.writerow(
                [r.time.isoformat(), *["" if v is None else repr(v) for v in r.values.values()]]
            )
        return out.getvalue()
