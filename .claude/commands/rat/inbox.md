---
name: Rationale Inbox
description: List and process pending a2a messages. Interactive by default; `auto` for use inside /loop.
category: Rationale
tags: [rationale, a2a, messaging]
---

List pending messages in `.a2a/inbox/` and process them. Two modes selected by `$ARGUMENTS`:

- **Interactive (default)** — when `$ARGUMENTS` does NOT contain the word `auto`. Show each pending message to the user, draft a reply, and ask "send / edit / skip / abort" before doing anything. Suitable when the user invokes `/rat:inbox` manually.
- **Autonomous (`auto`)** — when `$ARGUMENTS` contains `auto` (typically because `/loop /rat:inbox auto` is running). Process each message and send replies without prompting. No human-in-the-loop. Exit cleanly so the next loop iteration can run.

Both modes use the same CLI primitives. Only the prompting differs.

## Instructions

### 1. Come online and claim the first message

`a2a sync` is the fused handshake: it (a) idempotently registers pwd as an a2a participant, (b) sweeps any stale `.tmp-processing-*` claims older than 10 minutes back into the inbox (crash recovery), and (c) atomically claims the oldest pending message. One JSON blob is emitted on stdout.

```bash
result=$(~/.rationale/repo/bin/rationale a2a sync)
status=$(echo "$result" | jq -r .claim.status)
```

If `status == "empty"`, skip to step 3.

If `status == "claimed"`, the claim payload is under `.claim`:
- `.claim.id`: the message id
- `.claim.claim_path`: where the message file lives during processing
- `.claim.envelope`: the parsed envelope `{from, from_alias, to, sent_at, body, context, reply_to}`

### 2. Process the claimed message

**Read the body** and use `from_alias`/`from` and `context` (branch, head_sha, pr) to ground your understanding.

**Decide on a reply.**

In **interactive mode**: show the user the sender, sent_at, branch/sha, and full body. Draft a candidate reply and present it. Ask: "**send** this reply / **edit** it / **skip** (no reply) / **abort** (return to inbox, stop processing)?"

In **autonomous mode**: decide whether a reply is appropriate. Questions, requests, status asks → reply. Pure FYI ("step 3 is green") → typically no reply. Compose and send the reply directly.

If you decide to **send a reply**:

```bash
printf '%s' "$REPLY_BODY" | ~/.rationale/repo/bin/rationale a2a send \
  --to "$FROM_SLUG_OR_ALIAS" --reply-to "$ID"
```

Use `from_alias` if known and unambiguous, otherwise `from` (the slug).

**Close out the message** (one of three outcomes):

- Replied: `~/.rationale/repo/bin/rationale a2a inbox-finish --claim "$claim_path" --replied`
- Skipped (handled, no reply): `~/.rationale/repo/bin/rationale a2a inbox-finish --claim "$claim_path"`
- Aborted (interactive only — return to inbox, stop processing): `~/.rationale/repo/bin/rationale a2a inbox-abort --claim "$claim_path"` then break out of the loop.

`inbox-finish` writes a one-line trace to `.a2a/handled.jsonl` (with `replied` flag) and deletes the claim file. The daemon's archive at `~/.rationale/state/a2a-archive/YYYY-MM-DD/<id>.json` is the durable record; `handled.jsonl` is just this clone's local index of what it's seen.

Then claim the next message with `~/.rationale/repo/bin/rationale a2a inbox-claim` (no need to re-run `a2a sync` — the participant is already joined and stale claims were swept at step 1). Repeat step 2 until `inbox-claim` returns `status: "empty"`.

### 3. Closing

In **autonomous mode**: just exit. The loop will iterate.

In **interactive mode**: tell the user what was processed (count + senders + reply count). Then check whether a watcher is already running, and phrase the closing suggestion intelligently. Use the `CronList` tool and filter for jobs whose prompt contains `rat:inbox`:

- **None running** → "Done. To watch for new messages continuously, run `/loop /rat:inbox auto`."
- **Exactly one running** → "Done. An autonomous watcher is already active (job `<id>`). Run `CronDelete <id>` to stop it."
- **More than one** → "Done. Note: multiple watchers are active (<ids>). Consider `CronDelete`-ing the extras."

If the inbox was empty at step 1 (no messages processed): same closing-line logic, just preface with "Inbox empty."

$ARGUMENTS
