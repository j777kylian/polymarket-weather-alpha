"""JSON-on-disk raw payload store with content-addressed paths."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from weather_alpha.models.timeutil import ensure_utc


@dataclass(frozen=True, slots=True)
class PersistedPayload:
    request_url: str
    raw_path: str
    content_sha256: str
    retrieved_at: datetime
    payload: Any

    def __post_init__(self) -> None:
        object.__setattr__(self, "retrieved_at", ensure_utc(self.retrieved_at))


def canonical_request_url(url: str, params: dict[str, Any] | None = None) -> str:
    split = urlsplit(url)
    query_items = parse_qsl(split.query, keep_blank_values=True)
    if params:
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, bool):
                query_items.append((key, "true" if value else "false"))
            elif isinstance(value, list | tuple):
                for item in value:
                    query_items.append((key, str(item)))
            else:
                query_items.append((key, str(value)))
    query_items.sort()
    return urlunsplit(
        (split.scheme, split.netloc, split.path, urlencode(query_items, doseq=True), "")
    )


def payload_digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def deterministic_raw_path(
    root: Path,
    *,
    source: str,
    url: str,
    payload_digest: str,
    suffix: str = ".json",
) -> Path:
    material = f"{url}\n{payload_digest}".encode()
    digest = hashlib.sha256(material).hexdigest()
    safe_source = source.replace("..", "").replace("/", "_")
    return root / safe_source / digest[:2] / f"{digest}{suffix}"


def write_raw_json(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    path.write_text(encoded + "\n", encoding="utf-8")
    return payload_digest(payload)


def persist_raw_payload(
    root: Path,
    *,
    source: str,
    url: str,
    payload: Any,
    retrieved_at: datetime,
) -> PersistedPayload:
    request_url = canonical_request_url(url)
    digest = payload_digest(payload)
    path = deterministic_raw_path(root, source=source, url=request_url, payload_digest=digest)
    if not path.exists():
        write_raw_json(path, payload)
    return PersistedPayload(
        request_url=request_url,
        raw_path=str(path),
        content_sha256=digest,
        retrieved_at=retrieved_at,
        payload=payload,
    )
