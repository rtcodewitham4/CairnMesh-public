"""Process pools for the slow benches: every core by default (user 2026-10-21: speed first).

``CAIRNMESH_WORKERS`` sets the count (``0`` or unset: every core).
How many processes run changes nothing in the results: every seed is its own
simulation with its own random state.
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor


def workers() -> int:
    wanted = int(os.environ.get("CAIRNMESH_WORKERS", "0") or 0)
    return wanted if wanted > 0 else (os.cpu_count() or 1)


def pool(**options) -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=workers(), **options)
