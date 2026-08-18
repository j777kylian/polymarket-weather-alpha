"""Canonical historical raw provenance and atomic persist/probe APIs."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from weather_alpha.phase35.full_collection.policy import (
    FORBIDDEN_CANONICAL_PATH_PREFIXES,
    STABLE_PROVENANCE_PREFIX,
)
from weather_alpha.storage.raw import payload_digest

FORBIDDEN_ROOT_MARKERS: tuple[str, ...] = (
    *FORBIDDEN_CANONICAL_PATH_PREFIXES,
    "/var/folders/",
    "C:\\",
    "c:\\",
)


def canonical_raw_relative_path(provider: str, day: str, content_sha256: str) -> str:
    safe_provider = provider.replace("..", "").replace("/", "_").replace("\\", "_")
    digest = content_sha256.lower()
    return f"{STABLE_PROVENANCE_PREFIX}{safe_provider}/{day}/{digest}.json"


def stable_historical_raw_provenance_path(raw_path: str | Path) -> str:
    posix = Path(raw_path).as_posix()
    marker = STABLE_PROVENANCE_PREFIX
    index = posix.find(marker)
    if index >= 0:
        stable = posix[index:]
        assert_canonical_path_safe(stable)
        return stable
    raise ValueError(
        "historical raw_path must contain "
        f"{marker!r} for stable provenance serialization; got {posix!r}"
    )


def assert_canonical_path_safe(value: str) -> None:
    posix = Path(value).as_posix()
    if posix.startswith("/") or posix.startswith("\\") or (len(posix) >= 3 and posix[1:3] == ":/"):
        raise ValueError(
            f"canonical provenance must be a stable relative POSIX path; got {value!r}"
        )
    for prefix in FORBIDDEN_ROOT_MARKERS:
        if prefix in posix or posix.startswith(prefix.rstrip("/")):
            raise ValueError(f"canonical provenance must not serialize machine root {prefix!r}")
    if ".." in Path(posix).parts:
        raise ValueError("canonical provenance must not contain parent-directory segments")


def assert_text_has_no_machine_roots(text: str) -> None:
    for prefix in FORBIDDEN_CANONICAL_PATH_PREFIXES:
        if prefix in text:
            raise ValueError(f"canonical artifact leaked machine path prefix {prefix!r}")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def atomic_write_json(path: Path, payload: Any) -> str:
    encoded = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    atomic_write_bytes(path, (encoded + "\n").encode("utf-8"))
    return digest


@dataclass(frozen=True, slots=True)
class PersistedRaw:
    content_sha256: str
    stable_path: str
    runtime_path: str

    def as_dict(self) -> dict[str, str]:
        return {
            "content_sha256": self.content_sha256,
            "stable_path": self.stable_path,
        }


def persist_raw_atomically(
    root: Path,
    *,
    provider: str,
    day: str,
    payload: Any,
) -> PersistedRaw:
    digest = payload_digest(payload)
    stable = canonical_raw_relative_path(provider, day, digest)
    runtime = root / Path(stable)
    encoded = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    atomic_write_bytes(runtime, encoded.encode("utf-8"))
    return PersistedRaw(content_sha256=digest, stable_path=stable, runtime_path=str(runtime))


@dataclass(frozen=True, slots=True)
class RawProbeResult:
    exists: bool
    hash_matches: bool
    observed_sha256: str | None
    fail_closed: bool


def probe_raw(runtime_path: Path, expected_sha256: str) -> RawProbeResult:
    if not runtime_path.is_file():
        return RawProbeResult(
            exists=False, hash_matches=False, observed_sha256=None, fail_closed=True
        )
    payload = json.loads(runtime_path.read_text(encoding="utf-8"))
    observed = payload_digest(payload)
    matches = observed == expected_sha256.lower()
    return RawProbeResult(
        exists=True,
        hash_matches=matches,
        observed_sha256=observed,
        fail_closed=not matches,
    )
