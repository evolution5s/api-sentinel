# PERMISSION_REQUESTS_reviewed_2026-08-13.md

Archive of `PERMISSION_REQUESTS.md` entries Jan has already reviewed and
resolved - moved out of the live file so that one only ever shows what's
still actually pending. See `CLAUDE.md`'s "Berechtigungsanfragen" section
for the workflow this is part of.

---

[2026-08-13] `Bash(railway restart:*)` / `Bash(railway redeploy:*)` - **resolved: added to `deny`**

Context: investigating whether Railway offers a manual "run this cron job
now" trigger, as a workaround for volume access between scheduled ticks.
Confirmed: `railway restart`/`railway redeploy` (top-level CLI commands)
do trigger an immediate, real, out-of-schedule `python crew.py` cycle -
real Anthropic API cost, real side effects (Telegram messages, possible
approval-queue writes).
Decision this time: approved for investigation/documentation purposes
only - never actually invoked to trigger a real cycle this session.
Finding: `.claude/settings.json`'s existing `deny` list blocked `Bash(railway
service restart:*)`/`Bash(railway service redeploy:*)` but not the
top-level `railway restart`/`railway redeploy` aliases - functionally the
same destructive/cost-triggering action, different literal string. This
session's `settings.local.json` showed the top-level form had in fact
been separately approved once already, confirming the gap was real, not
theoretical.
**Resolution (2026-08-13, Jan):** close the gap immediately, don't wait
for a batch review - `Bash(railway restart:*)` and `Bash(railway
redeploy:*)` added to `.claude/settings.json`'s `deny` list alongside
their `railway service` equivalents. Both spellings of the same action are
now denied consistently.
