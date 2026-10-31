---
name: rework
description: Send a software-factory job that is waiting for a person back to the builder with the user's note, then run it. Only when the user types /factory:rework.
argument-hint: "<job-id> <note for the builder>"
disable-model-invocation: true
---

# Rework a job

Arguments: $ARGUMENTS

The first argument is the job id. Everything after it is the user's note for
the builder.

1. Save the note unchanged with a quoted heredoc:

   ```bash
   factory rework <job-id> --stdin <<'FACTORY_NOTE_END'
   <the note, verbatim>
   FACTORY_NOTE_END
   ```

2. If that succeeds, read `${CLAUDE_PLUGIN_ROOT}/skills/run/SKILL.md` and run
   the job exactly as it describes, starting at "2. The loop", with this job id.
   If it fails, show the user the `factory:` message and stop.
