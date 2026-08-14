"""Adaptive check-cadence utility (self-development addendum, Part D).

For a named, LLM-costly check that's often a no-op (Kaizen assembly is the
first real user - confirmed empty on every attempt so far), tracks how many
of its recent attempts actually produced something meaningful versus found
nothing, and self-tunes an interval (in cycles) accordingly: a low hit rate
stretches the interval (check less often); a stretched-interval check that
DOES produce something meaningful tightens straight back to every cycle,
rather than waiting out the rest of its old interval.

Deliberately simple - a small persisted JSON state per check name, not a
new heavy subsystem, mirroring jsonl_store.py's "tiny shared primitive"
shape. Callers supply their own state_dir (subsidiary-scoped or
holding-scoped, whichever fits the check) rather than this module owning a
STATE_DIR of its own.
"""
import json
from pathlib import Path

MIN_INTERVAL = 1
MAX_INTERVAL = 8
STRETCH_AFTER_CONSECUTIVE_MISSES = 3
_STATE_FILENAME = "adaptive_checks.json"


def _read_all(state_dir: Path) -> dict:
    path = state_dir / _STATE_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_all(state_dir: Path, data: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / _STATE_FILENAME).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def is_check_due(state_dir: Path, check_name: str) -> bool:
    """Whether `check_name` should run THIS cycle, given its adaptive
    interval. A brand-new check (never recorded) is always due - never
    silently skips something that hasn't had a chance to prove its own
    yield yet.
    """
    entry = _read_all(state_dir).get(check_name)
    if entry is None:
        return True
    return entry.get("cycles_since_last_run", 0) + 1 >= entry.get("interval", MIN_INTERVAL)


def record_check_outcome(state_dir: Path, check_name: str, produced_something: bool) -> dict:
    """Call after actually running `check_name` this cycle (only when
    is_check_due said yes) - updates and persists its adaptive interval.
    Returns the new state entry.
    """
    all_state = _read_all(state_dir)
    entry = all_state.get(check_name) or {"interval": MIN_INTERVAL, "cycles_since_last_run": 0, "recent_outcomes": []}
    entry["cycles_since_last_run"] = 0
    recent = (entry.get("recent_outcomes") or [])[-(STRETCH_AFTER_CONSECUTIVE_MISSES - 1):]
    recent.append(bool(produced_something))
    entry["recent_outcomes"] = recent
    if produced_something:
        # Tighten back immediately - a stretched check that DID produce
        # something meaningful shouldn't keep waiting out its old interval.
        entry["interval"] = MIN_INTERVAL
    elif len(recent) >= STRETCH_AFTER_CONSECUTIVE_MISSES and not any(recent):
        entry["interval"] = min(entry.get("interval", MIN_INTERVAL) * 2, MAX_INTERVAL)
    all_state[check_name] = entry
    _write_all(state_dir, all_state)
    return entry


def note_cycle_passed_without_running(state_dir: Path, check_name: str) -> None:
    """Call once per cycle for a check that was SKIPPED this cycle
    (is_check_due returned False) - advances its cycles_since_last_run
    counter so it eventually becomes due again. A no-op for a check with no
    recorded state yet, since is_check_due already treats that as
    always-due (nothing to advance).
    """
    all_state = _read_all(state_dir)
    entry = all_state.get(check_name)
    if entry is None:
        return
    entry["cycles_since_last_run"] = entry.get("cycles_since_last_run", 0) + 1
    all_state[check_name] = entry
    _write_all(state_dir, all_state)
