"""Tiny shared JSONL read/write/append primitives.

Used by both tools.py (a subsidiary's own Sub-CEO/operative state) and
holding.py (Main-CEO/holding-level state) so each layer can have its own
directory without duplicating this logic.
"""
import json
import os
from pathlib import Path


def read_jsonl(directory: Path, filename: str) -> list:
    path = directory / filename
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def write_jsonl(directory: Path, filename: str, records: list) -> None:
    # Live-discovered bug (2026-08-14): under a rapid burst of full-file
    # rewrites in a single cycle (e.g. 10+ backlog candidates created back
    # to back), writes that landed right before the container exited were
    # missing from the very next read_backlog() call, despite each write
    # itself returning {"ok": true}. plain open("w") only flushes into the
    # OS page cache, not to the underlying (network-backed Railway) volume
    # - a container teardown right after can lose data that was never
    # fsync'd. Write to a temp file, fsync it, then atomically rename over
    # the target so a read never observes a partial file and a completed
    # write() call means the bytes are actually durable.
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    tmp = directory / f".{filename}.tmp{os.getpid()}"
    with tmp.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


def append_jsonl(directory: Path, filename: str, record: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / filename).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
