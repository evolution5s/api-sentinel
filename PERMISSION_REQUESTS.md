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
