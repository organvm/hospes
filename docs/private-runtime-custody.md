# Private runtime custody

The live deployment authority belongs in an encrypted, operator-controlled database outside the
checkout, for example:

```text
/secure/operator-data/HOSPES/production/hospes.sqlite3
```

Its parent should be mode `0700`; the database and any SQLite WAL/SHM files should be mode `0600`.
Operator launches must pass this path with `--db` or `HOSPES_DB`. The gitignored
`out/hospes.sqlite3` path is a development artifact, not live authority.

Plaintext database copies are forbidden on unencrypted backup media. The bounded backup helper
creates or opens one AES-256 sparsebundle, obtains its passphrase from exactly one local provider,
uses SQLite's online backup operation inside the mounted encrypted volume, restores to a temporary
local file, runs `PRAGMA quick_check`, and compares logical SHA-256 checksums:

```bash
export HOSPES_DB="/secure/operator-data/HOSPES/production/hospes.sqlite3"
HOSPES_BACKUP_CONTAINER=/encrypted-backups/HOSPES/production-backup.sparsebundle \
  scripts/backup-private-runtime.sh backup
```

The passphrase is never accepted on argv, written to disk, committed, or printed. With no provider,
the helper uses a local-terminal prompt. A credential owner may instead pipe the value through an
inherited descriptor by setting `HOSPES_BACKUP_PASSPHRASE_FD` to that descriptor number. A
1Password owner may instead set `HOSPES_BACKUP_PASSPHRASE_REF` to a runtime-only `op://`
reference. Both paths retrieve the value directly in memory and do not prompt. Never commit a
concrete reference. The disposable worktree copy must remain until the encrypted backup command
succeeds and its reported logical checksum agrees with the canonical database.
