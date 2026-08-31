from __future__ import annotations

import base64
import io
import json
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml

from hospes import artifacts, configuration, encryption, migrations, store

MASTER_REF = "credential://hospes/master-key"
MASTER_KEY = bytes(range(32))
TIMESTAMP = "2026-08-10T00:00:00+00:00"


@pytest.fixture
def custody(tmp_path: Path):
    connection = store.connect(tmp_path / "custody.sqlite3")
    for tenant, show in (("tenant-a", "show-a"), ("tenant-a", "show-b"), ("tenant-b", "show-a")):
        store.insert(
            connection,
            "show_registry",
            {
                "id": f"{tenant}-{show}",
                "tenant_id": tenant,
                "show_id": show,
                "label": f"{tenant} {show}",
                "config_ref": f"config://{tenant}/{show}",
                "status": "active",
                "created_at": TIMESTAMP,
                "updated_at": TIMESTAMP,
            },
        )
    connection.commit()
    provider = encryption.StaticMasterKeyProvider({MASTER_REF: MASTER_KEY})
    keys = encryption.TenantKeyManager(provider, master_key_ref=MASTER_REF)
    vault = encryption.FieldVault(keys)
    yield connection, keys, vault, tmp_path
    connection.close()


def field_scope(
    *,
    tenant: str = "tenant-a",
    show: str = "show-a",
    category: str = "contact",
    record: str = "guest-a",
    field: str = "route_value",
) -> encryption.PrivateFieldScope:
    return encryption.PrivateFieldScope(
        tenant_id=tenant,
        show_id=show,
        category=category,
        owner_table="guest_contacts",
        owner_record_id=record,
        field_name=field,
    )


def test_master_key_providers_fail_closed_without_echoing_values() -> None:
    encoded = base64.b64encode(MASTER_KEY).decode("ascii")
    provider = encryption.EnvironmentMasterKeyProvider(env={"HOSPES_MASTER_KEY_B64": encoded})
    assert provider.resolve(MASTER_REF) == MASTER_KEY
    assert isinstance(provider, encryption.MasterKeyProvider)

    for env in ({}, {"HOSPES_MASTER_KEY_B64": "not-base64"}, {"HOSPES_MASTER_KEY_B64": "YQ=="}):
        with pytest.raises(encryption.EncryptionConfigurationError) as caught:
            encryption.EnvironmentMasterKeyProvider(env=env).resolve(MASTER_REF)
        assert encoded not in str(caught.value)
    with pytest.raises(encryption.EncryptionConfigurationError, match="reference"):
        provider.resolve("credential://hospes/other-key")
    with pytest.raises(encryption.EncryptionConfigurationError, match="unavailable"):
        encryption.StaticMasterKeyProvider({}).resolve(MASTER_REF)
    with pytest.raises(encryption.EncryptionConfigurationError, match="credential-wall"):
        encryption.TenantKeyManager(provider, master_key_ref="raw-key-value")


def test_runtime_custody_configuration_is_typed_and_fail_closed(tmp_path: Path) -> None:
    runtime = configuration.load_runtime()
    custody_config = runtime.artifact_custody
    assert custody_config.master_key_credential_ref == MASTER_REF
    assert custody_config.private_records == "aes_256_gcm_tenant_envelope"
    assert custody_config.sensitive_categories == configuration.CUSTODY_SENSITIVE_CATEGORIES
    assert custody_config.outbound_requires_authorization_receipt is True

    raw = yaml.safe_load(
        (configuration.CONFIG_DIR / "runtime.yaml").read_text(encoding="utf-8")
    )
    raw["runtime"]["artifact_custody"]["local_root"] = "../private-artifacts"
    invalid = tmp_path / "invalid-runtime.yaml"
    invalid.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(configuration.ConfigurationError, match="safe relative path"):
        configuration.load_runtime(invalid)

    raw["runtime"]["artifact_custody"]["local_root"] = "out/private-artifacts"
    raw["runtime"]["artifact_custody"]["sensitive_categories"].pop()
    invalid.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(configuration.ConfigurationError, match="every required"):
        configuration.load_runtime(invalid)


