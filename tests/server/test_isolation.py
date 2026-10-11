import os
import stat
from pathlib import Path

import pytest

from fenrir.server import isolation
from fenrir.server.isolation import check, isolation_problems
from fenrir.settings import Settings

pwd = pytest.importorskip("pwd")

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
        "private_dir_real": PRIVATE,
        "app_roots": (APP, Path("/work")),
    } | kw
    return check(**args)


def test_isolated_image_has_no_problems():
    assert reasons() == []
    env = PRIVATE / ".env"
    assert reasons(env_file_path=env, env_file_real=env) == []


@pytest.mark.parametrize(
    "kw, fragment",
    [
        ({"euid": 1000}, "not running as root"),
        ({"pty_user_uid": None, "pty_user_gid": None}, "names no existing user"),
        ({"pty_user_uid": 0}, "is root"),
        ({"pty_user_gid": 0}, "primary group is root"),
        (
            {"private_dir_path": APP / "p", "private_dir_real": APP / "p"},
            "is inside /app",
        ),
        ({"private_dir_path": APP, "private_dir_real": APP}, "is inside /app"),
        (
            {"private_dir_path": Path("/work/p"), "private_dir_real": Path("/work/p")},
            "is inside /work",
        ),
        ({"private_dir_real": Path("/var/lib/other")}, "contains a symlink or .."),
        ({"private_dir_stat": None}, "does not exist"),
        ({"private_dir_stat": st(mode=stat.S_IFREG | 0o700)}, "not a directory"),
        ({"private_dir_stat": st(uid=1000)}, "not owned by root"),
        ({"private_dir_stat": st(mode=stat.S_IFDIR | 0o750)}, "mode 0o750"),
        (
            {"env_file_path": PRIVATE / ".env", "env_file_real": Path("/app/.env")},
            "FENRIR_ENV_FILE /var/lib/fenrir/.env contains a symlink",
        ),
        (
            {"env_file_path": Path("/etc/env"), "env_file_real": Path("/etc/env")},
            "outside the private dir",
        ),
    ],
)
def test_each_reason(kw, fragment):
    [reason] = reasons(**kw)
    assert fragment in reason


def test_private_dir_symlinked_into_app_is_inside_app():
    out = reasons(private_dir_path=PRIVATE, private_dir_real=APP / "priv")
    assert any("symlink" in r for r in out)
    assert any("inside /app" in r for r in out)


def test_reasons_accumulate():
    assert len(reasons(euid=1000, pty_user_uid=0, private_dir_stat=st(uid=5))) == 3


@pytest.fixture
def image(monkeypatch, tmp_path):
    """Image env with a fake `claude` user; root euid; private dir in tmp_path, faked root-owned."""
    monkeypatch.setenv("FENRIR_BIND_ALL", "1")
    monkeypatch.setenv("FENRIR_PTY_USER", "claude")
    monkeypatch.delenv("FENRIR_ENV_FILE", raising=False)
    claude = pwd.struct_passwd(("claude", "x", 1000, 1000, "", "/home/claude", "/bin/sh"))
    users = {"claude": claude}
    monkeypatch.setattr(pwd, "getpwnam", lambda name: users[name])
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    real_lstat = os.lstat

    def fake_lstat(p, **kw):
        found = real_lstat(p, **kw)
        return st(found.st_mode) if Path(p) == private else found

    monkeypatch.setattr(os, "lstat", fake_lstat)
    return Settings(private_dir=private)


def test_shell_passes_an_isolated_image(image, monkeypatch):
    assert isolation_problems(image) == []
    monkeypatch.setenv("FENRIR_ENV_FILE", str(image.private_dir / ".env"))
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


def test_shell_reports_private_dir_inside_cwd(image, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    [reason] = isolation_problems(image)
    assert f"inside {tmp_path}" in reason


def test_shell_reports_symlinked_private_dir(image, monkeypatch, tmp_path):
    # the review's PoC: app/data/private -> a root-owned 0700 dir outside the app
    app = tmp_path / "app"
    (app / "data").mkdir(parents=True)
    (app / "data" / "private").symlink_to(image.private_dir)
    monkeypatch.setattr(isolation, "APP_ROOT", app)
    out = isolation_problems(Settings(private_dir=app / "data" / "private"))
    assert any("contains a symlink" in r for r in out)
    assert any(f"inside {app}" in r for r in out)
    assert any("not a directory" in r for r in out)  # lstat sees the link itself


def test_shell_reports_symlink_out_of_the_app(image, monkeypatch, tmp_path):
    # a link outside the app whose target is inside it
    app = tmp_path / "app"
    (app / "priv").mkdir(parents=True, mode=0o700)
    (tmp_path / "link").symlink_to(app / "priv")
    monkeypatch.setattr(isolation, "APP_ROOT", app)
    out = isolation_problems(Settings(private_dir=tmp_path / "link"))
    assert any(f"inside {app}" in r for r in out)


def test_shell_reports_env_file_outside_or_through_a_symlink(image, monkeypatch, tmp_path):
    monkeypatch.setenv("FENRIR_ENV_FILE", str(tmp_path / ".env"))
    [reason] = isolation_problems(image)
    assert "outside the private dir" in reason
    (image.private_dir / "link.env").symlink_to(tmp_path / ".env")
    monkeypatch.setenv("FENRIR_ENV_FILE", str(image.private_dir / "link.env"))
    [reason] = isolation_problems(image)
    assert "contains a symlink" in reason
    monkeypatch.setenv("FENRIR_ENV_FILE", str(image.private_dir / ".." / "x.env"))
    assert any("symlink or .." in r for r in isolation_problems(image))


def test_not_enforced_natively(image, monkeypatch):
    monkeypatch.delenv("FENRIR_BIND_ALL")
    monkeypatch.setattr(os, "geteuid", lambda: 501)
    assert isolation_problems(Settings(private_dir=Path("nowhere"))) == []
