---
name: status
description: Show the software factory's jobs, what is waiting for a person, and the backlog. Use when the user types /factory:status or asks what the factory is doing.
argument-hint: "[--project <name>]"
---

# Factory status

Arguments: $ARGUMENTS

Run `factory status $ARGUMENTS`. If a project is named, or there is only one
profile, also run `factory queue $ARGUMENTS`. Show the output, then point out
any job in `needs-human` or `needs-info` and the command that moves it on
(`factory step <job-id>` prints it in `tell_user`).

Don't change anything: this skill only reads.
