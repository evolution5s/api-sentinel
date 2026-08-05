"""Tiny shared JSONL read/write/append primitives.

Used by both tools.py (a subsidiary's own Sub-CEO/operative state) and
holding.py (Main-CEO/holding-level state) so each layer can have its own
directory without duplicating this logic.
"""
import json
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
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / filename).open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_jsonl(directory: Path, filename: str, record: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / filename).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
