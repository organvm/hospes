"""Encrypted local and S3-compatible artifact custody.

Both backends receive only client-side AES-256-GCM ciphertext. Database rows
carry internal checksums and opaque references; public receipts never expose
object keys, bucket names, filesystem paths, or plaintext metadata.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable
from urllib.parse import urlsplit

from . import generation, store
from .configuration import CREDENTIAL_REF
from .encryption import (
    AUTHORIZED_OPERATOR_ROLES,
    EncryptionError,
    FieldVault,
    PrivateFieldScope,
    SealedValue,
)

ARTIFACT_REF = re.compile(
    r"^artifact://([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
OPAQUE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
OBJECT_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024


class ArtifactStoreError(RuntimeError):
    """Base artifact error with storage-provider details redacted."""


class ArtifactAuthorizationError(ArtifactStoreError):
    """The caller cannot access this private artifact."""


class ArtifactIntegrityError(ArtifactStoreError):
    """Stored ciphertext or plaintext did not match its custody receipt."""


@dataclass(frozen=True)
class ArtifactReceipt:
    artifact_ref: str
    status: str
    backend: str
    checksum_ref: str
    metadata_ref: str

    def public_dict(self) -> dict[str, str]:
        return {
            "artifact_ref": self.artifact_ref,
            "custody": "client-side-encrypted",
            "metadata_ref": self.metadata_ref,
            "status": self.status,
        }


@runtime_checkable
class ArtifactStore(Protocol):
    def put(
        self,
        content: bytes,
        *,
        metadata: Mapping[str, Any],
        created_by: str,
    ) -> ArtifactReceipt: ...

    def get(self, reference: str, *, actor_role: str) -> bytes: ...

    def metadata(self, reference: str, *, actor_role: str) -> dict[str, Any]: ...

    def public_receipt(self, reference: str) -> dict[str, str]: ...


def _opaque(value: str, label: str) -> str:
    normalized = str(value).strip()
    if not OPAQUE_VALUE.fullmatch(normalized):
        raise ValueError(f"{label} must be an opaque identifier")
    return normalized


class _EncryptedArtifactStore:
    backend: str

    def __init__(
        self,
        conn: store.DatabaseConnection,
        vault: FieldVault,
        *,
        tenant_id: str,
        show_id: str,
    ) -> None:
        self.conn = conn
        self.vault = vault
        self.tenant_id = _opaque(tenant_id, "tenant_id")
        self.show_id = _opaque(show_id, "show_id")

    @staticmethod
    def _artifact_id(reference: str) -> str:
        match = ARTIFACT_REF.fullmatch(str(reference))
        if match is None:
            raise ArtifactAuthorizationError("artifact reference is invalid")
        return match.group(1)

    @staticmethod
    def _require_role(actor_role: str) -> None:
        if actor_role not in AUTHORIZED_OPERATOR_ROLES:
            raise ArtifactAuthorizationError("operator role cannot access private artifacts")

    def _content_scope(self, artifact_id: str) -> PrivateFieldScope:
        return PrivateFieldScope(
            tenant_id=self.tenant_id,
            show_id=self.show_id,
            category="artifact_content",
            owner_table="artifact_objects",
            owner_record_id=artifact_id,
            field_name="content",
        )

    def _metadata_scope(self, artifact_id: str) -> PrivateFieldScope:
        return PrivateFieldScope(
            tenant_id=self.tenant_id,
            show_id=self.show_id,
            category="artifact_metadata",
            owner_table="artifact_objects",
            owner_record_id=artifact_id,
            field_name="metadata",
        )

    def _row(self, reference: str) -> dict[str, Any]:
        artifact_id = self._artifact_id(reference)
        row = store.fetch_one(
            self.conn,
            "SELECT * FROM artifact_objects "
            "WHERE id = ? AND tenant_id = ? AND show_id = ?",
            (artifact_id, self.tenant_id, self.show_id),
        )
        if row is None:
            raise ArtifactAuthorizationError("artifact is unavailable in this scope")
        return row

    @staticmethod
    def _receipt(row: Mapping[str, Any]) -> ArtifactReceipt:
        artifact_id = str(row["id"])
        return ArtifactReceipt(
            artifact_ref=f"artifact://{artifact_id}",
            status=str(row["status"]),
            backend=str(row["backend"]),
            checksum_ref=f"checksum://artifact/{artifact_id}",
            metadata_ref=f"private-field://{row['metadata_field_id']}",
        )

    def put(
        self,
        content: bytes,
        *,
        metadata: Mapping[str, Any],
        created_by: str,
    ) -> ArtifactReceipt:
        if not isinstance(content, bytes) or len(content) > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact content must be bounded bytes")
        if not isinstance(metadata, Mapping):
            raise TypeError("artifact metadata must be a mapping")
        actor = _opaque(created_by, "created_by")
        artifact_id = generation.new_id("artifact_object")
        sealed = self.vault.seal_bytes(
            self.conn, self._content_scope(artifact_id), content
        )
        payload = sealed.to_bytes()
        ciphertext_checksum = hashlib.sha256(payload).hexdigest()
        content_checksum = hashlib.sha256(content).hexdigest()
        metadata_ref = self.vault.put_json(
            self.conn,
            self._metadata_scope(artifact_id),
            metadata,
            commit=False,
        )
        metadata_field_id = metadata_ref.removeprefix("private-field://")
        timestamp = generation.now().isoformat()
        row = {
            "id": artifact_id,
            "tenant_id": self.tenant_id,
            "show_id": self.show_id,
            "backend": self.backend,
            "object_key_ref": f"artifact-object://{artifact_id}",
            "content_key_version": sealed.key_version,
            "ciphertext_checksum": ciphertext_checksum,
            "content_checksum": content_checksum,
            "size_bytes": len(content),
            "metadata_field_id": metadata_field_id,
            "status": "staging",
            "created_by": actor,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        store.insert(self.conn, "artifact_objects", row)
        self.conn.commit()
        try:
            stored_checksum = self._write_payload(artifact_id, payload)
            if not secrets.compare_digest(stored_checksum, ciphertext_checksum):
                raise ArtifactIntegrityError("artifact ciphertext checksum mismatch")
        except Exception as exc:
            store.update(
                self.conn,
                "artifact_objects",
                artifact_id,
                {"status": "failed", "updated_at": generation.now().isoformat()},
            )
            self.conn.commit()
            if isinstance(exc, ArtifactStoreError):
                raise
            raise ArtifactStoreError("encrypted artifact write failed") from exc
        store.update(
            self.conn,
            "artifact_objects",
            artifact_id,
            {"status": "ready", "updated_at": generation.now().isoformat()},
        )
        self.conn.commit()
        row["status"] = "ready"
        return self._receipt(row)

    def get(self, reference: str, *, actor_role: str) -> bytes:
        self._require_role(actor_role)
        row = self._row(reference)
        if row["status"] != "ready":
            raise ArtifactStoreError("artifact is not ready")
        try:
            payload = self._read_payload(str(row["id"]))
            actual_ciphertext = hashlib.sha256(payload).hexdigest()
            if not secrets.compare_digest(
                actual_ciphertext, str(row["ciphertext_checksum"])
            ):
                raise ArtifactIntegrityError("artifact ciphertext checksum mismatch")
            sealed = SealedValue.from_bytes(payload)
            if sealed.key_version != int(row["content_key_version"]):
                raise ArtifactIntegrityError("artifact key version mismatch")
            content = self.vault.open_bytes(
                self.conn, self._content_scope(str(row["id"])), sealed
            )
            actual_content = hashlib.sha256(content).hexdigest()
            if not secrets.compare_digest(actual_content, str(row["content_checksum"])):
                raise ArtifactIntegrityError("artifact content checksum mismatch")
            if len(content) != int(row["size_bytes"]):
                raise ArtifactIntegrityError("artifact size mismatch")
            return content
        except ArtifactStoreError:
            raise
        except EncryptionError as exc:
            raise ArtifactIntegrityError("artifact encryption verification failed") from exc
        except Exception as exc:
            raise ArtifactStoreError("encrypted artifact read failed") from exc

    def metadata(self, reference: str, *, actor_role: str) -> dict[str, Any]:
        self._require_role(actor_role)
        row = self._row(reference)
        return self.vault.reveal_json(
            self.conn,
            f"private-field://{row['metadata_field_id']}",
            self._metadata_scope(str(row["id"])),
            actor_role=actor_role,
        )

    def public_receipt(self, reference: str) -> dict[str, str]:
        return self._receipt(self._row(reference)).public_dict()

    def _write_payload(self, artifact_id: str, payload: bytes) -> str:
        raise NotImplementedError

    def _read_payload(self, artifact_id: str) -> bytes:
        raise NotImplementedError


class LocalArtifactStore(_EncryptedArtifactStore):
    backend = "local"

    def __init__(
        self,
        conn: store.DatabaseConnection,
        vault: FieldVault,
        root: str | Path,
        *,
        tenant_id: str,
        show_id: str,
    ) -> None:
        super().__init__(conn, vault, tenant_id=tenant_id, show_id=show_id)
        requested_root = Path(root)
        if requested_root.is_symlink():
            raise ArtifactStoreError("local artifact root cannot be a symlink")
        self.root = requested_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ArtifactStoreError("local artifact root is invalid")
        self.root.chmod(0o700)

    def _directory(self) -> Path:
        if self.root.is_symlink() or not self.root.is_dir():
            raise ArtifactStoreError("local artifact root is invalid")
        digest = hashlib.sha256(self.tenant_id.encode("utf-8")).hexdigest()[:24]
        directory = self.root / digest
        if directory.is_symlink():
            raise ArtifactStoreError("local artifact directory is invalid")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        return directory

    def _path(self, artifact_id: str) -> Path:
        return self._directory() / f"{artifact_id}.hospes.enc"

    def _write_payload(self, artifact_id: str, payload: bytes) -> str:
        target = self._path(artifact_id)
        if target.exists() or target.is_symlink():
            raise ArtifactStoreError("local artifact target already exists")
        temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            target.chmod(0o600)
            directory_descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return hashlib.sha256(payload).hexdigest()

    def _read_payload(self, artifact_id: str) -> bytes:
        target = self._path(artifact_id)
        if target.is_symlink() or not target.is_file():
            raise ArtifactStoreError("local artifact object is unavailable")
        if target.stat().st_size > MAX_ARTIFACT_BYTES * 2:
            raise ArtifactIntegrityError("encrypted artifact payload is oversized")
        return target.read_bytes()


class S3ArtifactStore(_EncryptedArtifactStore):
    backend = "s3"

    def __init__(
        self,
        conn: store.DatabaseConnection,
        vault: FieldVault,
        client: Any,
        *,
        bucket: str,
        tenant_id: str,
        show_id: str,
        prefix: str = "hospes",
    ) -> None:
        super().__init__(conn, vault, tenant_id=tenant_id, show_id=show_id)
        if not BUCKET_NAME.fullmatch(bucket):
            raise ArtifactStoreError("object-store bucket name is invalid")
        normalized_prefix = prefix.strip("/")
        if (
            not OBJECT_PREFIX.fullmatch(normalized_prefix)
            or ".." in normalized_prefix.split("/")
        ):
            raise ArtifactStoreError("object-store prefix is invalid")
        self.client = client
        self.bucket = bucket
        self.prefix = normalized_prefix

    @classmethod
    def from_boto3(
        cls,
        conn: store.DatabaseConnection,
        vault: FieldVault,
        *,
        endpoint_url: str,
        bucket: str,
        credential_ref: str,
        tenant_id: str,
        show_id: str,
        prefix: str = "hospes",
        region_name: str = "auto",
    ) -> S3ArtifactStore:
        parsed = urlsplit(endpoint_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ArtifactStoreError("object-store endpoint must use HTTPS")
        if not CREDENTIAL_REF.fullmatch(credential_ref):
            raise ArtifactStoreError(
                "object-store credential must be an opaque credential-wall reference"
            )
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise ArtifactStoreError(
                "S3 artifact custody requires the HOSPES hosted extra"
            ) from exc
        client = boto3.client(
            "s3", endpoint_url=endpoint_url, region_name=region_name
        )
        return cls(
            conn,
            vault,
            client,
            bucket=bucket,
            tenant_id=tenant_id,
            show_id=show_id,
            prefix=prefix,
        )

    def _key(self, artifact_id: str) -> str:
        digest = hashlib.sha256(self.tenant_id.encode("utf-8")).hexdigest()[:24]
        return f"{self.prefix}/{digest}/{artifact_id}.hospes.enc"

    def _write_payload(self, artifact_id: str, payload: bytes) -> str:
        checksum = hashlib.sha256(payload).hexdigest()
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._key(artifact_id),
                Body=payload,
                ContentType="application/octet-stream",
                Metadata={"sha256": checksum},
            )
            head = self.client.head_object(
                Bucket=self.bucket, Key=self._key(artifact_id)
            )
        except Exception as exc:
            raise ArtifactStoreError("S3-compatible artifact write failed") from exc
        metadata = head.get("Metadata") if isinstance(head, Mapping) else None
        observed = metadata.get("sha256") if isinstance(metadata, Mapping) else None
        if not isinstance(observed, str) or not secrets.compare_digest(observed, checksum):
            raise ArtifactIntegrityError("S3-compatible artifact checksum is unverified")
        return checksum

    def _read_payload(self, artifact_id: str) -> bytes:
        try:
            response = self.client.get_object(
                Bucket=self.bucket, Key=self._key(artifact_id)
            )
            body = response["Body"]
            payload = body.read() if hasattr(body, "read") else body
        except Exception as exc:
            raise ArtifactStoreError("S3-compatible artifact read failed") from exc
        if not isinstance(payload, bytes) or len(payload) > MAX_ARTIFACT_BYTES * 2:
            raise ArtifactIntegrityError("S3-compatible artifact payload is invalid")
        return payload


__all__ = [
    "ArtifactAuthorizationError",
    "ArtifactIntegrityError",
    "ArtifactReceipt",
    "ArtifactStore",
    "ArtifactStoreError",
    "LocalArtifactStore",
    "S3ArtifactStore",
]
