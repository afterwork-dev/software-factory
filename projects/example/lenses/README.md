# Project lenses

A file here named `<lens>.md` (lowercase) is a review checklist. It either adds
a lens the engine doesn't have, or replaces the engine's lens of the same name
(`engine/lenses/code.md`, `security.md`, `ux.md`, `ui.md`).

A lens only runs when routing asks for it. To add, say, a performance review
for changes that touch hot paths, write `lenses/performance.md` and add a
surface to this profile's `config.json`:

    "surfaces": {
      "performance": {
        "about": "touches request handlers, queries or loops on hot paths",
        "lenses": { "performance": "sonnet" }
      }
    }

Intake then flags `performance` when a change touches it, and the reviewer
agent runs with this checklist plus `context/performance.md`, if you write one.
