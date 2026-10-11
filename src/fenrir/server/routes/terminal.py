"""`WS /ws/terminal`: the Workbench shell over a WebSocket, one PTY per connection.

Client → server text frames (`terminal.parse_message`; anything else is ignored); server →
client binary frames of raw PTY output. The socket closes 1000 when the shell exits, and 1011
after a one-line output frame when the shell cannot start.
"""

import asyncio
import logging
import os
import sys

from fastapi import APIRouter, WebSocket

from fenrir.server import ptyproc
from fenrir.server.auth import check_ws
from fenrir.server.isolation import drop_to, isolation_problems
from fenrir.server.terminal import Input, Resize, child_env, parse_message, shell_argv

log = logging.getLogger(__name__)
router = APIRouter()


async def _output(ws: WebSocket, pty: ptyproc.PosixPty | ptyproc.WindowsPty) -> None:
    while data := await pty.read():
        await ws.send_bytes(data)
    await ws.close()  # the shell exited


async def _input(ws: WebSocket, pty: ptyproc.PosixPty | ptyproc.WindowsPty) -> None:
    while (msg := await ws.receive())["type"] != "websocket.disconnect":
        match parse_message(msg.get("text") or ""):
            case Input(data):
                await pty.write(data.encode(errors="replace"))  # a lone surrogate from a paste
            case Resize(cols, rows):
                pty.resize(cols, rows)


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
    overlay, as_user = drop_to(user) if user else ({}, {})
    env = child_env(os.environ, ws.app.state.session_key) | overlay
    argv = shell_argv(env, sys.platform)  # SHELL from the overlay: the PTY user's own shell
    await ws.accept()
    try:
        pty = ptyproc.spawn(argv, env, as_user)
    except OSError as e:
        message = f"cannot start the shell {argv[0]}: {e.strerror or e}"
        log.error(message)
        await ws.send_bytes(f"{message}\r\n".encode(errors="replace"))
        await ws.close(1011, "shell did not start")
        return
    tasks = [asyncio.create_task(_output(ws, pty)), asyncio.create_task(_input(ws, pty))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)  # a send to a gone client raises
        await pty.close()
