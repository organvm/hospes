from __future__ import annotations

import os
from pathlib import Path
import subprocess
import uuid


ROOT = Path(__file__).resolve().parents[1]
BACKUP_HELPER = ROOT / "scripts" / "backup-private-runtime.sh"


def _fake_tools(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("hdiutil", "sqlite3", "shasum"):
        command = bin_dir / name
        command.write_text("#!/usr/bin/env bash\nexit 0\n")
        command.chmod(0o755)
    op = bin_dir / "op"
    op.write_text(
        "#!/usr/bin/env bash\n"
        "[[ $1 == read && $2 == --no-newline && $3 == op://vault/item/password ]]\n"
        "printf 'test-passphrase-at-least-16'\n"
    )
    op.chmod(0o755)
    return bin_dir


def _environment(tmp_path: Path, bin_dir: Path) -> dict[str, str]:
    database = tmp_path / "hospes.sqlite3"
    database.touch()
    environment = os.environ.copy()
    environment.update(
        {
            "HOSPES_DB": str(database),
            "HOSPES_BACKUP_CONTAINER": (
                "/Volumes/EncryptedBackups/HOSPES/"
                f"missing-test-{uuid.uuid4()}.sparsebundle"
            ),
            "PATH": f"{bin_dir}{os.pathsep}{environment['PATH']}",
        }
    )
    environment.pop("HOSPES_BACKUP_PASSPHRASE_FD", None)
    environment.pop("HOSPES_BACKUP_PASSPHRASE_REF", None)
    return environment


def test_backup_helper_accepts_pipe_only_passphrase_provider(tmp_path: Path) -> None:
    environment = _environment(tmp_path, _fake_tools(tmp_path))
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"test-passphrase-at-least-16\n")
        os.close(write_fd)
        environment["HOSPES_BACKUP_PASSPHRASE_FD"] = str(read_fd)
        result = subprocess.run(
            ["bash", str(BACKUP_HELPER), "verify"],
            env=environment,
            pass_fds=(read_fd,),
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        os.close(read_fd)

    assert result.returncode == 2
    assert "encrypted backup container does not exist" in result.stderr
    assert "test-passphrase" not in result.stdout + result.stderr
    assert "/dev/tty" not in result.stderr


def test_backup_helper_accepts_1password_reference(tmp_path: Path) -> None:
    environment = _environment(tmp_path, _fake_tools(tmp_path))
    environment["HOSPES_BACKUP_PASSPHRASE_REF"] = "op://vault/item/password"

    result = subprocess.run(
        ["bash", str(BACKUP_HELPER), "verify"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "encrypted backup container does not exist" in result.stderr
    assert "test-passphrase" not in result.stdout + result.stderr
    assert "/dev/tty" not in result.stderr


def test_backup_helper_rejects_multiple_passphrase_providers(tmp_path: Path) -> None:
    environment = _environment(tmp_path, _fake_tools(tmp_path))
    environment["HOSPES_BACKUP_PASSPHRASE_FD"] = "3"
    environment["HOSPES_BACKUP_PASSPHRASE_REF"] = "op://vault/item/password"

    result = subprocess.run(
        ["bash", str(BACKUP_HELPER), "verify"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "set only one backup passphrase provider" in result.stderr


def test_backup_helper_rejects_non_op_reference(tmp_path: Path) -> None:
    environment = _environment(tmp_path, _fake_tools(tmp_path))
    environment["HOSPES_BACKUP_PASSPHRASE_REF"] = "https://not-a-secret-owner.test"

    result = subprocess.run(
        ["bash", str(BACKUP_HELPER), "verify"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "must be an op:// secret reference" in result.stderr
