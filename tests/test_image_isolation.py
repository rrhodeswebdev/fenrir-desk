"""DEC-1 in the running image, as the terminal user: the private volume, the server's
environment and its open files are out of reach. Skipped anywhere else. Run (SPEC Commands):
`docker compose exec -u claude fenrir uvx pytest==9.1.1 -p no:cacheprovider <this file>`
"""

import os
from pathlib import Path

import pytest

PRIVATE = Path("/var/lib/fenrir")

pytestmark = pytest.mark.skipif(
    not PRIVATE.is_dir() or os.geteuid() == 0, reason="runs in the image as the terminal user"
)


def test_server_is_pid_1_and_root():
    assert b"fenrir" in Path("/proc/1/cmdline").read_bytes()
    assert Path("/proc/1").stat().st_uid == 0


def test_private_volume_unreadable():
    with pytest.raises(PermissionError):
        os.listdir(PRIVATE)
    for name in ("session.key", ".env", "fenrir.db"):
        with pytest.raises(PermissionError):
            (PRIVATE / name).read_bytes()


def test_server_environment_unreadable():
    with pytest.raises(PermissionError):
        Path("/proc/1/environ").read_bytes()


def test_server_fds_unreadable():
    with pytest.raises(PermissionError):
        os.listdir("/proc/1/fd")
    for fd in range(3, 32):
        with pytest.raises(PermissionError):
            Path(f"/proc/1/fd/{fd}").read_bytes()
