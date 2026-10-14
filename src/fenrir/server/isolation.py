"""DEC-1 isolation check: may the terminal and strategy children start in the image?"""

import os
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fenrir.settings import Settings

APP_ROOT = Path("/app")  # the terminal-writable repo bind mount in the image


def check(
    euid: int,
    pty_user_uid: int | None,
    pty_user_gid: int | None,
    private_dir_stat: os.stat_result | None,
    private_dir_path: Path,
    private_dir_real: Path,
    app_roots: Sequence[Path],
    env_file_path: Path | None = None,
    env_file_real: Path | None = None,
) -> list[str]:
    """Why the setup is not isolated; empty means isolated.

    `private_dir_path` is absolute as configured, `private_dir_real` its `resolve()`, and
    `private_dir_stat` its `lstat` (None = no dir). `None` uid/gid = no such user. `app_roots`
    are the terminal-writable trees (`/app`, the cwd). `env_file_path` is the absolute
    `$FENRIR_ENV_FILE` (None when unset), `env_file_real` its `resolve()`.
    """
    reasons = []
    if euid != 0:
        reasons.append(f"server is not running as root (euid {euid})")
    if pty_user_uid is None:
        reasons.append("FENRIR_PTY_USER is unset or names no existing user")
    elif pty_user_uid == 0:
        reasons.append("FENRIR_PTY_USER is root")
    elif pty_user_gid == 0:
        reasons.append("FENRIR_PTY_USER's primary group is root (gid 0)")
    if private_dir_path != private_dir_real:
        reasons.append(f"private dir path {private_dir_path} contains a symlink or ..")
    inside = [
        r for r in app_roots for p in (private_dir_path, private_dir_real) if p.is_relative_to(r)
    ]
    if inside:
        reasons.append(f"private dir {private_dir_path} is inside {inside[0]}")
    if private_dir_stat is None:
        reasons.append(f"private dir {private_dir_path} does not exist")
    elif not stat.S_ISDIR(private_dir_stat.st_mode):
        reasons.append(f"private dir {private_dir_path} is not a directory")
    else:
        if private_dir_stat.st_uid != 0:
            reasons.append(f"private dir {private_dir_path} is not owned by root")
        if (mode := stat.S_IMODE(private_dir_stat.st_mode)) != 0o700:
            reasons.append(f"private dir {private_dir_path} has mode {oct(mode)}, not 0o700")
    if env_file_path is not None:
        if env_file_path != env_file_real:
            reasons.append(f"FENRIR_ENV_FILE {env_file_path} contains a symlink or ..")
        if not env_file_path.is_relative_to(private_dir_path):
            reasons.append(f"FENRIR_ENV_FILE {env_file_path} is outside the private dir")
    return reasons


# the only read-write mounts in /app (compose.yaml): the rest of the repo is read-only
RW_DIRS = ("data", "rules", "strategies")


def nested_git(root: Path, dirs: Sequence[str] = RW_DIRS) -> list[str]:
    """Every `.git` entry, in any case, in `root/<dir>` for each of `dirs`, relative to `root` and
    sorted, plus `<path> (unreadable)` for a directory the walk could not list. The host's git
    uses the nearest `.git` above its cwd, so one the terminal plants runs its config on the host
    (#184). Never follows a symlink."""
    found = []

    def unreadable(e: OSError) -> None:
        if not isinstance(e, FileNotFoundError):  # gone mid-walk, or a dir that is not there
            found.append(f"{Path(e.filename).relative_to(root)} (unreadable)")

    for d in dirs:
        for top, subdirs, files in os.walk(root / d, onerror=unreadable):
            here = Path(top)
            # any case: a case-insensitive host (macOS) git finds .GIT as its .git
            found += [str((here / n).relative_to(root)) for n in (*subdirs, *files) if _git(n)]
            subdirs[:] = [s for s in subdirs if not _git(s)]
    return sorted(found)


def _git(name: str) -> bool:
    return name.casefold() == ".git"


NESTED_GIT = "nested .git the terminal can write (your git there would run its config): %s"


def in_image() -> bool:
    """The Docker image (`FENRIR_BIND_ALL=1`, set by the image alone), where compose's restart
    policy brings the server back after it exits."""
    return os.environ.get("FENRIR_BIND_ALL") == "1"


def nested_git_in_image() -> list[str]:
    """`nested_git(/app)` in the image; `[]` natively, where the terminal is the user anyway."""
    return nested_git(APP_ROOT) if os.environ.get("FENRIR_BIND_ALL") == "1" else []


def as_user(user: str | None) -> tuple[dict[str, str], dict[str, Any]]:
    """(env overlay, `Popen` kwargs) that run a child as `user` from its passwd entry, with none
    of the server's groups; `({}, {})` for None (the server's own user). KeyError: no such user."""
    if not user:
        return {}, {}
    import pwd  # POSIX only; unset natively on Windows

    pw = pwd.getpwnam(user)
    env = {"HOME": pw.pw_dir, "USER": user, "LOGNAME": user, "SHELL": pw.pw_shell}
    return env, {"user": pw.pw_uid, "group": pw.pw_gid, "extra_groups": []}


def isolation_problems(settings: Settings) -> list[str]:
    """`check` on this process; always empty natively (`FENRIR_BIND_ALL` unset), where the
    terminal and strategies run as the user, unisolated by design."""
    if os.environ.get("FENRIR_BIND_ALL") != "1":
        return []
    import pwd  # Unix only; the image is Linux and Windows never sets FENRIR_BIND_ALL

    try:
        user = pwd.getpwnam(os.environ.get("FENRIR_PTY_USER", ""))
    except KeyError:
        user = None
    path = settings.private_dir.absolute()  # as configured: resolve() would hide a symlink
    try:
        private_stat = os.lstat(path)
    except OSError:
        private_stat = None
    env_file = os.environ.get("FENRIR_ENV_FILE")
    env_path = Path(env_file).absolute() if env_file else None
    return check(
        os.geteuid(),
        None if user is None else user.pw_uid,
        None if user is None else user.pw_gid,
        private_stat,
        path,
        path.resolve(),
        (APP_ROOT, Path.cwd()),
        env_path,
        env_path and env_path.resolve(),
    )
