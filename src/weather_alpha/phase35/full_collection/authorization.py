"""Persisted authorization receipts. Offline; never contacts providers.

A receipt is a separate artifact from the immutable collection manifest. It is
created only by an explicit authorization operation and is the integrity anchor
checked before any provider GET. Collection execution must not create, replace,
or rewrite a receipt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from weather_alpha.phase35.full_collection.policy import AUTHORIZATION_SCHEMA_VERSION
from weather_alpha.phase35.full_collection.provenance import (
    assert_text_has_no_machine_roots,
    atomic_write_json,
)

AUTHORIZATION_REQUIRED_FIELDS: tuple[str, ...] = (
    "AUTHORIZATION_SCHEMA_VERSION",
    "AUTHORIZED_AT",
    "CODE_COMMIT",
    "COLLECTION_ID",
    "MANIFEST_SHA256",
    "REQUEST_POLICY_VERSION",
)


class AuthorizationError(ValueError):
    """Fail-closed refusal for absent/invalid/mismatched authorization receipts."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        message = code if not detail else f"{code}: {detail}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AuthorizationReceipt:
    collection_id: str
    manifest_sha256: str
    code_commit: str
    request_policy_version: str
    authorized_at: str
    schema_version: str

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "AUTHORIZATION_SCHEMA_VERSION": self.schema_version,
            "AUTHORIZED_AT": self.authorized_at,
            "CODE_COMMIT": self.code_commit,
            "COLLECTION_ID": self.collection_id,
            "MANIFEST_SHA256": self.manifest_sha256,
            "REQUEST_POLICY_VERSION": self.request_policy_version,
        }
        assert_text_has_no_machine_roots(str(payload))
        return payload


def write_authorization_receipt(
    *,
    destination: Path,
    collection_id: str,
    manifest_sha256: str,
    code_commit: str,
    request_policy_version: str,
    authorized_at: datetime,
) -> AuthorizationReceipt:
    if destination.is_file():
        raise ValueError(f"immutable authorization receipt already exists: {destination.name}")
    receipt = AuthorizationReceipt(
        collection_id=collection_id,
        manifest_sha256=manifest_sha256,
        code_commit=code_commit,
        request_policy_version=request_policy_version,
        authorized_at=authorized_at.isoformat(),
        schema_version=AUTHORIZATION_SCHEMA_VERSION,
    )
    payload = receipt.as_dict()
    atomic_write_json(destination, payload)
    return receipt


def load_authorization_receipt(path: Path) -> AuthorizationReceipt:
    name = path.name
    if not path.is_file():
        raise AuthorizationError(
            "missing_authorization", f"authorization receipt not found: {name}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AuthorizationError(
            "invalid_authorization", "authorization receipt is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise AuthorizationError(
            "invalid_authorization", "authorization receipt must be a JSON object"
        )
    schema = str(payload.get("AUTHORIZATION_SCHEMA_VERSION") or "")
    if schema != AUTHORIZATION_SCHEMA_VERSION:
        raise AuthorizationError(
            "invalid_authorization", "authorization schema is not the frozen contract"
        )
    if any(key not in payload for key in AUTHORIZATION_REQUIRED_FIELDS):
        raise AuthorizationError(
            "invalid_authorization", "authorization receipt is missing required fields"
        )
    receipt = AuthorizationReceipt(
        collection_id=str(payload["COLLECTION_ID"]),
        manifest_sha256=str(payload["MANIFEST_SHA256"]),
        code_commit=str(payload["CODE_COMMIT"]),
        request_policy_version=str(payload["REQUEST_POLICY_VERSION"]),
        authorized_at=str(payload["AUTHORIZED_AT"]),
        schema_version=schema,
    )
    if (
        not receipt.collection_id
        or not receipt.manifest_sha256
        or not receipt.code_commit
        or not receipt.request_policy_version
        or not receipt.authorized_at
    ):
        raise AuthorizationError(
            "invalid_authorization", "authorization receipt has empty required fields"
        )
    return receipt


def assert_receipt_binds_manifest(
    *,
    collection_id: str,
    manifest_sha256: str,
    code_commit: str,
    request_policy_version: str,
    receipt: AuthorizationReceipt,
) -> None:
    if receipt.collection_id != collection_id:
        raise AuthorizationError(
            "collection_id_mismatch",
            "authorization receipt COLLECTION_ID does not match the manifest",
        )
    if receipt.manifest_sha256 != manifest_sha256:
        raise AuthorizationError(
            "manifest_sha_mismatch",
            "authorization receipt MANIFEST_SHA256 does not match the recomputed manifest digest",
        )
    if receipt.code_commit != code_commit:
        raise AuthorizationError(
            "code_mismatch",
            "authorization receipt CODE_COMMIT does not match the manifest",
        )
    if receipt.request_policy_version != request_policy_version:
        raise AuthorizationError(
            "policy_mismatch",
            "authorization receipt REQUEST_POLICY_VERSION does not match the manifest",
        )
