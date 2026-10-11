"""DEC-1 isolation check: may the terminal and strategy children start in the image?"""

import os
import stat
from pathlib import Path

from fenrir.settings import Settings

APP_ROOT = Path("/app")  # the terminal-writable repo bind mount in the image


def check(
    euid: int,
    pty_user_uid: int | None,
    pty_user_gid: int | None,
    private_dir_stat: os.stat_result | None,
    private_dir_path: Path,
    app_root: Path,
) -> list[str]:
    """Why the setup is not isolated; empty means isolated. `None` = no such user / no dir."""
    reasons = []
    if euid != 0:
        reasons.append(f"server is not running as root (euid {euid})")
    if pty_user_uid is None:
        reasons.append("FENRIR_PTY_USER is unset or names no existing user")
    elif pty_user_uid == 0:
        reasons.append("FENRIR_PTY_USER is root")
    elif pty_user_gid == 0:
        reasons.append("FENRIR_PTY_USER's primary group is root (gid 0)")
    if private_dir_path.is_relative_to(app_root):
        reasons.append(f"private dir {private_dir_path} is inside {app_root}")
    if private_dir_stat is None:
        reasons.append(f"private dir {private_dir_path} does not exist")
    elif not stat.S_ISDIR(private_dir_stat.st_mode):
        reasons.append(f"private dir {private_dir_path} is not a directory")
    else:
        if private_dir_stat.st_uid != 0:
            reasons.append(f"private dir {private_dir_path} is not owned by root")
        if (mode := stat.S_IMODE(private_dir_stat.st_mode)) != 0o700:
            reasons.append(f"private dir {private_dir_path} has mode {oct(mode)}, not 0o700")
    return reasons


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
    path = settings.private_dir.resolve()
    try:
        private_stat = os.stat(path)
    except OSError:
        private_stat = None
    return check(
        os.geteuid(),
        None if user is None else user.pw_uid,
        None if user is None else user.pw_gid,
        private_stat,
        path,
        APP_ROOT,
    )
