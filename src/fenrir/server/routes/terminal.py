"""`WS /ws/terminal`: the Workbench shell over a WebSocket, one PTY per connection.

Client → server text frames (`terminal.parse_message`; anything else is ignored); server →
client binary frames of raw PTY output. The socket closes 1000 when the shell exits, 1009 when
one input frame, or the input queued behind a write the shell is not reading, passes
`MAX_PENDING` bytes, and 1011 after a one-line output frame when the shell cannot start.
"""

import asyncio
import logging
import os
import subprocess
import sys

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketDisconnected
from uvicorn.protocols.utils import ClientDisconnected

from fenrir.server import ptyproc
from fenrir.server.auth import check_ws
from fenrir.server.isolation import NESTED_GIT, as_user, isolation_problems, nested_git_in_image
from fenrir.server.terminal import Input, Resize, child_env, parse_message, shell_argv

log = logging.getLogger(__name__)
router = APIRouter()
MAX_PENDING = 1 << 20  # bytes of input the shell has not taken yet

Pty = ptyproc.PosixPty | ptyproc.WindowsPty
GONE = (WebSocketDisconnect, WebSocketDisconnected, ClientDisconnected)  # client left mid-send


async def _output(ws: WebSocket, pty: Pty) -> None:
    while data := await pty.read():
        await ws.send_bytes(data)
    await ws.close()  # the shell exited


async def _input(ws: WebSocket, pty: Pty, pending: bytearray, ready: asyncio.Event) -> None:
    # never awaits the PTY, so a disconnect is seen even while the shell ignores its stdin
    while (msg := await ws.receive())["type"] != "websocket.disconnect":
        match parse_message(msg.get("text") or ""):
            case Input(data):
                pending += data.encode(errors="replace")  # a lone surrogate from a paste
                if len(pending) > MAX_PENDING:
                    await ws.close(1009, "too much input pending")
                    return
                ready.set()
            case Resize(cols, rows):
                pty.resize(cols, rows)


async def _write(pty: Pty, pending: bytearray, ready: asyncio.Event) -> None:
    while True:
        await ready.wait()
        ready.clear()
        data = bytes(pending)
        pending.clear()
        await pty.write(data)  # blocks while the shell is not reading


@router.websocket("/ws/terminal")
async def terminal_ws(ws: WebSocket) -> None:
    if not check_ws(ws):
        await ws.close()
        return
    if problems := isolation_problems(ws.app.state.settings):
        log.error("refusing to open the terminal: %s", "; ".join(problems))
        await ws.close()
        return
    user = os.environ.get("FENRIR_PTY_USER")
    try:
        overlay, drop = as_user(user)
    except KeyError:  # getpwnam: no such user
        log.error("refusing to open the terminal: FENRIR_PTY_USER %r is not a user", user)
        await ws.close()
        return
    env = child_env(os.environ, ws.app.state.session_key) | overlay
    argv = shell_argv(env, sys.platform)  # SHELL from the overlay: the PTY user's own shell
    await ws.accept()
    try:
        pty = ptyproc.spawn(argv, env, drop)
    except (OSError, subprocess.SubprocessError) as e:  # SubprocessError: preexec_fn failed
        message = f"cannot start the shell {argv[0]}: {getattr(e, 'strerror', None) or e}"
        log.error(message)
        await ws.send_bytes(f"{message}\r\n".encode(errors="replace"))
        await ws.close(1011, "shell did not start")
        return
    pending, ready = bytearray(), asyncio.Event()
    tasks = [
        asyncio.create_task(_output(ws, pty)),
        asyncio.create_task(_input(ws, pty, pending, ready)),
        asyncio.create_task(_write(pty, pending, ready)),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:  # a second cancel must not skip killing the shell
            await pty.close()
            if nested := await asyncio.to_thread(nested_git_in_image):
                log.warning(NESTED_GIT, ", ".join(nested))
        for e in results:  # a send to a gone client raises; anything else is a bug
            if isinstance(e, Exception) and not isinstance(e, GONE):
                log.error("terminal session failed", exc_info=e)
