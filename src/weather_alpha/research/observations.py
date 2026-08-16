"""Decision-time observation providers.

Open-Meteo Archive is retrieved retrospectively and does not expose immutable
point-in-time publication or revision timestamps. It must not populate
observation features at decision T. Point-in-time METAR is not fabricated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from weather_alpha.config.stations import Station

ARCHIVE_NOT_DECISION_TIME_LIMITATION = (
    "Open-Meteo Archive is retrospective and is not a decision-time observation "
    "feature: it does not expose immutable point-in-time publication/revision "
    "timestamps. observation_max_so_far_c and observation_as_of remain null. "
    "Archive daily maximum may be stored only as diagnostic_actual_max_c / a "
    "training-target diagnostic when fitting forecast error on train dates."
)


@dataclass(frozen=True, slots=True)
class ObservationAsOfResult:
    max_so_far_c: float | None
    as_of: datetime | None
    limitations: tuple[str, ...]


class ObservationAsOfProvider(Protocol):
    """Optional as-of observation source. Default implementation is disabled."""

    def observation_max_so_far(
        self,
        *,
        station: Station,
        event_date: str,
        decision_ts: datetime,
    ) -> ObservationAsOfResult: ...


class DisabledObservationProvider:
    """Default: no point-in-time observations. Does not read Archive hourly rows."""

    def observation_max_so_far(
        self,
        *,
        station: Station,
        event_date: str,
        decision_ts: datetime,
    ) -> ObservationAsOfResult:
        del station, event_date, decision_ts
        return ObservationAsOfResult(
            max_so_far_c=None,
            as_of=None,
            limitations=(ARCHIVE_NOT_DECISION_TIME_LIMITATION,),
        )
