---
name: Rationale Leave
description: Remove this clone from the a2a chat. Does NOT disable rationale checkpointing.
category: Rationale
tags: [rationale, a2a, messaging]
---

Leave the a2a chat from this clone. Useful when you (the agent) need to focus on work without being interrupted by incoming messages from other sessions.

**This does NOT disable rationale checkpointing.** The Stop hook and post-commit hook keep capturing rationale silently. `/rat:checkpoint` keeps working. Only chat membership is dropped.

To re-join later: any of `/rat:ask`, `/rat:inbox`, or `~/.rationale/repo/bin/rationale a2a join` will register this clone again.

## Instructions

1. Stop any active inbox watcher first. Use `CronList` to find cron jobs whose prompt contains `rat:inbox`. For each match, run `CronDelete <jobId>`. Tell the user how many watchers were stopped (zero is fine).

2. Remove this clone from the participants registry:
   ```bash
   ~/.rationale/repo/bin/rationale a2a leave
   ```

   The local `.a2a/` workspace is preserved — only the registry entry is removed. The daemon stops watching this clone's outbox and stops publishing manifests to it.

3. Tell the user: a2a chat left, watchers stopped, rationale checkpointing remains active. Mention that any of `/rat:ask`, `/rat:inbox`, or `rationale a2a join` will re-register the clone.

$ARGUMENTS
