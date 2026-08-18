"""Assemble ExpectedCell/DatasetObservation from a persisted collection namespace."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from weather_alpha.phase35.full_collection.audit import DatasetObservation, ExpectedCell
from weather_alpha.phase35.full_collection.provenance import assert_text_has_no_machine_roots


@dataclass(frozen=True, slots=True)
class CorpusAssembly:
    collection_id: str
    expected: tuple[ExpectedCell, ...]
    observations: tuple[DatasetObservation, ...]
    quarantine: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "collection_id": self.collection_id,
            "expected": [row.as_dict() for row in self.expected],
            "observations": [row.as_dict() for row in self.observations],
            "quarantine": list(self.quarantine),
        }
        assert_text_has_no_machine_roots(json.dumps(payload, sort_keys=True))
        return payload


class FullCollectionCorpusAssembler:
    """Normal path reads persisted namespace artifacts; no caller-fake grid required."""

    def __init__(self, *, collection_root: Path, collection_id: str) -> None:
        self.collection_id = collection_id
        self._root = collection_root / collection_id

    def assemble(self) -> CorpusAssembly:
        expected_path = self._root / "expected_cells.json"
        observations_path = self._root / "observations.json"
        if not expected_path.is_file():
            raise FileNotFoundError("expected_cells.json is required in the collection namespace")
        expected_payload = _load_json(expected_path)
        if not isinstance(expected_payload, list):
            raise ValueError("expected_cells.json must be a list")
        expected = tuple(
            ExpectedCell.from_dict(row) for row in expected_payload if isinstance(row, dict)
        )
        observations: tuple[DatasetObservation, ...]
        if observations_path.is_file():
            raw_obs = _load_json(observations_path)
            if not isinstance(raw_obs, list):
                raise ValueError("observations.json must be a list")
            observations = tuple(
                DatasetObservation.from_dict(row) for row in raw_obs if isinstance(row, dict)
            )
        else:
            observations = _observations_from_pit(self._root, expected)
        quarantine_path = self._root / "events" / "quarantined.json"
        quarantine: tuple[dict[str, Any], ...] = ()
        if quarantine_path.is_file():
            raw_q = _load_json(quarantine_path)
            if isinstance(raw_q, list):
                quarantine = tuple(row for row in raw_q if isinstance(row, dict))
        encoded = json.dumps(
            [row.as_dict() for row in expected] + [row.as_dict() for row in observations],
            sort_keys=True,
        )
        assert_text_has_no_machine_roots(encoded)
        return CorpusAssembly(
            collection_id=self.collection_id,
            expected=expected,
            observations=observations,
            quarantine=quarantine,
        )


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _observations_from_pit(
    root: Path,
    expected: tuple[ExpectedCell, ...],
) -> tuple[DatasetObservation, ...]:
    pit_path = root / "selections" / "pit.json"
    if not pit_path.is_file():
        return ()
    raw = _load_json(pit_path)
    if not isinstance(raw, list):
        return ()
    index = {
        (
            row["date"],
            row["city"],
            row["station"],
            int(row["checkpoint"]),
            row["event_family_id"],
        ): row
        for row in raw
        if isinstance(row, dict)
    }
    out: list[DatasetObservation] = []
    for cell in expected:
        row = index.get((cell.date, cell.city, cell.station, cell.checkpoint, cell.event_family_id))
        if row is None:
            out.append(
                DatasetObservation(
                    date=cell.date,
                    city=cell.city,
                    station=cell.station,
                    checkpoint=cell.checkpoint,
                    event_family_id=cell.event_family_id,
                    month=cell.month,
                    ecmwf_run_cycle=cell.ecmwf_run_cycle,
                    observed=False,
                    usable=False,
                    has_settlement=False,
                    scored=False,
                    has_price_history=False,
                    future_leakage=False,
                    retrospective_substitution=False,
                    raw_hash_ok=True,
                    topology_valid=True,
                    topology_reviewed_quarantine=False,
                    missing_reasons=("missing",),
                )
            )
            continue
        out.append(DatasetObservation.from_dict(row))
    return tuple(out)
