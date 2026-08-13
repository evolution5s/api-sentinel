# PERMISSION_REQUESTS_reviewed_2026-08-13.md

Archive of `PERMISSION_REQUESTS.md` entries Jan has already reviewed and
resolved - moved out of the live file so that one only ever shows what's
still actually pending. See `CLAUDE.md`'s "Berechtigungsanfragen" section
for the workflow this is part of.

---

[2026-08-13] `Bash(railway restart:*)` / `Bash(railway redeploy:*)` - **resolved: left unlisted, per-use confirmation (final)**

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

**Resolution, step 1 (2026-08-13, Jan):** close the gap immediately,
don't wait for a batch review - `Bash(railway restart:*)` and
`Bash(railway redeploy:*)` added to `.claude/settings.json`'s `deny` list
alongside their `railway service` equivalents. Both spellings of the same
action denied consistently.

**Resolution, step 2 (2026-08-13, Jan, same day):** that overshot the
actual intent - a `deny` rule can't be bypassed by asking at all, which
made the manual trigger completely unusable rather than just gated behind
a real confirmation each time (the originally intended behavior). Final
state: all four spellings (`railway restart`, `railway redeploy`,
`railway service restart`, `railway service redeploy`) removed from
`deny` again and left deliberately unlisted - same category as `railway
ssh`: neither blanket-allowed nor blanket-denied, a genuine per-use
approval prompt every time. This preserves the intended confirmation
requirement without blocking the action outright.
