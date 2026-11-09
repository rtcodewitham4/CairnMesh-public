"""Durable writes for state that must survive a power cut (FCNT reservations, layer-3 state).

2026-10-21 second review B3: the secure layer replaced its state file
without fsync, and nobody fsynced the directory, so after a power cut the
rename could be lost and an older file read back (a FCNT reservation going
back reuses nonces under the network key).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile


def write_json(path: str, data) -> None:
    """Replace ``path`` with ``data`` as JSON; on return it is on disk, the rename included.

    A temporary file of its own in the same directory (two writers never share
    one, review D2), flushed and fsynced, renamed over ``path``, then the
    directory fsynced (POSIX; elsewhere the rename is as durable as the
    platform makes it).
    """

    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    if os.name == "posix":
        handle = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)
