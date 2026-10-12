"""Reading files the terminal can write, as the root server (DEC-1). Shell."""

import errno
import os
import stat
from pathlib import Path


def read_regular(path: Path, limit: int) -> bytes:
    """`path`'s bytes. Never follows a symlink at `path`, never blocks on a FIFO or device,
    never reads past `limit`. FileNotFoundError when missing; ValueError naming the file
    (never its contents) when it is a symlink, not a regular file or over `limit`."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise ValueError(f"{path} is a symlink; not reading it") from None
        if e.errno == errno.ENOENT:
            raise
        raise ValueError(f"{path} is not a regular file") from None  # a socket: ENXIO
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise ValueError(f"{path} is not a regular file")
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path} is over {limit} bytes")
    return data