@pytest.mark.parametrize(
    "category",
    [
        "contact",
        "correspondence",
        "consent",
        "financial",
        "private_relationship",
        "research",
        "artifact_metadata",
    ],
)
def test_required_private_categories_store_only_ciphertext(custody, category: str) -> None:
    connection, _keys, vault, _tmp_path = custody
    scope = field_scope(category=category, field=f"{category}_value")
    plaintext = f"synthetic private {category} value"
    reference = vault.put_text(connection, scope, plaintext)
    row = store.fetch_one(
        connection,
        "SELECT * FROM private_field_values WHERE id = ?",
        (reference.removeprefix("private-field://"),),
    )
    assert row is not None
    assert plaintext not in json.dumps(row, sort_keys=True)
    assert row["algorithm"] == "AES-256-GCM"
    assert row["key_version"] == 1
    assert vault.reveal_text(
        connection, reference, scope, actor_role="relationship_owner"
    ) == plaintext
    assert vault.public_reference(reference) == {"private_value_ref": reference}


def test_private_field_scope_role_and_ciphertext_tampering_fail_closed(custody) -> None:
    connection, _keys, vault, _tmp_path = custody
    scope = field_scope()
    reference = vault.put_text(connection, scope, "private route value")
    with pytest.raises(encryption.EncryptionAuthorizationError, match="role"):
        vault.reveal_text(connection, reference, scope, actor_role="guest_portal")
    with pytest.raises(encryption.EncryptionAuthorizationError, match="role"):
        vault.reveal_text(
            connection,
            reference,
            scope,
            actor_role="producer",
            allowed_roles={"relationship_owner"},
        )
    with pytest.raises(encryption.EncryptionAuthorizationError, match="scope"):
        vault.reveal_text(
            connection,
            reference,
            field_scope(show="show-b"),
            actor_role="relationship_owner",
        )
    with pytest.raises(encryption.EncryptionAuthorizationError, match="scope"):
        vault.reveal_text(
            connection,
            reference,
            field_scope(tenant="tenant-b"),
            actor_role="relationship_owner",
        )

    row = store.fetch_one(
        connection,
        "SELECT * FROM private_field_values WHERE id = ?",
        (reference.removeprefix("private-field://"),),
    )
    assert row is not None
    sealed = encryption.SealedValue(
        key_version=row["key_version"],
        nonce=row["nonce"],
        ciphertext=row["ciphertext"],
        aad_checksum=row["aad_checksum"],
    )
    with pytest.raises(encryption.EncryptionIntegrityError, match="scope"):
        vault.open_bytes(connection, field_scope(record="guest-b"), sealed)

    tampered = bytearray(base64.urlsafe_b64decode(row["ciphertext"]))
    tampered[-1] ^= 1
    store.update(
        connection,
        "private_field_values",
        reference.removeprefix("private-field://"),
        {"ciphertext": base64.urlsafe_b64encode(bytes(tampered)).decode("ascii")},
    )
    connection.commit()
    with pytest.raises(encryption.EncryptionIntegrityError, match="authentication"):
        vault.reveal_text(
            connection, reference, scope, actor_role="relationship_owner"
        )


def test_private_json_updates_reuse_reference_and_validate_media(custody) -> None:
    connection, _keys, vault, _tmp_path = custody
    scope = field_scope(category="research", field="private_brief")
    reference = vault.put_json(connection, scope, {"risk": "first"})
    first = store.fetch_one(
        connection,
        "SELECT ciphertext FROM private_field_values WHERE id = ?",
        (reference.removeprefix("private-field://"),),
    )["ciphertext"]
    repeated = vault.put_json(connection, scope, {"risk": "second"})
    second = store.fetch_one(
        connection,
        "SELECT ciphertext FROM private_field_values WHERE id = ?",
        (reference.removeprefix("private-field://"),),
    )["ciphertext"]
    assert repeated == reference
    assert first != second
    assert vault.reveal_json(
        connection, reference, scope, actor_role="producer"
    ) == {"risk": "second"}
    with pytest.raises(TypeError, match="mapping"):
        vault.put_json(connection, scope, ["not", "a", "mapping"])
    with pytest.raises(ValueError, match="media_type"):
        vault.put_bytes(connection, scope, b"value", media_type="invalid")
    with pytest.raises(TypeError, match="bytes"):
        vault.seal_bytes(connection, scope, "not-bytes")


