"""The NUCLEO dual-radio bench as a test fixture.

``Board`` flashes one of the firmware's bench programs (``make flash
CMD=...``), resets the board while capturing its serial output, and parses
the output lines (``X key= value ...``, the format every bench program
prints) into ``Line`` records. ``bridge()`` opens the gateway firmware's
bridge (``cairnmesh.bridge``) for tests that drive it.

The bench needs the ST-LINK serial port and STM32CubeProgrammer; ``missing()``
says what is not there, so the tests skip on a machine without the board.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "firmware"
PORT = os.environ.get("CAIRNMESH_BOARD_PORT", "/dev/ttyACM0")
PROGRAMMER = Path(
    os.environ.get(
        "CAIRNMESH_PROGRAMMER", str(Path.home() / "apps/STM32CubeProrammer/bin/STM32_Programmer_CLI")
    )
)

_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=$")


@dataclass
class Line:
    """One output line: its tag (first word), the words before the first key, and key= values."""

    tag: str
    head: list[str]
    fields: dict[str, list[str]] = field(default_factory=dict)
    text: str = ""

    def int(self, key: str, index: int = 0) -> int:
        return int(self.fields[key][index])

    def get(self, key: str, default: str = "") -> str:
        values = self.fields.get(key)
        return values[0] if values else default


def parse(text: str) -> list[Line]:
    lines = []
    for raw in text.splitlines():
        words = raw.split()
        if not words:
            continue
        line = Line(words[0], [], text=raw)
        key: Optional[str] = None
        for word in words[1:]:
            m = _KEY.match(word)
            if m:
                # A key may come twice (radiotest's "ok= 50 ... ok= true"):
                # the first keeps the name, the next ones get _2, _3...
                key, n = m.group(1), 2
                while key in line.fields:
                    key, n = f"{m.group(1)}_{n}", n + 1
                line.fields[key] = []
            elif key is None:
                line.head.append(word)
            else:
                line.fields[key].append(word)
        lines.append(line)
    return lines


class Board:
    def __init__(self, port: str = PORT) -> None:
        self.port = port

    @staticmethod
    def missing() -> Optional[str]:
        if not os.path.exists(PORT):
            return f"no board serial port {PORT}"
        if not PROGRAMMER.exists():
            return f"no STM32CubeProgrammer at {PROGRAMMER}"
        if shutil.which("tinygo") is None:
            return "no tinygo"
        return None

    def flash(self, cmd: str, ldflags: str = "", make_vars: Optional[dict[str, str]] = None) -> None:
        args = ["make", "flash", f"CMD={cmd}"]
        if ldflags:
            args.append(f"LDFLAGS={ldflags}")
        for name, value in (make_vars or {}).items():
            args.append(f"{name}={value}")
        done = subprocess.run(args, cwd=FIRMWARE, capture_output=True, text=True, timeout=300)
        if done.returncode != 0:
            raise RuntimeError(f"flash {cmd} failed:\n{done.stdout[-2000:]}\n{done.stderr[-2000:]}")

    def reset(self) -> None:
        subprocess.run([str(PROGRAMMER), "-c", "port=SWD", "-rst"], capture_output=True, timeout=60)

    def run(self, cmd: str, seconds: float, ldflags: str = "", log: Optional[Path] = None) -> list[Line]:
        """Flash ``cmd``, reset, capture up to ``seconds`` (the capture ends at ``R done``)."""

        self.flash(cmd, ldflags)
        if log is None:
            log = Path(tempfile.mkstemp(suffix=".log")[1])
        capture = subprocess.Popen(
            [sys.executable, str(FIRMWARE / "tools" / "loadreport.py"), "capture", self.port, str(log), str(seconds)],
            cwd=FIRMWARE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(1.0)  # the port open before the reset
            self.reset()
            capture.wait(timeout=seconds + 30)
        finally:
            if capture.poll() is None:
                capture.kill()
        return parse(log.read_text(errors="replace"))

    def bridge(self):
        """The gateway firmware's bridge (flash ``gateway`` first): ``(GatewayBridge, transport)``."""

        sys.path.insert(0, str(FIRMWARE / "tools"))
        from serial_bench import Port

        from cairnmesh.bridge import GatewayBridge
        from cairnmesh.serial_link import SerialTransport

        transport = SerialTransport(Port(self.port))
        bridge = GatewayBridge(transport)
        transport.start()
        return bridge, transport


def done(lines: list[Line]) -> Line:
    """The ``R done`` line; AssertionError when the program did not finish."""

    for line in lines:
        if line.tag == "R" and line.head[:1] == ["done"]:
            return line
    raise AssertionError("no 'R done' line:\n" + "\n".join(line.text for line in lines[-20:]))
