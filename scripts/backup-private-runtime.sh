#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: HOSPES_DB=/absolute/path/hospes.sqlite3 $0 backup|verify" >&2
  exit 2
}

[[ $# -eq 1 ]] || usage
ACTION="$1"
[[ "$ACTION" == "backup" || "$ACTION" == "verify" ]] || usage
DB_PATH="${HOSPES_DB:-}"
[[ -n "$DB_PATH" && "$DB_PATH" == /* && -f "$DB_PATH" ]] || {
  echo "HOSPES_DB must name the existing absolute canonical database" >&2
  exit 2
}
CONTAINER_PATH="${HOSPES_BACKUP_CONTAINER:-/Volumes/EncryptedBackups/HOSPES/production-backup.sparsebundle}"
[[ "$CONTAINER_PATH" == /Volumes/EncryptedBackups/* ]] || {
  echo "HOSPES_BACKUP_CONTAINER must remain under /Volumes/EncryptedBackups" >&2
  exit 2
}
command -v hdiutil >/dev/null
command -v sqlite3 >/dev/null
command -v shasum >/dev/null

WORK_ROOT="$(mktemp -d /tmp/hospes-private-backup.XXXXXX)"
MOUNT_PATH="$WORK_ROOT/mount"
RESTORE_PATH="$WORK_ROOT/restored.sqlite3"
mkdir -m 700 "$MOUNT_PATH"
MOUNTED=0

cleanup() {
  if [[ "$MOUNTED" -eq 1 ]]; then
    hdiutil detach "$MOUNT_PATH" -quiet || true
  fi
  if [[ "$WORK_ROOT" == /tmp/hospes-private-backup.* ]]; then
    rm -rf -- "$WORK_ROOT"
  fi
  unset HOSPES_BACKUP_PASSPHRASE
}
trap cleanup EXIT

PASSPHRASE_FD="${HOSPES_BACKUP_PASSPHRASE_FD:-}"
PASSPHRASE_REF="${HOSPES_BACKUP_PASSPHRASE_REF:-}"
[[ -z "$PASSPHRASE_FD" || -z "$PASSPHRASE_REF" ]] || {
  echo "set only one backup passphrase provider" >&2
  exit 2
}
if [[ -n "$PASSPHRASE_FD" ]]; then
  [[ "$PASSPHRASE_FD" =~ ^[0-9]+$ ]] || {
    echo "HOSPES_BACKUP_PASSPHRASE_FD must be an open numeric file descriptor" >&2
    exit 2
  }
  IFS= read -r HOSPES_BACKUP_PASSPHRASE <&"$PASSPHRASE_FD" ||
    [[ -n "${HOSPES_BACKUP_PASSPHRASE:-}" ]]
elif [[ -n "$PASSPHRASE_REF" ]]; then
  [[ "$PASSPHRASE_REF" == op://* ]] || {
    echo "HOSPES_BACKUP_PASSPHRASE_REF must be an op:// secret reference" >&2
    exit 2
  }
  command -v op >/dev/null
  HOSPES_BACKUP_PASSPHRASE="$(op read --no-newline "$PASSPHRASE_REF")"
else
  echo -n "Encrypted HOSPES backup passphrase: " >/dev/tty
  IFS= read -r -s HOSPES_BACKUP_PASSPHRASE </dev/tty
  echo >/dev/tty
fi
[[ ${#HOSPES_BACKUP_PASSPHRASE} -ge 16 ]] || {
  echo "passphrase must be at least 16 characters" >&2
  exit 2
}

if [[ "$ACTION" == "backup" && ! -e "$CONTAINER_PATH" ]]; then
  mkdir -p "$(dirname "$CONTAINER_PATH")"
  printf '%s' "$HOSPES_BACKUP_PASSPHRASE" | hdiutil create \
    -size 64m -fs APFS -volname HOSPES_PRIVATE_PILOT_BACKUP \
    -type SPARSEBUNDLE -encryption AES-256 -stdinpass "$CONTAINER_PATH" >/dev/null
fi
[[ -e "$CONTAINER_PATH" ]] || {
  echo "encrypted backup container does not exist: $CONTAINER_PATH" >&2
  exit 2
}

printf '%s' "$HOSPES_BACKUP_PASSPHRASE" | hdiutil attach \
  -stdinpass -nobrowse -mountpoint "$MOUNT_PATH" "$CONTAINER_PATH" >/dev/null
MOUNTED=1

if [[ "$ACTION" == "backup" ]]; then
  sqlite3 "$DB_PATH" ".backup '$MOUNT_PATH/hospes.sqlite3'"
  chmod 600 "$MOUNT_PATH/hospes.sqlite3"
  sqlite3 "$MOUNT_PATH/hospes.sqlite3" "PRAGMA quick_check" | grep -qx ok
fi

sqlite3 "$MOUNT_PATH/hospes.sqlite3" ".backup '$RESTORE_PATH'"
chmod 600 "$RESTORE_PATH"
sqlite3 "$RESTORE_PATH" "PRAGMA quick_check" | grep -qx ok
SOURCE_LOGICAL="$(sqlite3 "$DB_PATH" .dump | shasum -a 256 | awk '{print $1}')"
RESTORE_LOGICAL="$(sqlite3 "$RESTORE_PATH" .dump | shasum -a 256 | awk '{print $1}')"
[[ "$SOURCE_LOGICAL" == "$RESTORE_LOGICAL" ]] || {
  echo "restore checksum differs from canonical database" >&2
  exit 1
}
printf 'verified logical checksum %s\n' "$RESTORE_LOGICAL"