def test_key_rotation_keeps_history_decryptable_and_new_writes_on_latest(custody) -> None:
    connection, keys, vault, _tmp_path = custody
    old_scope = field_scope(field="old_value")
    old_reference = vault.put_text(connection, old_scope, "old private value")
    assert keys.rotate(connection, "tenant-a") == 2
    new_scope = field_scope(field="new_value")
    new_reference = vault.put_text(connection, new_scope, "new private value")
    versions = store.fetch_all(
        connection,
        "SELECT key_version, status FROM tenant_encryption_keys "
        "WHERE tenant_id = ? ORDER BY key_version",
        ("tenant-a",),
    )
    assert versions == [
        {"key_version": 1, "status": "retired"},
        {"key_version": 2, "status": "active"},
    ]
    assert vault.reveal_text(
        connection, old_reference, old_scope, actor_role="network_operator"
    ) == "old private value"
    assert vault.reveal_text(
        connection, new_reference, new_scope, actor_role="network_operator"
    ) == "new private value"
    assert keys.rotate(connection, "tenant-b") == 1


def test_wrapped_key_scope_and_revocation_fail_closed(custody) -> None:
    connection, keys, vault, _tmp_path = custody
    vault.put_text(connection, field_scope(), "private value")
    store.update(
        connection,
        "tenant_encryption_keys",
        store.fetch_one(
            connection,
            "SELECT id FROM tenant_encryption_keys WHERE tenant_id = ?",
            ("tenant-a",),
        )["id"],
        {"wrap_aad_checksum": "0" * 64},
    )
    connection.commit()
    with pytest.raises(encryption.EncryptionIntegrityError, match="scope"):
        keys.key_for_version(connection, "tenant-a", 1)
    connection.execute(
        "UPDATE tenant_encryption_keys SET wrap_aad_checksum = ?, status = 'revoked' "
        "WHERE tenant_id = ?",
        (
            hashlib_sha256_key_aad("tenant-a", 1),
            "tenant-a",
        ),
    )
    connection.commit()
    with pytest.raises(encryption.EncryptionConfigurationError, match="unavailable"):
        keys.key_for_version(connection, "tenant-a", 1)


def hashlib_sha256_key_aad(tenant_id: str, version: int) -> str:
    import hashlib

    return hashlib.sha256(
        encryption.TenantKeyManager._wrap_aad(tenant_id, version, MASTER_REF)
    ).hexdigest()


def test_sealed_value_contract_rejects_malformed_payloads(custody) -> None:
    connection, _keys, vault, _tmp_path = custody
    scope = field_scope(category="artifact_content", field="content")
    sealed = vault.seal_bytes(connection, scope, b"encrypted object")
    assert encryption.SealedValue.from_bytes(sealed.to_bytes()) == sealed
    for payload in (
        b"not-json",
        b"[]",
        json.dumps({"schema": "wrong", "algorithm": "wrong"}).encode(),
        json.dumps(
            {
                "schema": encryption.ENVELOPE_SCHEMA,
                "algorithm": encryption.ALGORITHM,
                "key_version": 0,
            }
        ).encode(),
    ):
        with pytest.raises(encryption.EncryptionIntegrityError):
            encryption.SealedValue.from_bytes(payload)
    with pytest.raises(encryption.EncryptionAuthorizationError, match="invalid"):
        vault.public_reference("private-field://invalid")


