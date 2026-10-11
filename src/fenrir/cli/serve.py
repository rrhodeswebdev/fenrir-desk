"""`fenrir serve`: the web app on 127.0.0.1, entered through a one-time launch link. Shell."""

import os
import secrets
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import uvicorn

from fenrir.server.app import create_app
from fenrir.server.auth import VITE_PORT
from fenrir.settings import load_settings


def load_session_key(private_dir: Path) -> str:
    """The key in `<private_dir>/session.key`, created (file 0600, dir 0700) when missing."""
    private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = private_dir / "session.key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    with os.fdopen(fd, "w") as f:
        f.write(key := secrets.token_urlsafe(32))
    return key


def exposes_secrets(static_dir: Path, private_dir: Path) -> bool:
    # static files are served without the session cookie, so nothing secret may sit under them
    root = static_dir.resolve()
    return private_dir.resolve().is_relative_to(root) or any(root.rglob(".env*"))


def _fail(message: str) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(1)


def serve(
    port: Annotated[int, typer.Option(help="Port on 127.0.0.1")] = 8000,
    dev: Annotated[bool, typer.Option(help="Also accept the Vite dev server on :5173")] = False,
    static_dir: Annotated[Path, typer.Option(help="Built web UI directory")] = Path("web/dist"),
) -> None:
    """Start the Fenrir Desk server on 127.0.0.1 and print its launch link."""
    try:
        settings = load_settings()
        if exposes_secrets(static_dir, settings.private_dir):
            _fail(f"refusing to serve {static_dir}: it holds the private dir or a .env file")
        key = load_session_key(settings.private_dir)
    except (ValueError, OSError) as e:
        _fail(str(e))
    if not key:
        _fail(f"{settings.private_dir / 'session.key'} is empty; delete it and restart")
    static = static_dir if (static_dir / "index.html").is_file() else None
    if static is None:
        typer.echo(
            f"UI not built ({static_dir / 'index.html'} missing); serving the API only. "
            "Build it with: npm --prefix web run build"
        )
    code = secrets.token_urlsafe(16)
    app = create_app(settings, key, code, static, port=port, dev=dev)
    if not os.environ.get("FENRIR_PTY_USER"):
        typer.echo(
            "Note: running natively, the Workbench terminal runs as you and is not isolated."
        )
    typer.echo(f"Open http://localhost:{VITE_PORT if dev else port}/launch?code={code}")
    uvicorn.run(app, host="127.0.0.1", port=port)
