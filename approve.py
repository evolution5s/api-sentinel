"""CLI for the board to review and decide on entries in approval_queue.jsonl.

Usage:
    python approve.py                       # list pending requests
    python approve.py approve appr_ab12cd34
    python approve.py reject appr_ab12cd34 [reason]
"""
import json
import sys
from datetime import datetime, timezone

from tools import STATE_DIR

QUEUE_FILE = STATE_DIR / "approval_queue.jsonl"


def _load():
    if not QUEUE_FILE.exists():
        return []
    with QUEUE_FILE.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _save(records):
    with QUEUE_FILE.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def list_pending(records):
    pending = [r for r in records if r.get("status") == "pending"]
    if not pending:
        print("No pending approval requests.")
        return
    for r in pending:
        print(f"[{r['id']}] ({r['category']}) {r['proposal']}")
        print(f"  reasoning: {r['reasoning']}")
        print(f"  filed: {r['created_at']}")
        print()


def decide(records, request_id, status, reason=None):
    for r in records:
        if r.get("id") == request_id:
            if r.get("status") != "pending":
                print(f"{request_id} is already '{r.get('status')}', not touching it.")
                return records
            r["status"] = status
            r["decided_at"] = datetime.now(timezone.utc).isoformat()
            if reason:
                r["decision_reason"] = reason
            print(f"{request_id} marked {status}.")
            return records
    print(f"No request with id {request_id} found.")
    return records


def main():
    records = _load()
    if len(sys.argv) == 1:
        list_pending(records)
        return

    action = sys.argv[1]
    if action not in ("approve", "reject"):
        print("Usage: python approve.py [approve|reject] <id> [reason]")
        sys.exit(1)
    if len(sys.argv) < 3:
        print("Missing request id.")
        sys.exit(1)

    request_id = sys.argv[2]
    reason = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else None
    status = "approved" if action == "approve" else "rejected"
    records = decide(records, request_id, status, reason)
    _save(records)


if __name__ == "__main__":
    main()
