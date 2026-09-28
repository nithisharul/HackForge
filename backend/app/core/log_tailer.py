"""Async `tail -F` for a growing log file.

* Waits for the file to exist.
* Yields complete lines only (a half-written line is buffered until its newline).
* Survives rotation (file replaced -> inode changes -> reopen from start).
* Survives truncation (file shrinks -> seek back to 0).
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import AsyncIterator

log = logging.getLogger(__name__)


class LogTailer:
    def __init__(self, path: Path | str, from_end: bool = True,
                 poll_interval: float = 0.25, max_batch: int = 5000):
        self.path = Path(path)
        self.from_end = from_end
        self.poll_interval = poll_interval
        self.max_batch = max_batch
        self._stopped = False
        self.lines_read = 0
        self.rotations = 0
        self.truncations = 0

    def stop(self) -> None:
        self._stopped = True

    async def lines(self) -> AsyncIterator[list[str]]:
        """Yield batches of new lines as they are appended."""
        fh = None
        inode = None
        buffer = b""
        first_open = True
        try:
            while not self._stopped:
                if fh is None:
                    if not self.path.exists():
                        await asyncio.sleep(self.poll_interval)
                        continue
                    fh = open(self.path, "rb")
                    inode = os.fstat(fh.fileno()).st_ino
                    if first_open and self.from_end:
                        fh.seek(0, os.SEEK_END)
                    first_open = False
                    buffer = b""
                    log.info("Tailing %s (inode %s, pos %s)", self.path, inode, fh.tell())

                chunk = fh.read(1024 * 1024)
                if chunk:
                    buffer += chunk
                    *complete, buffer = buffer.split(b"\n")
                    if complete:
                        lines = [c.decode("utf-8", errors="replace") for c in complete]
                        self.lines_read += len(lines)
                        for i in range(0, len(lines), self.max_batch):
                            yield lines[i:i + self.max_batch]
                    continue  # there may be more to read right away

                # Nothing new: check for rotation / truncation, then sleep.
                try:
                    st = os.stat(self.path)
                except FileNotFoundError:
                    st = None
                if st is None or st.st_ino != inode:
                    log.info("Log rotated; reopening %s", self.path)
                    self.rotations += 1
                    fh.close()
                    fh = None
                    continue
                if st.st_size < fh.tell():
                    log.info("Log truncated; rewinding %s", self.path)
                    self.truncations += 1
                    fh.seek(0)
                    buffer = b""
                    continue
                await asyncio.sleep(self.poll_interval)
        finally:
            if fh is not None:
                fh.close()
