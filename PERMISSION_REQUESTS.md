# PERMISSION_REQUESTS.md

Append-only, human-readable log of permission prompts hit during a Claude
Code session that weren't already covered by `.claude/settings.json`'s
`allow`/`deny` rules - same lean-live-file pattern as `FIX.md`. Nothing
here is applied automatically; Jan reviews periodically (often outside
Claude Code entirely) and decides which entries become permanent rules.
When that happens: add the confirmed rule to `.claude/settings.json`, then
move the entry into `PERMISSION_REQUESTS_reviewed_<date>.md`, so this live
file only ever shows what's still actually pending. See `CLAUDE.md`'s
"Berechtigungsanfragen" section for the full workflow this file is part
of.

Entry format:

```
[<date>] <exact command/tool pattern>

Context: <one line on what task this came up during and why>
Decision this time: <approved / denied>
Suggested rule: <a specific, narrow pattern - not a blanket wildcard>
```

Repeats of an already-logged, still-pending pattern get a `(seen N times,
last: <date>)` note appended to the existing entry instead of a new one.

---

[2026-08-13] `Bash(railway ssh:*)`

Context: investigating whether real live volume access (`/data/_holding/FIX.md`,
`approval_queue.jsonl`) is possible between cron ticks - CLAUDE.md's
existing `railway run -- cat` instruction turned out to be local-only, not
remote.
Decision this time: approved (multiple times this session).
Suggested rule: `Bash(railway ssh:*)` - read-only reconnaissance
(confirms container up/down, then reads files inside it), no cost beyond
the CLI call itself, no destructive potential. Reasonable standing allow.
(seen 3+ times this session)

---

[2026-08-13] `Bash(railway service files:*)` / `Bash(railway service *)`

Context: same volume-access investigation - tried as a hoped-for
alternative to `railway ssh` that might read the volume's persistent
storage directly rather than needing a live container (confirmed
empirically that it doesn't - same live-container requirement, just a
less legible error).
Decision this time: approved, scoped broadly as `railway service *` by
the harness (not just `files`).
Suggested rule: narrower than what was actually granted -
`Bash(railway service files:*)` specifically (list/download/browse are
read-only). Do **not** promote the broader `railway service *` - that
namespace also includes `railway service delete`/`railway service
restart`/`railway service redeploy`, which `.claude/settings.json`
already explicitly denies for good reason (real destructive/cost
potential). A blanket `railway service *` allow would silently
re-open exactly what those deny rules exist to block.

---

[2026-08-13] `Bash(railway restart:*)` / `Bash(railway redeploy:*)` - **flagged as a deny-list gap, not a candidate for promotion**

Context: investigating whether Railway offers a manual "run this cron job
now" trigger, as a workaround for volume access between scheduled ticks.
Confirmed: `railway restart`/`railway redeploy` (top-level CLI commands)
do trigger an immediate, real, out-of-schedule `python crew.py` cycle -
real Anthropic API cost, real side effects (Telegram messages, possible
approval-queue writes).
Decision this time: approved for investigation/documentation purposes
only - never actually invoked to trigger a real cycle this session.
Suggested rule: **none.** `.claude/settings.json`'s existing `deny` list
already blocks `Bash(railway service restart:*)` and `Bash(railway
service redeploy:*)` - clearly written to prevent exactly this class of
unscheduled-real-cost action. But `railway restart`/`railway redeploy`
(the top-level command aliases, not under the `railway service`
subcommand) are a *different literal string* and are **not** covered by
that deny pattern - and this session's `settings.local.json` shows
`Bash(railway restart *)` was in fact separately approved once already.
This looks like an unintentional gap in the deny list's coverage (the
top-level alias slipping through a rule clearly meant to block the
underlying action), not a case for promoting to `allow`. Recommend Jan
either add explicit `deny` entries for `Bash(railway restart:*)`/
`Bash(railway redeploy:*)` to close the gap, or confirm the existing
`railway service restart/redeploy` denials were only ever meant to block
that specific subcommand form and the top-level aliases are intentionally
fine to allow case-by-case (matching `CLAUDE.md`'s current wording, which
already requires per-use confirmation for this regardless of any
`allow`/`deny` rule, since it's a real-cost action). Per `CLAUDE.md`'s own
rule-growth discipline, this is exactly the kind of ambiguity that
deserves an explicit conversation, not a quiet default either way.
