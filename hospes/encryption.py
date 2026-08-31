"""Tenant-scoped AES-256-GCM envelope encryption for private HOSPES data.

The credential wall injects a 256-bit master key at runtime. The master key is
never persisted; it wraps one versioned data-encryption key (DEK) per tenant.
Field ciphertext is authenticated against the full tenant/show/record scope so
copying it to another row or field fails closed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from typing import Any, Collection, Mapping, Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import generation, store
from .configuration import CREDENTIAL_REF

ALGORITHM = "AES-256-GCM"
KEY_BYTES = 32
NONCE_BYTES = 12
ENVELOPE_SCHEMA = "HOSPESSealedValueV1"
PRIVATE_FIELD_REF = re.compile(
    r"^private-field://([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
OPAQUE_SCOPE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
SQL_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PRIVATE_FIELD_CATEGORIES = frozenset(
    {
        "contact",
        "correspondence",
        "consent",
        "financial",
        "private_relationship",
        "research",
        "artifact_metadata",
        "artifact_content",
    }
)
AUTHORIZED_OPERATOR_ROLES = frozenset(
    {
        "host",
        "network_operator",
        "producer",
        "editorial_owner",
        "relationship_owner",
    }
)


class EncryptionError(RuntimeError):
    """Base exception whose messages never include key or plaintext material."""


class EncryptionConfigurationError(EncryptionError):
    """The credential wall or tenant key record is not usable."""


class EncryptionAuthorizationError(EncryptionError):
    """The caller is not authorized to reveal the requested private value."""


class EncryptionIntegrityError(EncryptionError):
    """Ciphertext, scope, or authenticated metadata failed validation."""


def _require_scope_value(value: str, label: str) -> str:
    normalized = str(value).strip()
    if not OPAQUE_SCOPE_VALUE.fullmatch(normalized):
        raise ValueError(f"{label} must be an opaque scoped identifier")
    return normalized


def _require_identifier(value: str, label: str) -> str:
    normalized = str(value).strip()
    if not SQL_IDENTIFIER.fullmatch(normalized):
        raise ValueError(f"{label} must be a lower-case identifier")
    return normalized


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _b64decode(value: Any, label: str) -> bytes:
    if not isinstance(value, str) or len(value) > 131072:
        raise EncryptionIntegrityError(f"{label} is invalid")
    try:
        return base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise EncryptionIntegrityError(f"{label} is invalid") from exc


@dataclass(frozen=True)
class PrivateFieldScope:
    tenant_id: str
    show_id: str
    category: str
    owner_table: str
    owner_record_id: str
    field_name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _require_scope_value(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "show_id", _require_scope_value(self.show_id, "show_id"))
        if self.category not in PRIVATE_FIELD_CATEGORIES:
            raise ValueError(f"category must be one of {sorted(PRIVATE_FIELD_CATEGORIES)}")
        object.__setattr__(
            self, "owner_table", _require_identifier(self.owner_table, "owner_table")
        )
        object.__setattr__(
            self,
            "owner_record_id",
            _require_scope_value(self.owner_record_id, "owner_record_id"),
        )
        object.__setattr__(
            self, "field_name", _require_identifier(self.field_name, "field_name")
        )

    def aad(self, key_version: int) -> bytes:
        if isinstance(key_version, bool) or key_version < 1:
            raise ValueError("key_version must be a positive integer")
        return json.dumps(
            {
                "category": self.category,
                "field": self.field_name,
                "key_version": key_version,
                "record": self.owner_record_id,
                "schema": "HOSPESFieldAADV1",
                "show": self.show_id,
                "table": self.owner_table,
                "tenant": self.tenant_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


@dataclass(frozen=True)
class SealedValue:
    key_version: int
    nonce: str
    ciphertext: str
    aad_checksum: str

    def as_columns(self) -> dict[str, Any]:
        return {
            "algorithm": ALGORITHM,
            "key_version": self.key_version,
            "nonce": self.nonce,
            "ciphertext": self.ciphertext,
            "aad_checksum": self.aad_checksum,
        }

    def to_bytes(self) -> bytes:
        return (
            json.dumps(
                {
                    "aad_checksum": self.aad_checksum,
                    "algorithm": ALGORITHM,
                    "ciphertext": self.ciphertext,
                    "key_version": self.key_version,
                    "nonce": self.nonce,
                    "schema": ENVELOPE_SCHEMA,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, payload: bytes) -> SealedValue:
        if not isinstance(payload, bytes) or len(payload) > 128 * 1024 * 1024:
            raise EncryptionIntegrityError("encrypted payload is invalid")
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EncryptionIntegrityError("encrypted payload is invalid") from exc
        if not isinstance(raw, Mapping):
            raise EncryptionIntegrityError("encrypted payload is invalid")
        if raw.get("schema") != ENVELOPE_SCHEMA or raw.get("algorithm") != ALGORITHM:
            raise EncryptionIntegrityError("encrypted payload contract is unsupported")
        version = raw.get("key_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise EncryptionIntegrityError("encrypted payload key version is invalid")
        nonce = _b64decode(raw.get("nonce"), "encrypted nonce")
        ciphertext = _b64decode(raw.get("ciphertext"), "encrypted ciphertext")
        checksum = raw.get("aad_checksum")
        if len(nonce) != NONCE_BYTES or len(ciphertext) < 16:
            raise EncryptionIntegrityError("encrypted payload shape is invalid")
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise EncryptionIntegrityError("encrypted AAD checksum is invalid")
        return cls(version, _b64encode(nonce), _b64encode(ciphertext), checksum)


@runtime_checkable
class MasterKeyProvider(Protocol):
    def resolve(self, credential_ref: str) -> bytes: ...


@dataclass(frozen=True)
class StaticMasterKeyProvider:
    """Explicit provider for isolated tests and offline drills only."""

    keys: Mapping[str, bytes]

    def resolve(self, credential_ref: str) -> bytes:
        value = self.keys.get(credential_ref)
        if value is None:
            raise EncryptionConfigurationError("master key credential is unavailable")
        return bytes(value)


@dataclass(frozen=True)
class EnvironmentMasterKeyProvider:
    """Read a wall-injected base64 key without persisting or logging it."""

    credential_ref: str = "credential://hospes/master-key"
    env_name: str = "HOSPES_MASTER_KEY_B64"
    env: Mapping[str, str] | None = None

    def resolve(self, credential_ref: str) -> bytes:
        if credential_ref != self.credential_ref:
            raise EncryptionConfigurationError("master key credential reference is unavailable")
        source = os.environ if self.env is None else self.env
        encoded = source.get(self.env_name)
        if not encoded:
            raise EncryptionConfigurationError("master key credential is unavailable")
        try:
            key = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise EncryptionConfigurationError("master key credential is invalid") from exc
        if len(key) != KEY_BYTES:
            raise EncryptionConfigurationError("master key credential is invalid")
        return key


class TenantKeyManager:
    """Create, unwrap, and rotate one AES data key per tenant/version."""

    def __init__(
        self,
        provider: MasterKeyProvider,
        *,
        master_key_ref: str = "credential://hospes/master-key",
    ) -> None:
        if not CREDENTIAL_REF.fullmatch(master_key_ref):
            raise EncryptionConfigurationError(
                "master_key_ref must be an opaque credential-wall reference"
            )
        self.provider = provider
        self.master_key_ref = master_key_ref

    @staticmethod
    def _wrap_aad(tenant_id: str, key_version: int, credential_ref: str) -> bytes:
        return json.dumps(
            {
                "algorithm": ALGORITHM,
                "credential_ref": credential_ref,
                "key_version": key_version,
                "schema": "HOSPESKeyWrapAADV1",
                "tenant": tenant_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def _master_key(self, credential_ref: str) -> bytes:
        key = self.provider.resolve(credential_ref)
        if not isinstance(key, bytes) or len(key) != KEY_BYTES:
            raise EncryptionConfigurationError("master key credential is invalid")
        return key

    def _unwrap(self, row: Mapping[str, Any]) -> tuple[int, bytes]:
        try:
            tenant_id = str(row["tenant_id"])
            version = int(row["key_version"])
            credential_ref = str(row["wrapped_key_ref"])
            nonce = _b64decode(row["wrap_nonce"], "wrapped key nonce")
            ciphertext = _b64decode(
                row["wrapped_key_ciphertext"], "wrapped key ciphertext"
            )
            expected_checksum = str(row["wrap_aad_checksum"])
        except (KeyError, TypeError, ValueError) as exc:
            raise EncryptionConfigurationError("tenant encryption key record is invalid") from exc
        aad = self._wrap_aad(tenant_id, version, credential_ref)
        actual_checksum = hashlib.sha256(aad).hexdigest()
        if not hmac.compare_digest(actual_checksum, expected_checksum):
            raise EncryptionIntegrityError("tenant key authenticated scope is invalid")
        try:
            key = AESGCM(self._master_key(credential_ref)).decrypt(nonce, ciphertext, aad)
        except InvalidTag as exc:
            raise EncryptionIntegrityError("tenant key authentication failed") from exc
        if len(key) != KEY_BYTES:
            raise EncryptionIntegrityError("tenant data key is invalid")
        return version, key

    def _new_key_row(self, conn: store.DatabaseConnection, tenant_id: str, status: str) -> dict[str, Any]:
        tenant = _require_scope_value(tenant_id, "tenant_id")
        maximum = conn.execute(
            "SELECT MAX(key_version) AS maximum FROM tenant_encryption_keys "
            "WHERE tenant_id = ?",
            (tenant,),
        ).fetchone()
        version = int(maximum[0] or 0) + 1
        data_key = secrets.token_bytes(KEY_BYTES)
        nonce = secrets.token_bytes(NONCE_BYTES)
        aad = self._wrap_aad(tenant, version, self.master_key_ref)
        wrapped = AESGCM(self._master_key(self.master_key_ref)).encrypt(
            nonce, data_key, aad
        )
        timestamp = generation.now().isoformat()
        return {
            "id": generation.new_id("tenant_encryption_key"),
            "tenant_id": tenant,
            "key_version": version,
            "algorithm": ALGORITHM,
            "wrapped_key_ref": self.master_key_ref,
            "wrapped_key_ciphertext": _b64encode(wrapped),
            "wrap_nonce": _b64encode(nonce),
            "wrap_aad_checksum": hashlib.sha256(aad).hexdigest(),
            "status": status,
            "created_at": timestamp,
            "retired_at": None,
        }

    def ensure_active(
        self,
        conn: store.DatabaseConnection,
        tenant_id: str,
        *,
        commit: bool = True,
    ) -> tuple[int, bytes]:
        tenant = _require_scope_value(tenant_id, "tenant_id")
        row = store.fetch_one(
            conn,
            "SELECT * FROM tenant_encryption_keys "
            "WHERE tenant_id = ? AND status = 'active' "
            "ORDER BY key_version DESC LIMIT 1",
            (tenant,),
        )
        if row is not None:
            return self._unwrap(row)
        candidate = self._new_key_row(conn, tenant, "active")
        try:
            store.insert(conn, "tenant_encryption_keys", candidate)
            if commit:
                conn.commit()
            return candidate["key_version"], self._unwrap(candidate)[1]
        except Exception:
            if not commit:
                raise
            conn.rollback()
            row = store.fetch_one(
                conn,
                "SELECT * FROM tenant_encryption_keys "
                "WHERE tenant_id = ? AND status = 'active' "
                "ORDER BY key_version DESC LIMIT 1",
                (tenant,),
            )
            if row is None:
                raise
            return self._unwrap(row)

    def key_for_version(
        self, conn: store.DatabaseConnection, tenant_id: str, key_version: int
    ) -> bytes:
        tenant = _require_scope_value(tenant_id, "tenant_id")
        row = store.fetch_one(
            conn,
            "SELECT * FROM tenant_encryption_keys "
            "WHERE tenant_id = ? AND key_version = ? AND status != 'revoked'",
            (tenant, key_version),
        )
        if row is None:
            raise EncryptionConfigurationError("tenant encryption key is unavailable")
        return self._unwrap(row)[1]

    def rotate(self, conn: store.DatabaseConnection, tenant_id: str) -> int:
        tenant = _require_scope_value(tenant_id, "tenant_id")
        active = store.fetch_one(
            conn,
            "SELECT * FROM tenant_encryption_keys "
            "WHERE tenant_id = ? AND status = 'active' "
            "ORDER BY key_version DESC LIMIT 1",
            (tenant,),
        )
        if active is None:
            return self.ensure_active(conn, tenant)[0]
        candidate = self._new_key_row(conn, tenant, "retired")
        # End the read transaction opened while deriving the next version. The
        # candidate insert and active-key switch then share one write transaction.
        conn.commit()
        timestamp = generation.now().isoformat()
        try:
            conn.execute(
                "BEGIN" if conn.backend == "postgresql" else "BEGIN IMMEDIATE"
            )
            store.insert(conn, "tenant_encryption_keys", candidate)
            conn.execute(
                "UPDATE tenant_encryption_keys SET status = 'retired', retired_at = ? "
                "WHERE tenant_id = ? AND key_version = ? AND status = 'active'",
                (timestamp, tenant, active["key_version"]),
            )
            conn.execute(
                "UPDATE tenant_encryption_keys SET status = 'active', retired_at = NULL "
                "WHERE tenant_id = ? AND key_version = ? AND status = 'retired'",
                (tenant, candidate["key_version"]),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return int(candidate["key_version"])


class FieldVault:
    """Persist and reveal encrypted private fields through opaque references."""

    def __init__(self, keys: TenantKeyManager):
        self.keys = keys

    def seal_bytes(
        self,
        conn: store.DatabaseConnection,
        scope: PrivateFieldScope,
        value: bytes,
        *,
        commit_key: bool = True,
    ) -> SealedValue:
        if not isinstance(value, bytes):
            raise TypeError("private value must be bytes")
        version, key = self.keys.ensure_active(
            conn, scope.tenant_id, commit=commit_key
        )
        aad = scope.aad(version)
        nonce = secrets.token_bytes(NONCE_BYTES)
        ciphertext = AESGCM(key).encrypt(nonce, value, aad)
        return SealedValue(
            key_version=version,
            nonce=_b64encode(nonce),
            ciphertext=_b64encode(ciphertext),
            aad_checksum=hashlib.sha256(aad).hexdigest(),
        )

    def open_bytes(
        self,
        conn: store.DatabaseConnection,
        scope: PrivateFieldScope,
        sealed: SealedValue,
    ) -> bytes:
        aad = scope.aad(sealed.key_version)
        checksum = hashlib.sha256(aad).hexdigest()
        if not hmac.compare_digest(checksum, sealed.aad_checksum):
            raise EncryptionIntegrityError("encrypted field scope is invalid")
        nonce = _b64decode(sealed.nonce, "encrypted nonce")
        ciphertext = _b64decode(sealed.ciphertext, "encrypted ciphertext")
        if len(nonce) != NONCE_BYTES or len(ciphertext) < 16:
            raise EncryptionIntegrityError("encrypted field shape is invalid")
        key = self.keys.key_for_version(conn, scope.tenant_id, sealed.key_version)
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, aad)
        except InvalidTag as exc:
            raise EncryptionIntegrityError("encrypted field authentication failed") from exc

    def put_bytes(
        self,
        conn: store.DatabaseConnection,
        scope: PrivateFieldScope,
        value: bytes,
        *,
        media_type: str = "application/octet-stream",
        commit: bool = True,
    ) -> str:
        if not isinstance(media_type, str) or not re.fullmatch(
            r"[a-z0-9.+-]+/[a-z0-9.+-]+", media_type
        ):
            raise ValueError("media_type is invalid")
        sealed = self.seal_bytes(conn, scope, value, commit_key=commit)
        existing = store.fetch_one(
            conn,
            "SELECT id FROM private_field_values WHERE tenant_id = ? AND show_id = ? "
            "AND owner_table = ? AND owner_record_id = ? AND field_name = ?",
            (
                scope.tenant_id,
                scope.show_id,
                scope.owner_table,
                scope.owner_record_id,
                scope.field_name,
            ),
        )
        timestamp = generation.now().isoformat()
        values = {
            "category": scope.category,
            "media_type": media_type,
            **sealed.as_columns(),
            "updated_at": timestamp,
        }
        if existing is None:
            field_id = generation.new_id("private_field")
            store.insert(
                conn,
                "private_field_values",
                {
                    "id": field_id,
                    "tenant_id": scope.tenant_id,
                    "show_id": scope.show_id,
                    "owner_table": scope.owner_table,
                    "owner_record_id": scope.owner_record_id,
                    "field_name": scope.field_name,
                    "created_at": timestamp,
                    **values,
                },
            )
        else:
            field_id = str(existing["id"])
            store.update(conn, "private_field_values", field_id, values)
        if commit:
            conn.commit()
        return f"private-field://{field_id}"

    def put_text(
        self,
        conn: store.DatabaseConnection,
        scope: PrivateFieldScope,
        value: str,
        *,
        commit: bool = True,
    ) -> str:
        if not isinstance(value, str):
            raise TypeError("private text must be a string")
        return self.put_bytes(
            conn,
            scope,
            value.encode("utf-8"),
            media_type="text/plain",
            commit=commit,
        )

    def put_json(
        self,
        conn: store.DatabaseConnection,
        scope: PrivateFieldScope,
        value: Mapping[str, Any],
        *,
        commit: bool = True,
    ) -> str:
        if not isinstance(value, Mapping):
            raise TypeError("private JSON must be a mapping")
        payload = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return self.put_bytes(
            conn,
            scope,
            payload,
            media_type="application/json",
            commit=commit,
        )

    @staticmethod
    def _field_id(reference: str) -> str:
        match = PRIVATE_FIELD_REF.fullmatch(str(reference))
        if match is None:
            raise EncryptionAuthorizationError("private field reference is invalid")
        return match.group(1)

    def reveal_bytes(
        self,
        conn: store.DatabaseConnection,
        reference: str,
        scope: PrivateFieldScope,
        *,
        actor_role: str,
        allowed_roles: Collection[str] = AUTHORIZED_OPERATOR_ROLES,
    ) -> bytes:
        if actor_role not in AUTHORIZED_OPERATOR_ROLES or actor_role not in allowed_roles:
            raise EncryptionAuthorizationError("operator role cannot reveal this private field")
        field_id = self._field_id(reference)
        row = store.fetch_one(
            conn,
            "SELECT * FROM private_field_values "
            "WHERE id = ? AND tenant_id = ? AND show_id = ?",
            (field_id, scope.tenant_id, scope.show_id),
        )
        if row is None:
            raise EncryptionAuthorizationError("private field is unavailable in this scope")
        actual_scope = PrivateFieldScope(
            tenant_id=str(row["tenant_id"]),
            show_id=str(row["show_id"]),
            category=str(row["category"]),
            owner_table=str(row["owner_table"]),
            owner_record_id=str(row["owner_record_id"]),
            field_name=str(row["field_name"]),
        )
        if actual_scope != scope:
            raise EncryptionAuthorizationError("private field is unavailable in this scope")
        sealed = SealedValue(
            key_version=int(row["key_version"]),
            nonce=str(row["nonce"]),
            ciphertext=str(row["ciphertext"]),
            aad_checksum=str(row["aad_checksum"]),
        )
        return self.open_bytes(conn, scope, sealed)

    def reveal_text(
        self,
        conn: store.DatabaseConnection,
        reference: str,
        scope: PrivateFieldScope,
        *,
        actor_role: str,
        allowed_roles: Collection[str] = AUTHORIZED_OPERATOR_ROLES,
    ) -> str:
        payload = self.reveal_bytes(
            conn,
            reference,
            scope,
            actor_role=actor_role,
            allowed_roles=allowed_roles,
        )
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EncryptionIntegrityError("private field text encoding is invalid") from exc

    def reveal_json(
        self,
        conn: store.DatabaseConnection,
        reference: str,
        scope: PrivateFieldScope,
        *,
        actor_role: str,
        allowed_roles: Collection[str] = AUTHORIZED_OPERATOR_ROLES,
    ) -> dict[str, Any]:
        payload = self.reveal_bytes(
            conn,
            reference,
            scope,
            actor_role=actor_role,
            allowed_roles=allowed_roles,
        )
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise EncryptionIntegrityError("private field JSON is invalid") from exc
        if not isinstance(value, dict):
            raise EncryptionIntegrityError("private field JSON is invalid")
        return value

    @classmethod
    def public_reference(cls, reference: str) -> dict[str, str]:
        cls._field_id(reference)
        return {"private_value_ref": reference}


__all__ = [
    "ALGORITHM",
    "AUTHORIZED_OPERATOR_ROLES",
    "EncryptionAuthorizationError",
    "EncryptionConfigurationError",
    "EncryptionError",
    "EncryptionIntegrityError",
    "EnvironmentMasterKeyProvider",
    "FieldVault",
    "MasterKeyProvider",
    "PrivateFieldScope",
    "SealedValue",
    "StaticMasterKeyProvider",
    "TenantKeyManager",
]
