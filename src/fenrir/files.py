"""Reading files the terminal can write, as the root server (DEC-1). Shell."""

import errno
import os
import stat
from pathlib import Path


def read_regular(path: Path, limit: int) -> bytes:
    """`path`'s bytes. Never follows a symlink at `path`, never blocks on a FIFO or device,
    never reads past `limit`. FileNotFoundError when missing, PermissionError when denied;
    ValueError naming the file (never its contents) when it is a symlink, not a regular file
    or over `limit`."""
    # Windows has neither flag (and no FIFOs or terminal user to plant a symlink)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise ValueError(f"{path} is a symlink; not reading it") from None
        if e.errno in (errno.ENOENT, errno.EACCES, errno.EPERM):
            raise
        raise ValueError(f"{path} is not a regular file") from None  # a socket: ENXIO
    # the raw fd, closed exactly once here: no file object that could also close it
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{path} is not a regular file")
        chunks, size = [], 0
        while size <= limit and (chunk := os.read(fd, limit + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > limit:
        raise ValueError(f"{path} is over {limit} bytes")
    return b"".join(chunks)
