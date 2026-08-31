# Encryption and artifact custody

HOSPES encrypts private values before they reach SQLite, PostgreSQL, the local artifact tree, or
an S3-compatible object store. The algorithm is AES-256-GCM. Every tenant has one active,
versioned data-encryption key (DEK); historical retired versions remain available for decryption
until an attributable rekey operation is complete.

## Credential-wall boundary

The master key is owned by `credential://hospes/master-key`. It is never stored in configuration,
the database, an artifact receipt, or a provider error. A credential-wall bridge injects its
32-byte value as strict base64 in `HOSPES_MASTER_KEY_B64` for the process. The runtime wraps each
tenant DEK with that master key and persists only the wrapped ciphertext, nonce, authenticated
scope checksum, key version, and opaque credential reference.

Local tests and offline drills use `StaticMasterKeyProvider` with synthetic bytes. Live code uses
`EnvironmentMasterKeyProvider`; a missing, malformed, wrong-length, or differently referenced key
fails closed without echoing the value.

## Authenticated field scope

`FieldVault` binds every ciphertext to all of these values as AES-GCM additional authenticated
data:

- tenant;
- show;
- category;
- owning table;
- owning record;
- field; and
- tenant key version.

Moving ciphertext between tenants, shows, records, fields, categories, or key versions therefore
fails authentication. The encrypted categories are contact, correspondence, consent, financial,
private relationship, research, artifact metadata, and artifact content. Database rows contain no
plaintext value. Public projections receive only `private-field://…` references; reveal operations
require an authenticated operator role and may be narrowed further by the calling service.

Key rotation creates a new tenant DEK and switches active status from the prior version in one
transaction. New writes use the new version, while old ciphertext remains decryptable through its
recorded version. A revoked key is never unwrapped. Migration 10 revokes any pre-envelope key row
that lacks authenticated wrapped-key material before permitting a fresh active DEK.

## Artifact stores

`LocalArtifactStore` writes client-side ciphertext to a mode-0700 tenant-digest directory beneath
a mode-0700 root. Each object is staged, fsynced, atomically installed as mode 0600, and checked
against the database ciphertext receipt before decryption.

`S3ArtifactStore` uses the same encrypted envelope for any S3-compatible service, including
Cloudflare R2. It uploads only ciphertext, attaches its SHA-256 checksum as object metadata, then
requires an authenticated `head_object` readback before marking the database row ready. Downloads
verify the encrypted-object checksum, AES-GCM scope, plaintext checksum, and byte length. Bucket,
object key, endpoint, filesystem path, plaintext metadata, and raw checksums are excluded from the
public receipt.

The optional hosted extra supplies `boto3`; runtime credentials remain in the provider credential
chain referenced by the wall, never in tracked configuration. R2 endpoints must use HTTPS.

## Verification

```bash
python -m pytest tests/substrate/test_encryption_artifacts.py -q
bash scripts/verify-postgres-bootstrap.sh
./done.sh
```

The substrate predicate covers master-key validation, DEK wrapping and rotation, all required
authenticated scope dimensions, ciphertext tampering, cross-tenant and cross-show denial, role
denial, encrypted metadata, local permissions and checksum failures, S3-compatible checksum
readback, provider-error redaction, public receipt redaction, and migration compatibility.
