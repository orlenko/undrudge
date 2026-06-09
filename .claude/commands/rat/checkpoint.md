---
name: Rationale Checkpoint
description: Capture a rationale checkpoint — summarize the current session's decisions, risks, and context
category: Rationale
tags: [rationale, checkpoint, documentation]
---

Create a rationale checkpoint for the current session. This captures your decisions, risks, open questions, and context into a structured summary published to the agent-rationale repo.

Use this when you want to record rationale at a meaningful point — after a design decision, before switching context, or after a significant investigation. Checkpoints are also created automatically on every git commit via the post-commit hook.

This command does **not** touch a2a (agent-to-agent messaging) state. Checkpointing and chat membership are independent. If you want to send a message, use `/rat:ask`. If you want to read or watch incoming mail, use `/rat:inbox`.

## Instructions

1. Run the checkpoint command:
   ```bash
   ~/.rationale/repo/bin/rationale checkpoint
   ```
2. Report the result to the user — whether the checkpoint was published successfully, and any redactions that were applied.

If the command fails (e.g., no captured session state), explain that the Stop hook needs to have fired at least once in this session to accumulate state.

$ARGUMENTS
