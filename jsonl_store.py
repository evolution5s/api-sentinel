"""Tiny shared JSONL read/write/append primitives.

Used by both tools.py (a subsidiary's own Sub-CEO/operative state) and
holding.py (Main-CEO/holding-level state) so each layer can have its own
directory without duplicating this logic.
"""
import json
import os
import threading
import uuid
from pathlib import Path

_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def locked(directory: Path, filename: str) -> threading.RLock:
    """Lock serializing a read-modify-write critical section for one JSONL file.

    Root cause of the live-observed backlog-candidate data loss
    (2026-08-14): crewai's agent executor runs multiple tool calls
    requested in the same LLM turn CONCURRENTLY on a thread pool
    (crew_agent_executor.py's ThreadPoolExecutor, up to 8 workers). Any
    function that does read_jsonl() -> mutate the list -> write_jsonl()
    is racy under that: two threads both snapshot the same
    pre-mutation list, each appends its own record, and whichever
    thread's write_jsonl() call finishes last wins - silently discarding
    every other thread's change, even though each call itself returns
    success. A lock inside write_jsonl() alone can't fix this because
    the race starts at the read, not the write. Callers must hold this
    lock across their entire read + mutate + write span. One lock per
    (directory, filename) - real concurrent writes to *different* files
    are unaffected.

    Reentrant (RLock, not Lock) on purpose: a locked span can legitimately
    call a helper that acquires the same (directory, filename) lock again
    on the same thread (e.g. a bootstrap-on-empty helper) - that must not
    deadlock the thread against itself.
    """
    key = str((directory / filename).resolve())
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


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
    # Belt-and-suspenders durability/atomicity: write to a uniquely-named
    # temp file (pid + thread id + a random suffix - concurrent callers,
    # e.g. from locked() being held by a different (directory, filename)
    # key, or a caller that doesn't go through locked() at all, must never
    # share a temp path or one os.replace() can find the other's temp file
    # already gone), fsync it, then atomically rename over the target. This
    # means a read never observes a partial file, and a completed write()
    # call means the bytes are durable even if the container is torn down
    # right after. This alone does NOT fix the lost-update race between
    # concurrent read-modify-write callers - see locked() above, which
    # callers must hold across their read + mutate + write span.
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    tmp = directory / f".{filename}.tmp{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}"
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