def test_encryption_contract_rejects_invalid_scope_shapes_and_encodings(custody) -> None:
    connection, _keys, vault, _tmp_path = custody
    with pytest.raises(ValueError, match="tenant_id"):
        field_scope(tenant="bad tenant")
    with pytest.raises(ValueError, match="owner_table"):
        encryption.PrivateFieldScope(
            "tenant-a", "show-a", "contact", "BadTable", "guest-a", "route"
        )
    with pytest.raises(ValueError, match="category"):
        field_scope(category="unknown")
    with pytest.raises(ValueError, match="key_version"):
        field_scope().aad(0)
    with pytest.raises(encryption.EncryptionIntegrityError, match="invalid"):
        encryption._b64decode(None, "fixture")
    with pytest.raises(encryption.EncryptionIntegrityError, match="invalid"):
        encryption._b64decode("%%%", "fixture")
    with pytest.raises(encryption.EncryptionIntegrityError, match="invalid"):
        encryption.SealedValue.from_bytes("not-bytes")

    text_scope = field_scope(field="invalid_text")
    text_ref = vault.put_bytes(
        connection, text_scope, b"\xff", media_type="text/plain"
    )
    with pytest.raises(encryption.EncryptionIntegrityError, match="encoding"):
        vault.reveal_text(connection, text_ref, text_scope, actor_role="producer")
    json_scope = field_scope(category="research", field="invalid_json")
    json_ref = vault.put_bytes(
        connection, json_scope, b"[]", media_type="application/json"
    )
    with pytest.raises(encryption.EncryptionIntegrityError, match="JSON"):
        vault.reveal_json(connection, json_ref, json_scope, actor_role="producer")
    with pytest.raises(TypeError, match="string"):
        vault.put_text(connection, field_scope(field="bad_text"), b"bytes")


class FailingLocalStore(artifacts.LocalArtifactStore):
    def _write_payload(self, artifact_id: str, payload: bytes) -> str:
        raise RuntimeError("provider diagnostic must not escape")


def test_local_artifact_store_encrypts_metadata_and_content(custody) -> None:
    connection, _keys, vault, tmp_path = custody
    root = tmp_path / "objects"
    backend = artifacts.LocalArtifactStore(
        connection,
        vault,
        root,
        tenant_id="tenant-a",
        show_id="show-a",
    )
    assert isinstance(backend, artifacts.ArtifactStore)
    receipt = backend.put(
        b"private media master",
        metadata={"filename": "private-master.wav", "consent": "private"},
        created_by="producer-a",
    )
    public = receipt.public_dict()
    assert public == backend.public_receipt(receipt.artifact_ref)
    assert set(public) == {"artifact_ref", "custody", "metadata_ref", "status"}
    assert "checksum" not in json.dumps(public)
    assert backend.get(receipt.artifact_ref, actor_role="producer") == b"private media master"
    assert backend.metadata(receipt.artifact_ref, actor_role="producer") == {
        "consent": "private",
        "filename": "private-master.wav",
    }
    row = store.fetch_one(
        connection,
        "SELECT * FROM artifact_objects WHERE id = ?",
        (receipt.artifact_ref.removeprefix("artifact://"),),
    )
    metadata_row = store.fetch_one(
        connection,
        "SELECT * FROM private_field_values WHERE id = ?",
        (row["metadata_field_id"],),
    )
    assert "private-master.wav" not in json.dumps(metadata_row)
    target = backend._path(row["id"])
    assert b"private media master" not in target.read_bytes()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(artifacts.ArtifactAuthorizationError, match="role"):
        backend.get(receipt.artifact_ref, actor_role="guest_portal")


def test_local_artifact_tamper_cross_scope_and_failures_are_redacted(custody) -> None:
    connection, keys, vault, tmp_path = custody
    backend = artifacts.LocalArtifactStore(
        connection, vault, tmp_path / "objects", tenant_id="tenant-a", show_id="show-a"
    )
    receipt = backend.put(b"master", metadata={"kind": "audio"}, created_by="producer-a")
    artifact_id = receipt.artifact_ref.removeprefix("artifact://")
    path = backend._path(artifact_id)
    payload = bytearray(path.read_bytes())
    payload[-3] ^= 1
    path.write_bytes(bytes(payload))
    with pytest.raises(artifacts.ArtifactIntegrityError, match="checksum"):
        backend.get(receipt.artifact_ref, actor_role="producer")

    other = artifacts.LocalArtifactStore(
        connection, encryption.FieldVault(keys), tmp_path / "objects", tenant_id="tenant-b", show_id="show-a"
    )
    with pytest.raises(artifacts.ArtifactAuthorizationError, match="scope"):
        other.get(receipt.artifact_ref, actor_role="producer")

    failing = FailingLocalStore(
        connection, vault, tmp_path / "failed", tenant_id="tenant-a", show_id="show-a"
    )
    with pytest.raises(artifacts.ArtifactStoreError) as caught:
        failing.put(b"value", metadata={"kind": "test"}, created_by="producer-a")
    assert "diagnostic" not in str(caught.value)
    assert store.fetch_one(
        connection,
        "SELECT status FROM artifact_objects WHERE backend = 'local' "
        "ORDER BY created_at DESC LIMIT 1",
    )["status"] == "failed"


