---
name: Rationale Ask
description: Send a prompt to another local Claude session via the a2a relay daemon
category: Rationale
tags: [rationale, a2a, messaging]
---

Send a message from this clone to another participating Claude session. Delivery happens via the agent-rationale a2a relay daemon (`rationale a2a daemon`); the recipient sees the message in their `.a2a/inbox/`.

## Arguments

`$ARGUMENTS` is `<recipient> <body...>`. The first whitespace-delimited token is the recipient (alias, slug, or unique substring of either). Everything after that, up to the end, is the body.

Examples:
- `tester pull origin/feat/foo and run the smoke suite, reply with the first failure`
- `dev step 3 is green; we're moving on`
- `abc123 investigate the timeout in /api/handler and reply with a root-cause summary`

If the user wrote something more conversational like `ask tester to pull origin/foo`, normalize it: extract `tester` as the recipient, drop the framing words, and write the body in the second person as a direct prompt to that recipient. But prefer the simple positional form.

## Instructions

1. Ensure this clone is registered as an a2a participant (idempotent — safe to re-run):
   ```bash
   ~/.rationale/repo/bin/rationale a2a join >/dev/null
   ```

2. Parse `$ARGUMENTS` into `RECIPIENT` (first token) and `BODY` (the rest, trimmed). If the input doesn't have at least two whitespace-separated parts, tell the user the expected form (`/rat:ask <recipient> <body>`) and stop.

3. Send the message, piping the body via stdin so multi-line text and quotes pass through cleanly:
   ```bash
   printf '%s' "$BODY" | ~/.rationale/repo/bin/rationale a2a send --to "$RECIPIENT"
   ```
   The CLI prints the message id on stdout and a one-line summary on stderr. If the daemon is not running it warns but still queues the message — delivery resumes when the daemon comes up. If the recipient is ambiguous, surface the ambiguity-error message and ask the user to disambiguate.

4. After the send succeeds, check whether an inbox watcher is already running so you can phrase the closing line intelligently. Use the `CronList` tool to list active cron jobs and look for one whose prompt contains `rat:inbox`:
   - **None found** → close with: `Sent (id <id>). Replies will arrive in your inbox. Run /loop /rat:inbox auto to watch for them autonomously, or /rat:inbox to handle them by hand.`
   - **Exactly one found** → close with: `Sent (id <id>). Replies will arrive in your inbox; an autonomous watcher is already running as job <jobId>.`
   - **More than one found** → close with: `Sent (id <id>). Note: multiple inbox watchers are active (<jobIds>). You may want to CronDelete the extras to avoid duplicate replies.`

5. Do NOT enter `/loop` yourself. Sending is fire-and-send; watching for replies is the user's choice (suggested, not imposed).

$ARGUMENTS
