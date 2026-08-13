"""CLI for the Aufsichtsrat (board) to review and decide on entries in
approval_queue.jsonl - the single human sign-off point for anything this
system's agents are never allowed to just go do on their own (see
README chapter 8.1 for the full request_approval/category discipline this
queue is part of).

Commands:
    python approve.py
        List every currently pending request: id, category, the proposal
        itself, the agent's reasoning, and when it was filed. Nothing is
        decided by running this - it's read-only.
    python approve.py approve appr_ab12cd34
        Mark a request approved. For category='spend', this does NOT set up
        a payment link itself - reply on Telegram with
        "payment_link: <id> <url>" separately once a real one exists.
    python approve.py reject appr_ab12cd34 <reason>
        Mark a request rejected - reason is now REQUIRED to actually take
        effect (rejection-reasoning addendum), not just recommended. A
        reject with no reason does not close the request: it stays
        status='pending', flagged needs_rejection_reason=true, and keeps
        surfacing in the Telegram report's "Fuer den Aufsichtsrat" section
        as an open question until a real reason is given (reply again with
        the same id and a reason to actually close it). This exists
        because a silent, reason-less rejection was exactly the raw
        material this system was losing - the "why not" a future
        self-improvement pass could learn from.

What to weigh before approving, by category:
    spend    - real money or a payment-intent test going out. Confirm the
               amount/price point is genuinely what was asked for, not a
               rounder/higher number slipped in.
    legal    - creates a legal obligation (e.g. ToS, a contract term).
               Read the actual text, not just the reasoning summary.
    publish  - becomes publicly visible under this project's name. The
               proposal is a rigid template (platform/target_url/title/text/
               footer/hypothesis_id/evidence_stage/is_experiment/
               success_criterion) rendered verbatim - read the real `text`
               field itself, it's exactly what gets posted, not a
               paraphrase.
    deploy   - a real infrastructure/code change going live.
    pricing  - a price point being committed to (e.g. shown to real users),
               not just floated as a planning guess.

Telegram vs. this CLI: the same approval_queue.jsonl is the one source of
truth either way. Telegram is the fast path for a single reply ("approve"/
"reject" on the notification message, or "<id> approve"/"<id> reject" typed
directly - see process_telegram_commands in tools.py). This CLI is for
reviewing the whole queue at once, working without Telegram open, or
attaching a longer rejection reason than a quick reply invites. Both write
to the exact same file; there is no second, separate approval mechanism to
keep in sync.
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
    """Apply an approve/reject decision. status='approved' always takes
    effect immediately, no reason needed.

    status='rejected' WITHOUT a real, non-empty reason does NOT close the
    request (rejection-reasoning addendum) - a reject with no reason is
    exactly the feedback this system loses today: what NOT to propose
    again. Instead the request stays status='pending' with
    needs_rejection_reason=True, so it keeps surfacing as an open question
    (see crew.py's _aufsichtsrat_lines) rather than silently vanishing as a
    closed rejection. Reply again with a real reason (same request_id) to
    actually close it - at that point status='rejected' is set for real,
    decision_reason is recorded, and needs_rejection_reason clears.
    """
    for r in records:
        if r.get("id") == request_id:
            if r.get("status") != "pending":
                print(f"{request_id} is already '{r.get('status')}', not touching it.")
                return records
            if status == "rejected" and not (reason or "").strip():
                r["needs_rejection_reason"] = True
                print(
                    f"{request_id}: reject has no reason - left status='pending' rather than closing it. "
                    "Reply again with a real reason to actually reject it."
                )
                return records
            r["status"] = status
            r["decided_at"] = datetime.now(timezone.utc).isoformat()
            if reason:
                r["decision_reason"] = reason
            r["needs_rejection_reason"] = False
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