def test_local_artifact_root_rejects_symlinks_and_invalid_inputs(custody) -> None:
    connection, _keys, vault, tmp_path = custody
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(artifacts.ArtifactStoreError, match="symlink"):
        artifacts.LocalArtifactStore(
            connection, vault, linked, tenant_id="tenant-a", show_id="show-a"
        )
    backend = artifacts.LocalArtifactStore(
        connection, vault, tmp_path / "valid", tenant_id="tenant-a", show_id="show-a"
    )
    with pytest.raises(ValueError, match="bounded bytes"):
        backend.put("not-bytes", metadata={}, created_by="producer-a")
    with pytest.raises(TypeError, match="mapping"):
        backend.put(b"value", metadata=[], created_by="producer-a")
    with pytest.raises(ValueError, match="created_by"):
        backend.put(b"value", metadata={}, created_by="not allowed spaces")
    with pytest.raises(artifacts.ArtifactAuthorizationError, match="invalid"):
        backend.public_receipt("artifact://invalid")


def test_artifact_database_receipt_mismatches_fail_closed(custody) -> None:
    import hashlib

    connection, keys, vault, tmp_path = custody
    backend = artifacts.LocalArtifactStore(
        connection, vault, tmp_path / "receipt-mismatch", tenant_id="tenant-a", show_id="show-a"
    )
    receipt = backend.put(b"value", metadata={"kind": "test"}, created_by="producer-a")
    artifact_id = receipt.artifact_ref.removeprefix("artifact://")
    store.update(connection, "artifact_objects", artifact_id, {"status": "staging"})
    connection.commit()
    with pytest.raises(artifacts.ArtifactStoreError, match="not ready"):
        backend.get(receipt.artifact_ref, actor_role="producer")

    assert keys.rotate(connection, "tenant-a") == 2
    store.update(
        connection,
        "artifact_objects",
        artifact_id,
        {"status": "ready", "content_key_version": 2},
    )
    connection.commit()
    with pytest.raises(artifacts.ArtifactIntegrityError, match="key version"):
        backend.get(receipt.artifact_ref, actor_role="producer")

    store.update(
        connection,
        "artifact_objects",
        artifact_id,
        {"content_key_version": 1, "content_checksum": "0" * 64},
    )
    connection.commit()
    with pytest.raises(artifacts.ArtifactIntegrityError, match="content checksum"):
        backend.get(receipt.artifact_ref, actor_role="producer")

    store.update(
        connection,
        "artifact_objects",
        artifact_id,
        {"content_checksum": hashlib.sha256(b"value").hexdigest(), "size_bytes": 999},
    )
    connection.commit()
    with pytest.raises(artifacts.ArtifactIntegrityError, match="size"):
        backend.get(receipt.artifact_ref, actor_role="producer")


class FakeS3:
    def __init__(self, *, head_checksum: str | None = None, fail: bool = False):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.metadata: dict[tuple[str, str], dict[str, str]] = {}
        self.head_checksum = head_checksum
        self.fail = fail
        self.put_calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("private provider diagnostic")
        key = (kwargs["Bucket"], kwargs["Key"])
        self.objects[key] = kwargs["Body"]
        self.metadata[key] = kwargs["Metadata"]
        self.put_calls.append(kwargs)

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        key = (kwargs["Bucket"], kwargs["Key"])
        metadata = dict(self.metadata[key])
        if self.head_checksum is not None:
            metadata["sha256"] = self.head_checksum
        return {"Metadata": metadata}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        return {"Body": io.BytesIO(self.objects[(kwargs["Bucket"], kwargs["Key"])])}


