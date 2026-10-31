---
name: reply
description: Answer the questions intake asked about a software-factory job, then run it. Only when the user types /factory:reply.
argument-hint: "<job-id> <answers>"
disable-model-invocation: true
---

# Answer intake's questions

Arguments: $ARGUMENTS

The first argument is the job id. Everything after it is the user's answer.

1. Save the answer unchanged with a quoted heredoc:

   ```bash
   factory reply <job-id> --stdin <<'FACTORY_REPLY_END'
   <the answer, verbatim>
   FACTORY_REPLY_END
   ```

2. If that succeeds, read `${CLAUDE_PLUGIN_ROOT}/skills/run/SKILL.md` and run
   the job exactly as it describes, starting at "2. The loop", with this job id.
   If it fails, show the user the `factory:` message and stop.
