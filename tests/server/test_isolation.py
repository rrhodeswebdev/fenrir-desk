import os
import pwd
import stat
from pathlib import Path

import pytest

from fenrir.server import isolation
from fenrir.server.isolation import check, isolation_problems
from fenrir.settings import Settings

APP = Path("/app")
PRIVATE = Path("/var/lib/fenrir")


def st(mode=stat.S_IFDIR | 0o700, uid=0):
    return os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))


def reasons(**kw):
    args = {
        "euid": 0,
        "pty_user_uid": 1000,
        "pty_user_gid": 1000,
        "private_dir_stat": st(),
        "private_dir_path": PRIVATE,
        "app_root": APP,
    } | kw
    return check(**args)


def test_isolated_image_has_no_problems():
    assert reasons() == []


@pytest.mark.parametrize(
    "kw, fragment",
    [
        ({"euid": 1000}, "not running as root"),
        ({"pty_user_uid": None, "pty_user_gid": None}, "names no existing user"),
        ({"pty_user_uid": 0}, "is root"),
        ({"pty_user_gid": 0}, "primary group is root"),
        ({"private_dir_path": Path("/app/data/private")}, "inside /app"),
        ({"private_dir_path": APP}, "inside /app"),
        ({"private_dir_stat": None}, "does not exist"),
        ({"private_dir_stat": st(mode=stat.S_IFREG | 0o700)}, "not a directory"),
        ({"private_dir_stat": st(uid=1000)}, "not owned by root"),
        ({"private_dir_stat": st(mode=stat.S_IFDIR | 0o750)}, "mode 0o750"),
    ],
)
def test_each_reason(kw, fragment):
    [reason] = reasons(**kw)
    assert fragment in reason


def test_reasons_accumulate():
    assert len(reasons(euid=1000, pty_user_uid=0, private_dir_stat=st(uid=5))) == 3


@pytest.fixture
def image(monkeypatch, tmp_path):
    """Image env with a fake `claude` user; root euid; private dir in tmp_path, faked root-owned."""
    monkeypatch.setenv("FENRIR_BIND_ALL", "1")
    monkeypatch.setenv("FENRIR_PTY_USER", "claude")
    claude = pwd.struct_passwd(("claude", "x", 1000, 1000, "", "/home/claude", "/bin/sh"))
    users = {"claude": claude}
    monkeypatch.setattr(pwd, "getpwnam", lambda name: users[name])
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    real_stat = os.stat

    def fake_stat(p, **kw):
        found = real_stat(p, **kw)
        return st(found.st_mode) if p == private else found

    monkeypatch.setattr(os, "stat", fake_stat)
    return Settings(private_dir=private)


def test_shell_passes_an_isolated_image(image):
    assert isolation_problems(image) == []


def test_shell_reports_unknown_pty_user_and_non_root_server(image, monkeypatch):
    monkeypatch.setenv("FENRIR_PTY_USER", "nobody-here")
    monkeypatch.setattr(os, "geteuid", lambda: 501)
    assert len(isolation_problems(image)) == 2


def test_shell_reports_missing_private_dir(image, tmp_path):
    [reason] = isolation_problems(Settings(private_dir=tmp_path / "gone"))
    assert "does not exist" in reason


def test_shell_reports_private_dir_inside_app(image, monkeypatch, tmp_path):
    monkeypatch.setattr(isolation, "APP_ROOT", tmp_path)
    [reason] = isolation_problems(image)
    assert "inside" in reason


def test_not_enforced_natively(image, monkeypatch):
    monkeypatch.delenv("FENRIR_BIND_ALL")
    monkeypatch.setattr(os, "geteuid", lambda: 501)
    assert isolation_problems(Settings(private_dir=Path("nowhere"))) == []
