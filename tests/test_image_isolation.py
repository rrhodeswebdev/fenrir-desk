"""DEC-1 and #185 in the running image, as the terminal user: the private volume, the server's
environment and open files are out of reach, and only data/, rules/ and strategies/ are writable.
Skipped anywhere else. Run (SPEC Commands):
`docker compose exec -u claude fenrir uvx pytest==9.1.1 -p no:cacheprovider <this file>`
"""

import os
from pathlib import Path

import pytest

PRIVATE = Path("/var/lib/fenrir")
APP = Path("/app")
RW_DIRS = ("data", "rules", "strategies")  # compose.yaml's read-write mounts

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


# case variants too: on a case-insensitive host (macOS) /app/.ENV is the host's .env (#185)
READ_ONLY = [
    ".env",
    ".ENV",
    "compose.yaml",
    "COMPOSE.YAML",
    "compose.override.yaml",
    "Compose.Override.yaml",
    "Dockerfile",
    "dockerfile.dockerignore",
    ".dockerignore",
    ".mcp.json",
    "CLAUDE.md",
    ".claude/settings.json",
    ".CLAUDE/settings.json",
    ".git/config",
    ".GIT/config",
    ".git/commondir",
    ".git/hooks/pre-commit",
    "src/x",
    "SRC/x",
    "web/x",
    "tests/x",
]


@pytest.mark.parametrize("path", READ_ONLY)
def test_repo_not_writable(path):
    with pytest.raises(OSError):
        (APP / path).write_text("x\n")


@pytest.mark.parametrize("name", RW_DIRS)
def test_working_dirs_writable(name):
    f = APP / name / ".fenrir-write-probe"
    f.write_text("x\n")
    f.unlink()


@pytest.mark.parametrize("name", [".git", ".claude", "src", *RW_DIRS])
def test_dirs_cannot_be_renamed(name):
    with pytest.raises(OSError):
        (APP / name).rename(APP / f"{name}.moved")
