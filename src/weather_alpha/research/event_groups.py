"""Event-group coherence validation for Phase 3 research snapshots.

Hierarchy for candidate keys (never city/station/date alone):
  parent event_id > negRiskMarketID > normalized event_slug > market-slug family

Hard invariants for an authoritative parent group: event id (when present), city,
station ICAO, event date, native temperature unit.

Corroboration (absent is not a conflict; explicit contradiction may quarantine):
  resolution URL / resolution ICAO, parent event_slug, negRiskMarketID.

Child literal slug/question and imperfect canonical families are WEAK: they never
veto a coherent parent group unless they positively prove another city, date,
station, or a non-temperature event.

Whole-group conflicts quarantine every affected member. Record-only duplicates may
be isolated when the remaining members independently pass validation.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

from weather_alpha.collectors.polymarket.parser import is_temperature_market_text
from weather_alpha.research.types import (
    EVENT_IDENTITY_AMBIGUOUS,
    QuarantineRecord,
    ResearchSnapshot,
    build_canonical_event_identity,
    event_group_key,
    normalize_slug_family,
    question_family,
)

EVENT_ID_CONFLICT = "event_id_conflict"
EVENT_CITY_CONFLICT = "event_city_conflict"
EVENT_STATION_CONFLICT = "event_station_conflict"
EVENT_DATE_CONFLICT = "event_date_conflict"
EVENT_UNIT_CONFLICT = "event_unit_conflict"
EVENT_RESOLUTION_SOURCE_CONFLICT = "event_resolution_source_conflict"
EVENT_SLUG_FAMILY_CONFLICT = "event_slug_family_conflict"
EVENT_QUESTION_FAMILY_CONFLICT = "event_question_family_conflict"
EVENT_MARKET_FAMILY_CONFLICT = "event_market_family_conflict"
EVENT_BUCKET_STRUCTURE_CONFLICT = "event_bucket_structure_conflict"
DUPLICATE_SNAPSHOT = "duplicate_snapshot"
DUPLICATE_OUTCOME_CONFLICT = "duplicate_outcome_conflict"
EVENT_SETTLEMENT_CARDINALITY_CONFLICT = "event_settlement_cardinality_conflict"
EVENT_SETTLEMENT_UNRESOLVED = "event_settlement_unresolved"

_WUNDERGROUND_ICAO_RE = re.compile(
    r"/history/daily/(?:[a-z]{2}/[^/]+/)?([A-Za-z]{4})\b",
    re.IGNORECASE,
)
_ICAO_RE = re.compile(r"^[A-Z]{4}$")
_CITY_IN_TEXT_RE = re.compile(
    r"\b(new\s+york|paris|london|munich|amsterdam|milan)\b",
    re.IGNORECASE,
)
_DATE_IN_QUESTION_RE = re.compile(
    r"\bon\s+([A-Za-z]+)\s+(\d{1,2})(?:[a-z]{2})?(?:,?\s*(\d{4}))?",
    re.IGNORECASE,
)
_DATE_IN_SLUG_RE = re.compile(
    r"-on-([a-z]+)-(\d{1,2})(?:-(\d{4}))?",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class EventGroupValidation:
    accepted: bool
    quarantine_code: str | None = None
    detail: str | None = None


def normalize_station_icao(value: str | None) -> str | None:
    """trim -> uppercase -> require exactly [A-Z]{4}; otherwise None."""
    if value is None:
        return None
    token = value.strip().upper()
    if not token or _ICAO_RE.fullmatch(token) is None:
        return None
    return token


def validate_candidate_event_group(
    members: Sequence[ResearchSnapshot],
) -> EventGroupValidation:
    """Validate one candidate group before acceptance."""
    if not members:
        return EventGroupValidation(accepted=False, quarantine_code=EVENT_IDENTITY_AMBIGUOUS)
    if any(build_canonical_event_identity(row).ambiguous for row in members):
        return EventGroupValidation(accepted=False, quarantine_code=EVENT_IDENTITY_AMBIGUOUS)

    must = (
        ("event_id", EVENT_ID_CONFLICT, lambda s: _norm(s.event_id)),
        ("city", EVENT_CITY_CONFLICT, lambda s: _norm(s.city)),
        ("station_icao", EVENT_STATION_CONFLICT, lambda s: normalize_station_icao(s.station_icao)),
        ("event_date", EVENT_DATE_CONFLICT, lambda s: s.event_date),
        ("temperature_unit", EVENT_UNIT_CONFLICT, lambda s: _norm(s.temperature_unit)),
    )
    for _name, code, getter in must:
        values = {getter(row) for row in members}
        values.discard(None)
        values.discard("")
        if len(values) > 1:
            return EventGroupValidation(accepted=False, quarantine_code=code, detail=_name)

    # Corroboration: compatible when present; absent is not a conflict.
    res_stations = {normalize_station_icao(row.source_station_icao) for row in members}
    res_stations.discard(None)
    res_urls = {_resolution_url(row) for row in members}
    res_urls.discard(None)
    res_icaos = {_resolution_icao_from_urls(row) for row in members}
    res_icaos.discard(None)
    if len(res_stations) > 1 or len(res_urls) > 1 or len(res_icaos) > 1:
        return EventGroupValidation(
            accepted=False,
            quarantine_code=EVENT_RESOLUTION_SOURCE_CONFLICT,
        )
    if res_stations and res_icaos and res_stations != res_icaos:
        return EventGroupValidation(
            accepted=False,
            quarantine_code=EVENT_RESOLUTION_SOURCE_CONFLICT,
        )

    event_slugs = {_norm(row.event_slug) for row in members}
    event_slugs.discard(None)
    if len(event_slugs) > 1:
        return EventGroupValidation(accepted=False, quarantine_code=EVENT_SLUG_FAMILY_CONFLICT)

    neg_ids = {_norm(row.neg_risk_market_id) for row in members}
    neg_ids.discard(None)
    if len(neg_ids) > 1:
        return EventGroupValidation(accepted=False, quarantine_code=EVENT_MARKET_FAMILY_CONFLICT)

    # WEAK child evidence: never veto coherent parent group without positive proof.
    slug_families = {normalize_slug_family(row.slug) for row in members}
    slug_families.discard(None)
    if len(slug_families) > 1 and _weak_child_proves_conflict(members, kind="slug"):
        return EventGroupValidation(accepted=False, quarantine_code=EVENT_SLUG_FAMILY_CONFLICT)

    question_families = {question_family(row.question) for row in members}
    question_families.discard(None)
    if len(question_families) > 1 and _weak_child_proves_conflict(members, kind="question"):
        return EventGroupValidation(accepted=False, quarantine_code=EVENT_QUESTION_FAMILY_CONFLICT)

    labels = [(row.bucket_label or "").strip().lower() for row in members]
    if any(not label for label in labels):
        return EventGroupValidation(accepted=False, quarantine_code=EVENT_BUCKET_STRUCTURE_CONFLICT)
    if len(labels) != len(set(labels)):
        return EventGroupValidation(accepted=False, quarantine_code=DUPLICATE_OUTCOME_CONFLICT)

    kinds = {(row.bucket_kind or "").strip().lower() for row in members}
    if "unknown" in kinds or "" in kinds:
        return EventGroupValidation(accepted=False, quarantine_code=EVENT_BUCKET_STRUCTURE_CONFLICT)
    tempish = {"exact", "range", "above", "below"}
    if kinds - tempish and kinds & tempish:
        return EventGroupValidation(accepted=False, quarantine_code=EVENT_BUCKET_STRUCTURE_CONFLICT)

    topology = _validate_native_bucket_topology(members)
    if topology is not None:
        return topology

    yes_count = sum(1 for row in members if (row.settlement_label or "").lower() == "yes")
    labeled = [row for row in members if row.settlement_label is not None]
    if yes_count > 1:
        return EventGroupValidation(
            accepted=False, quarantine_code=EVENT_SETTLEMENT_CARDINALITY_CONFLICT
        )
    if labeled and len(labeled) == len(members) and yes_count == 0:
        return EventGroupValidation(accepted=False, quarantine_code=EVENT_SETTLEMENT_UNRESOLVED)

    return EventGroupValidation(accepted=True)


def accept_event_groups(
    snapshots: Sequence[ResearchSnapshot],
) -> tuple[tuple[ResearchSnapshot, ...], tuple[QuarantineRecord, ...]]:
    """Group by canonical key, isolate record-only dupes, quarantine incoherent groups."""
    accepted: list[ResearchSnapshot] = []
    quarantined: list[QuarantineRecord] = []

    ambiguous: list[ResearchSnapshot] = []
    keyed: dict[tuple[str, ...], list[ResearchSnapshot]] = defaultdict(list)
    for snap in snapshots:
        identity = build_canonical_event_identity(snap)
        if identity.ambiguous:
            ambiguous.append(snap)
            continue
        keyed[event_group_key(snap)].append(snap)

    for snap in ambiguous:
        quarantined.append(_quarantine(snap, EVENT_IDENTITY_AMBIGUOUS))

    for _key, members in keyed.items():
        unique_members, dup_records = _isolate_duplicates(members)
        quarantined.extend(dup_records)
        if not unique_members:
            continue
        result = validate_candidate_event_group(unique_members)
        if result.accepted:
            accepted.extend(unique_members)
            continue
        code = result.quarantine_code or EVENT_IDENTITY_AMBIGUOUS
        for snap in unique_members:
            quarantined.append(_quarantine(snap, code, detail=result.detail))

    accepted_sorted = tuple(
        sorted(accepted, key=lambda item: (item.event_date, item.token_id, item.condition_id))
    )
    return accepted_sorted, tuple(quarantined)


@dataclass(frozen=True, slots=True)
class _NativeSegment:
    kind: str
    lo: float | None
    hi: float | None
    token_id: str


def _validate_native_bucket_topology(
    members: Sequence[ResearchSnapshot],
) -> EventGroupValidation | None:
    """Validate numeric native-unit bucket topology; never infer from display labels."""
    tempish = {"exact", "range", "above", "below"}
    rows = [row for row in members if (row.bucket_kind or "").strip().lower() in tempish]
    if not rows:
        return None

    units = {_norm(row.temperature_unit) for row in rows}
    units.discard(None)
    if len(units) > 1:
        return EventGroupValidation(
            accepted=False,
            quarantine_code=EVENT_BUCKET_STRUCTURE_CONFLICT,
            detail="native_unit_mismatch",
        )
    unit_token = next((item for item in units if item is not None), "c")
    unit = "F" if unit_token.upper().startswith("F") else "C"

    segments: list[_NativeSegment] = []
    for row in rows:
        kind = (row.bucket_kind or "").strip().lower()
        lo = row.temperature_native_min
        hi = row.temperature_native_max
        # Numeric fields are authoritative when present; missing/invalid bounds conflict.
        if not _native_bounds_match_kind(kind, lo, hi):
            return EventGroupValidation(
                accepted=False,
                quarantine_code=EVENT_BUCKET_STRUCTURE_CONFLICT,
                detail=(
                    f"invalid_native_bounds:token={row.token_id};kind={kind};"
                    f"native_min={lo};native_max={hi}"
                ),
            )
        segments.append(_NativeSegment(kind=kind, lo=lo, hi=hi, token_id=row.token_id))

    # Duplicate semantic coverage (identical kind+bounds), independent of display labels.
    signatures = [(seg.kind, seg.lo, seg.hi) for seg in segments]
    if len(signatures) != len(set(signatures)):
        return EventGroupValidation(
            accepted=False,
            quarantine_code=EVENT_BUCKET_STRUCTURE_CONFLICT,
            detail="duplicate_semantic_coverage",
        )

    if unit == "F":
        return _topology_fahrenheit_integers(segments)
    return _topology_celsius_settlement(segments)


def _native_bounds_match_kind(kind: str, lo: float | None, hi: float | None) -> bool:
    if kind == "exact":
        return lo is not None and hi is not None and lo == hi
    if kind == "range":
        return lo is not None and hi is not None and lo <= hi
    if kind == "below":
        return hi is not None and lo is None
    if kind == "above":
        return lo is not None and hi is None
    return False


def _topology_fahrenheit_integers(
    segments: Sequence[_NativeSegment],
) -> EventGroupValidation | None:
    """Integer settlement partition: adjacent means next_lo == prev_hi + 1."""
    below = [seg for seg in segments if seg.kind == "below"]
    above = [seg for seg in segments if seg.kind == "above"]
    interiors = [seg for seg in segments if seg.kind in {"exact", "range"}]
    if len(below) > 1 or len(above) > 1:
        return EventGroupValidation(
            accepted=False,
            quarantine_code=EVENT_BUCKET_STRUCTURE_CONFLICT,
            detail="invalid_tail:multiple_open_ends",
        )

    ordered = sorted(
        interiors,
        key=lambda seg: (
            float("-inf") if seg.lo is None else float(seg.lo),
            float("inf") if seg.hi is None else float(seg.hi),
            seg.token_id,
        ),
    )
    # Convert to inclusive integer spans.
    spans: list[tuple[int, int, str]] = []
    for seg in ordered:
        assert seg.lo is not None and seg.hi is not None
        lo_i = round(seg.lo)
        hi_i = round(seg.hi)
        if seg.kind == "exact":
            hi_i = lo_i
        spans.append((lo_i, hi_i, seg.token_id))

    for index in range(1, len(spans)):
        prev_lo, prev_hi, prev_tok = spans[index - 1]
        cur_lo, cur_hi, cur_tok = spans[index]
        if cur_lo <= prev_hi:
            return EventGroupValidation(
                accepted=False,
                quarantine_code=EVENT_BUCKET_STRUCTURE_CONFLICT,
                detail=(
                    f"overlap:tokens={prev_tok},{cur_tok};"
                    f"spans={prev_lo}-{prev_hi},{cur_lo}-{cur_hi}"
                ),
            )
        if cur_lo != prev_hi + 1:
            return EventGroupValidation(
                accepted=False,
                quarantine_code=EVENT_BUCKET_STRUCTURE_CONFLICT,
                detail=(
                    f"gap:tokens={prev_tok},{cur_tok};spans={prev_lo}-{prev_hi},{cur_lo}-{cur_hi}"
                ),
            )

    if below:
        assert below[0].hi is not None
        below_hi = round(below[0].hi)
        if spans:
            first_lo = spans[0][0]
            if first_lo != below_hi + 1:
                return EventGroupValidation(
                    accepted=False,
                    quarantine_code=EVENT_BUCKET_STRUCTURE_CONFLICT,
                    detail=(f"invalid_tail:low;below_hi={below_hi};first_interior_lo={first_lo}"),
                )
        if above and not spans:
            assert above[0].lo is not None
            above_lo = round(above[0].lo)
            if above_lo != below_hi + 1:
                return EventGroupValidation(
                    accepted=False,
                    quarantine_code=EVENT_BUCKET_STRUCTURE_CONFLICT,
                    detail=(
                        f"invalid_tail:low_high_connect;below_hi={below_hi};above_lo={above_lo}"
                    ),
                )

    if above:
        assert above[0].lo is not None
        above_lo = round(above[0].lo)
        if spans:
            last_hi = spans[-1][1]
            if above_lo != last_hi + 1:
                return EventGroupValidation(
                    accepted=False,
                    quarantine_code=EVENT_BUCKET_STRUCTURE_CONFLICT,
                    detail=(f"invalid_tail:high;above_lo={above_lo};last_interior_hi={last_hi}"),
                )

    return None


def _topology_celsius_settlement(
    segments: Sequence[_NativeSegment],
) -> EventGroupValidation | None:
    """Celsius settlement: contiguous native spans with next_lo == prev_hi + 1."""
    below = [seg for seg in segments if seg.kind == "below"]
    above = [seg for seg in segments if seg.kind == "above"]
    interiors = [seg for seg in segments if seg.kind in {"exact", "range"}]
    if len(below) > 1 or len(above) > 1:
        return EventGroupValidation(
            accepted=False,
            quarantine_code=EVENT_BUCKET_STRUCTURE_CONFLICT,
            detail="invalid_tail:multiple_open_ends",
        )

    ordered = sorted(
        interiors,
        key=lambda seg: (
            float("-inf") if seg.lo is None else float(seg.lo),
            float("inf") if seg.hi is None else float(seg.hi),
            seg.token_id,
        ),
    )

    for index in range(1, len(ordered)):
        prev = ordered[index - 1]
        cur = ordered[index]
        assert prev.lo is not None and prev.hi is not None
        assert cur.lo is not None and cur.hi is not None
        if cur.lo <= prev.hi:
            return EventGroupValidation(
                accepted=False,
                quarantine_code=EVENT_BUCKET_STRUCTURE_CONFLICT,
                detail=(
                    f"overlap:tokens={prev.token_id},{cur.token_id};"
                    f"native={prev.lo}-{prev.hi},{cur.lo}-{cur.hi}"
                ),
            )
        if cur.lo != prev.hi + 1.0:
            return EventGroupValidation(
                accepted=False,
                quarantine_code=EVENT_BUCKET_STRUCTURE_CONFLICT,
                detail=(
                    f"gap:tokens={prev.token_id},{cur.token_id};"
                    f"native={prev.lo}-{prev.hi},{cur.lo}-{cur.hi}"
                ),
            )

    if below:
        assert below[0].hi is not None
        below_hi = float(below[0].hi)
        if ordered:
            first = ordered[0]
            assert first.lo is not None
            if first.lo != below_hi + 1.0:
                return EventGroupValidation(
                    accepted=False,
                    quarantine_code=EVENT_BUCKET_STRUCTURE_CONFLICT,
                    detail=(f"invalid_tail:low;below_hi={below_hi};first_interior_lo={first.lo}"),
                )
        elif above:
            assert above[0].lo is not None
            if float(above[0].lo) != below_hi + 1.0:
                return EventGroupValidation(
                    accepted=False,
                    quarantine_code=EVENT_BUCKET_STRUCTURE_CONFLICT,
                    detail=(
                        f"invalid_tail:low_high_connect;below_hi={below_hi};above_lo={above[0].lo}"
                    ),
                )

    if above and ordered:
        assert above[0].lo is not None
        last = ordered[-1]
        assert last.hi is not None
        if float(above[0].lo) != float(last.hi) + 1.0:
            return EventGroupValidation(
                accepted=False,
                quarantine_code=EVENT_BUCKET_STRUCTURE_CONFLICT,
                detail=(f"invalid_tail:high;above_lo={above[0].lo};last_interior_hi={last.hi}"),
            )

    return None


def _weak_child_proves_conflict(
    members: Sequence[ResearchSnapshot],
    *,
    kind: str,
) -> bool:
    """Return True only with positive proof of other city/date/station/non-temperature."""
    cities: set[str] = set()
    dates: set[str] = set()
    stations: set[str] = set()
    non_temp = False
    for row in members:
        if (row.question is not None or row.slug is not None) and not is_temperature_market_text(
            row.question or "", row.slug
        ):
            non_temp = True
        blobs = ((row.question or ""), (row.slug or ""))
        for blob in blobs:
            for match in _CITY_IN_TEXT_RE.finditer(blob):
                city = match.group(1)
                if city is not None:
                    cities.add(re.sub(r"\s+", " ", city.strip().lower()))
            for match in _WUNDERGROUND_ICAO_RE.finditer(blob):
                icao = match.group(1)
                if icao is not None:
                    normalized = normalize_station_icao(icao)
                    if normalized:
                        stations.add(normalized)
        if kind == "question" and row.question:
            question_date_match = _DATE_IN_QUESTION_RE.search(row.question)
            if question_date_match is not None:
                month, day, year = (
                    question_date_match.group(1),
                    question_date_match.group(2),
                    question_date_match.group(3),
                )
                if month is not None and day is not None:
                    dates.add(_date_key(month, day, year))
        if row.slug:
            slug_date_match = _DATE_IN_SLUG_RE.search(row.slug)
            if slug_date_match is not None:
                month, day, year = (
                    slug_date_match.group(1),
                    slug_date_match.group(2),
                    slug_date_match.group(3),
                )
                if month is not None and day is not None:
                    dates.add(_date_key(month, day, year))

    member_cities = {_norm(row.city) for row in members}
    member_cities.discard(None)
    if len(cities) > 1:
        return True
    if cities and member_cities and cities != member_cities:
        return True

    member_dates = {row.event_date for row in members if row.event_date}
    if dates and member_dates:
        child_md = {_month_day(token) for token in dates}
        member_md = {_month_day_from_iso(token) for token in member_dates}
        child_md.discard(None)
        member_md.discard(None)
        if child_md and member_md and child_md != member_md:
            return True
        if len(child_md) > 1:
            return True

    member_stations = {normalize_station_icao(row.station_icao) for row in members}
    member_stations.discard(None)
    if len(stations) > 1:
        return True
    if stations and member_stations and stations != member_stations:
        return True

    return non_temp


_MONTH_ALIASES = {
    "jan": "january",
    "january": "january",
    "feb": "february",
    "february": "february",
    "mar": "march",
    "march": "march",
    "apr": "april",
    "april": "april",
    "may": "may",
    "jun": "june",
    "june": "june",
    "jul": "july",
    "july": "july",
    "aug": "august",
    "august": "august",
    "sep": "september",
    "sept": "september",
    "september": "september",
    "oct": "october",
    "october": "october",
    "nov": "november",
    "november": "november",
    "dec": "december",
    "december": "december",
}


def _date_key(month: str, day: str, year: str | None) -> str:
    month_token = _MONTH_ALIASES.get(month.strip().lower(), month.strip().lower())
    day_token = str(int(day))
    return f"{month_token}-{day_token}-{year}" if year else f"{month_token}-{day_token}"


def _month_day(token: str) -> str | None:
    parts = token.split("-")
    if len(parts) < 2:
        return None
    return f"{parts[0]}-{parts[1]}"


def _month_day_from_iso(value: str) -> str | None:
    try:
        _year, month, day = value.split("-")
        month_i: int = int(month)
        day_i: int = int(day)
    except ValueError:
        return None
    months = (
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    )
    if not 1 <= month_i <= 12:
        return None
    return f"{months[month_i - 1]}-{day_i}"


def _isolate_duplicates(
    members: Sequence[ResearchSnapshot],
) -> tuple[list[ResearchSnapshot], list[QuarantineRecord]]:
    from weather_alpha.research.types import snapshot_dedup_key

    seen: dict[str, ResearchSnapshot] = {}
    quarantined: list[QuarantineRecord] = []
    unique: list[ResearchSnapshot] = []
    for snap in members:
        key = snapshot_dedup_key(snap)
        if key in seen:
            quarantined.append(_quarantine(snap, DUPLICATE_SNAPSHOT, detail=key))
            continue
        seen[key] = snap
        unique.append(snap)
    return unique, quarantined


def _quarantine(
    snap: ResearchSnapshot,
    reason: str,
    *,
    detail: str | None = None,
) -> QuarantineRecord:
    provenance = (
        f"urls={list(snap.provenance_urls)}; "
        f"raw_paths={list(snap.raw_paths)}; "
        f"hashes={list(snap.content_hashes)}"
    )
    extra = detail or ""
    if provenance:
        extra = (extra + "; " if extra else "") + provenance
    return QuarantineRecord(
        reason=reason,
        condition_id=snap.condition_id,
        market_id=snap.market_id,
        token_id=snap.token_id,
        city=snap.city,
        station_icao=snap.station_icao,
        event_date=snap.event_date,
        details=extra or None,
    )


def _norm(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().lower()
    return text or None


def _resolution_url(snap: ResearchSnapshot) -> str | None:
    for url in snap.provenance_urls:
        if "wunderground.com" in url.lower():
            return url.rstrip(".").lower()
    return None


def _resolution_icao_from_urls(snap: ResearchSnapshot) -> str | None:
    for url in snap.provenance_urls:
        match = _WUNDERGROUND_ICAO_RE.search(urlparse(url).path)
        if match is not None:
            icao = match.group(1)
            if icao is not None:
                return normalize_station_icao(icao)
    return None
