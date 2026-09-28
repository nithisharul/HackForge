#!/usr/bin/env python3
"""Replay an existing log file (e.g. BGL or test_labeled.log) into the watched
file at N x real speed, preserving the original gaps between lines.

    python scripts/replay_logs.py data/test_labeled.log --speed 10
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.parsers import get_parser  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("source", type=Path)
    p.add_argument("--out", type=Path, default=ROOT / "data" / "app.log")
    p.add_argument("--speed", type=float, default=10.0)
    p.add_argument("--format", default="generic")
    a = p.parse_args()
    parser = get_parser(a.format)
    start_wall = time.time()
    start_log = None
    buf: list[str] = []
    with open(a.source, encoding="utf-8", errors="replace") as src, open(a.out, "a") as out:
        for n, line in enumerate(src, 1):
            ev = parser.parse(line)
            if ev is None:
                continue
            t = ev.timestamp.timestamp()
            start_log = start_log or t
            due = start_wall + (t - start_log) / a.speed
            if due > time.time():
                if buf:
                    out.write("".join(buf)); out.flush(); buf.clear()
                time.sleep(due - time.time())
            buf.append(line if line.endswith("\n") else line + "\n")
            if n % 5000 == 0:
                print(f"\r{n:,} lines replayed", end="", flush=True)
        out.write("".join(buf))
    print("\ndone")


if __name__ == "__main__":
    main()
