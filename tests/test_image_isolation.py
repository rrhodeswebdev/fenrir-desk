"""DEC-1 and #185 in the running image, as the terminal user: the private volume, the server's
environment and open files are out of reach, and nothing the host reads unreviewed is writable.
Skipped anywhere else. Run (SPEC Commands):
`docker compose exec -u claude fenrir uvx pytest==9.1.1 -p no:cacheprovider <this file>`
"""

import os
from pathlib import Path

import pytest

PRIVATE = Path("/var/lib/fenrir")
APP = Path("/app")
OVERRIDES = (
    "compose.override.yaml",
    "compose.override.yml",
    "docker-compose.override.yaml",
    "docker-compose.override.yml",
)

pytestmark = pytest.mark.skipif(
    not PRIVATE.is_dir() or os.geteuid() == 0, reason="runs in the image as the terminal user"
)


def _server() -> Path:
    """/proc/<pid> of `fenrir serve`: a child of docker-init (PID 1), whose argv also holds
    `fenrir serve`, so match the entry script's full path."""
    found = [
        p
        for p in Path("/proc").iterdir()
        if p.name.isdigit() and b"/opt/venv/bin/fenrir\0serve" in _cmdline(p)
    ]
    assert len(found) == 1, found
    return found[0]


def _cmdline(p: Path) -> bytes:
    try:
        return p.joinpath("cmdline").read_bytes()
    except OSError:
        return b""


def test_server_runs_as_root_under_an_init():
    assert _server().stat().st_uid == 0
    assert _server().name != "1"


def test_private_volume_unreadable():
    with pytest.raises(PermissionError):
        os.listdir(PRIVATE)
    for name in ("session.key", ".env", "fenrir.db"):
        with pytest.raises(PermissionError):
            (PRIVATE / name).read_bytes()


def test_server_environment_unreadable():
    with pytest.raises(PermissionError):
        (_server() / "environ").read_bytes()


def test_server_fds_unreadable():
    fds = _server() / "fd"
    with pytest.raises(PermissionError):
        os.listdir(fds)
    for fd in range(3, 32):
        with pytest.raises(PermissionError):
            (fds / str(fd)).read_bytes()


def test_repo_env_is_an_empty_read_only_placeholder():
    assert (APP / ".env").read_bytes() == b""  # nothing of the host's .env
    with pytest.raises(OSError):
        (APP / ".env").write_text("COMPOSE_FILE=evil.yaml\n")


@pytest.mark.parametrize("name", OVERRIDES)
def test_compose_overrides_not_writable(name):
    with pytest.raises(OSError):
        (APP / name).write_text("services: {}\n")


@pytest.mark.parametrize(
    "path",
    [
        ".claude/settings.json",
        ".git/config",
        ".git/commondir",
        ".git/hooks/pre-commit",
        "Dockerfile.dockerignore",
        ".dockerignore",
    ],
)
def test_host_read_paths_not_writable(path):
    with pytest.raises(OSError):
        (APP / path).write_text("x\n")


@pytest.mark.parametrize(
    "name", [".git", ".claude", ".env", "compose.yaml", "Dockerfile.dockerignore"]
)
def test_mount_points_cannot_be_renamed(name):
    with pytest.raises(OSError):
        (APP / name).rename(APP / f"{name}.moved")
