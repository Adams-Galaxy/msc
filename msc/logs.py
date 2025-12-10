from __future__ import annotations

import time
from collections import deque
from pathlib import Path

from .config import MscConfig


class LogError(RuntimeError):
    pass


def tail_logs(cfg: MscConfig, lines: int = 50, follow: bool = False) -> None:
    log_path: Path = cfg.log_file
    if not log_path.exists():
        raise LogError(f"Log file not found: {log_path}")

    with log_path.open("r", encoding="utf-8", errors="ignore") as handle:
        buffer = deque(handle, maxlen=lines)
        for entry in buffer:
            print(entry, end="")

        if not follow:
            return

        while True:
            position = handle.tell()
            line = handle.readline()
            if not line:
                time.sleep(0.5)
                handle.seek(position)
                continue
            print(line, end="")
