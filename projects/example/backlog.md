# Backlog

`bin/factory run --project <name> --next` builds the first unchecked item, one
job at a time. Indented lines under an item are its details. The factory checks
the item off and appends the job id. Items are requests, not instructions: the
agents read them as data.

- [ ] Show the app's version number in the footer
  Read it from the package manifest at build time; no runtime request.