def test_s3_compatible_store_uploads_only_verified_ciphertext(custody) -> None:
    connection, _keys, vault, _tmp_path = custody
    client = FakeS3()
    backend = artifacts.S3ArtifactStore(
        connection,
        vault,
        client,
        bucket="hospes-private",
        tenant_id="tenant-a",
        show_id="show-a",
        prefix="private/hospes",
    )
    receipt = backend.put(
        b"private video master",
        metadata={"kind": "video", "private": True},
        created_by="producer-a",
    )
    call = client.put_calls[0]
    row = store.fetch_one(
        connection,
        "SELECT ciphertext_checksum FROM artifact_objects WHERE id = ?",
        (receipt.artifact_ref.removeprefix("artifact://"),),
    )
    assert b"private video master" not in call["Body"]
    assert row is not None
    assert call["Metadata"]["sha256"] == row["ciphertext_checksum"]
    assert len(call["Metadata"]["sha256"]) == 64
    assert backend.get(receipt.artifact_ref, actor_role="network_operator") == b"private video master"
    assert backend.metadata(receipt.artifact_ref, actor_role="network_operator") == {
        "kind": "video",
        "private": True,
    }
    assert "tenant-a" not in call["Key"]
    assert "hospes-private" not in json.dumps(receipt.public_dict())


def test_s3_checksum_and_provider_failures_fail_closed(custody) -> None:
    connection, _keys, vault, _tmp_path = custody
    mismatch = artifacts.S3ArtifactStore(
        connection,
        vault,
        FakeS3(head_checksum="0" * 64),
        bucket="hospes-private",
        tenant_id="tenant-a",
        show_id="show-a",
    )
    with pytest.raises(artifacts.ArtifactIntegrityError, match="unverified"):
        mismatch.put(b"value", metadata={"kind": "test"}, created_by="producer-a")

    failed = artifacts.S3ArtifactStore(
        connection,
        vault,
        FakeS3(fail=True),
        bucket="hospes-private",
        tenant_id="tenant-a",
        show_id="show-a",
    )
    with pytest.raises(artifacts.ArtifactStoreError) as caught:
        failed.put(b"value", metadata={"kind": "test"}, created_by="producer-a")
    assert "diagnostic" not in str(caught.value)


def test_s3_configuration_rejects_unsafe_values(custody) -> None:
    connection, _keys, vault, _tmp_path = custody
    with pytest.raises(artifacts.ArtifactStoreError, match="bucket"):
        artifacts.S3ArtifactStore(
            connection, vault, FakeS3(), bucket="INVALID", tenant_id="tenant-a", show_id="show-a"
        )
    with pytest.raises(artifacts.ArtifactStoreError, match="prefix"):
        artifacts.S3ArtifactStore(
            connection,
            vault,
            FakeS3(),
            bucket="hospes-private",
            tenant_id="tenant-a",
            show_id="show-a",
            prefix="../escape",
        )
    with pytest.raises(artifacts.ArtifactStoreError, match="HTTPS"):
        artifacts.S3ArtifactStore.from_boto3(
            connection,
            vault,
            endpoint_url="http://object.example.test",
            bucket="hospes-private",
            credential_ref="credential://hospes/object-store",
            tenant_id="tenant-a",
            show_id="show-a",
        )
    with pytest.raises(artifacts.ArtifactStoreError, match="credential-wall"):
        artifacts.S3ArtifactStore.from_boto3(
            connection,
            vault,
            endpoint_url="https://object.example.test",
            bucket="hospes-private",
            credential_ref="raw-object-secret",
            tenant_id="tenant-a",
            show_id="show-a",
        )


def test_migration_ten_declares_scoped_encryption_tables(custody) -> None:
    connection, _keys, _vault, _tmp_path = custody
    assert connection.execute(
        "SELECT MAX(version) FROM schema_migrations"
    ).fetchone()[0] == migrations.LATEST_VERSION
    assert any(
        item["version"] == 10 and item["name"] == "tenant_encryption_and_artifact_custody"
        for item in migrations.applied_migrations(connection)
    )
    key_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(tenant_encryption_keys)")
    }
    assert {"wrapped_key_ciphertext", "wrap_nonce", "wrap_aad_checksum"} <= key_columns
    foreign_keys = connection.execute("PRAGMA foreign_key_list(artifact_objects)").fetchall()
    assert {row[2] for row in foreign_keys} >= {
        "show_registry",
        "tenant_encryption_keys",
        "private_field_values",
    }
