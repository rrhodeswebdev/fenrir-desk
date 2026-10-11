"""`fenrir serve`: the web app on loopback, entered through a one-time launch link. Shell."""

import errno
import os
import secrets
import socket
import stat
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import uvicorn

from fenrir.server.app import create_app
from fenrir.server.auth import VITE_PORT
from fenrir.server.isolation import isolation_problems
from fenrir.settings import load_settings

MIN_KEY_LEN = 32


def _read_session_key(path: Path) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise ValueError(f"{path} is a symlink; delete it and restart") from None
        raise
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
            raise ValueError(f"{path} must be a file only you can read: chmod 600 it or delete it")
        if st.st_uid != os.geteuid():  # a key planted by another user is known to them
            raise ValueError(f"{path} is owned by another user; delete it and restart")
        key = f.read().strip()
    if len(key) < MIN_KEY_LEN:
        raise ValueError(
            f"{path} holds a key under {MIN_KEY_LEN} characters; delete it and restart"
        )
    return key


def load_session_key(private_dir: Path) -> str:
    """The key in `<private_dir>/session.key`, created (file 0600, dir 0700) when missing."""
    private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = private_dir / "session.key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_session_key(path)
    with os.fdopen(fd, "w") as f:
        f.write(key := secrets.token_urlsafe(32))
    return key


def exposes_secrets(static_dir: Path, private_dir: Path) -> bool:
    # static files are served without the session cookie, so nothing secret may sit under them
    root = static_dir.resolve()
    return private_dir.resolve().is_relative_to(root) or any(root.rglob(".env*"))


def bind_loopback(port: int) -> list[socket.socket]:
    # localhost resolves to ::1 first on macOS: leave it unbound and any local process can
    # take [::1]:port and catch the launch link
    v4 = socket.create_server(("127.0.0.1", port))
    try:
        v6 = socket.create_server(("::1", port), family=socket.AF_INET6)
    except OSError as e:
        if e.errno in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT):
            typer.echo(
                "warning: no IPv6 loopback on this host; listening on 127.0.0.1 only", err=True
            )
            return [v4]
        v4.close()
        raise
    return [v4, v6]


def _fail(message: str) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(1)


def serve(
    port: Annotated[int, typer.Option(help="Port on 127.0.0.1 and ::1")] = 8000,
    dev: Annotated[bool, typer.Option(help="Also accept the Vite dev server on :5173")] = False,
    static_dir: Annotated[Path, typer.Option(help="Built web UI directory")] = Path("web/dist"),
) -> None:
    """Start the Fenrir Desk server on the loopback addresses and print its launch link."""
    try:
        settings = load_settings()
        if exposes_secrets(static_dir, settings.private_dir):
            _fail(f"refusing to serve {static_dir}: it holds the private dir or a .env file")
        key = load_session_key(settings.private_dir)
    except (ValueError, OSError) as e:
        _fail(str(e))
    for problem in isolation_problems(settings):  # always empty natively
        typer.echo(f"warning: not isolated, terminal and strategies disabled: {problem}", err=True)
    static = static_dir if (static_dir / "index.html").is_file() else None
    if static is None:
        typer.echo(
            f"UI not built ({static_dir / 'index.html'} missing); serving the API only. "
            "Build it with: npm --prefix web run build"
        )
    try:
        sockets = bind_loopback(port)  # before the link is printed
    except OSError as e:
        _fail(f"cannot listen on port {port}: {e}")
    code = secrets.token_urlsafe(16)
    app = create_app(settings, key, code, static, port=port, dev=dev)
    if not os.environ.get("FENRIR_PTY_USER"):
        typer.echo(
            "Note: running natively, the Workbench terminal runs as you and is not isolated."
        )
    typer.echo(f"Open http://localhost:{VITE_PORT if dev else port}/launch?code={code}")
    # an open /ws/jobs socket would otherwise hold off the lifespan (and the job child's kill)
    config = uvicorn.Config(app, port=port, timeout_graceful_shutdown=5)
    uvicorn.Server(config).run(sockets=sockets)
