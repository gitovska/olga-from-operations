import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy-olga"


@pytest.mark.parametrize("preview_fails", [False, True])
def test_deploy_preserves_state_and_gates_restart(tmp_path, preview_fails):
    destination = tmp_path / "deployment"
    destination.mkdir()
    (destination / "compose.yaml").write_text("services: {}\n")
    (destination / "data").mkdir()
    database = destination / "data/olga.sqlite3"
    database.write_bytes(b"existing delivery history")
    (destination / "data/olga.sqlite3-wal").write_bytes(b"journal")
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "commands"
    for name in ("docker", "rsync"):
        tool = tools / name
        tool.write_text(
            "#!/usr/bin/env bash\nset -eu\n"
            f'printf "%s\\n" "{name} $*" >> "$DEPLOY_TEST_LOG"\n'
            'if [[ "${DEPLOY_FAIL_PREVIEW:-}" == 1 && "$*" == *"olga dry-run" ]]; '
            "then exit 1; fi\n"
        )
        tool.chmod(0o755)
    result = subprocess.run(
        ["bash", str(SCRIPT), str(destination)],
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "DEPLOY_TEST_LOG": str(log),
            "DEPLOY_FAIL_PREVIEW": "1" if preview_fails else "0",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == (1 if preview_fails else 0), result.stderr
    assert database.read_bytes() == b"existing delivery history"
    backups = list(destination.glob("data.backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "olga.sqlite3").read_bytes() == database.read_bytes()
    assert (backups[0] / "olga.sqlite3-wal").read_bytes() == b"journal"
    commands = log.read_text()
    assert commands.index("compose stop") < commands.index("rsync")
    assert commands.index("compose build") < commands.index("olga dry-run")
    for excluded in ("/data/", "/data.backup-*/", "/secrets/", "/.env"):
        assert f"--exclude={excluded}" in commands
    assert "--delete" not in commands
    if preview_fails:
        assert "compose up" not in commands
    else:
        assert commands.index("olga dry-run") < commands.index("compose up")


def test_deploy_refuses_missing_delivery_history(tmp_path):
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    # Supply dependency stubs; validation must fail before either tool is invoked.
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("docker", "rsync"):
        tool = tools / name
        tool.write_text("#!/bin/sh\nexit 99\n")
        tool.chmod(0o755)
    result = subprocess.run(
        ["bash", str(SCRIPT), str(tmp_path)],
        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "Missing existing deployment file:" in result.stderr
    assert "data/olga.sqlite3" in result.stderr
