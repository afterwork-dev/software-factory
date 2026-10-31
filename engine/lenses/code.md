Is this a correct, simple change that fits the codebase?

- **Correctness**: it does what each requirement in the spec says. Walk the edge
  cases: empty and missing input, boundaries, failures of the calls it makes,
  ordering and concurrency where they matter. Look for unhandled errors, missing
  awaits, inverted conditions and off-by-one mistakes.
- **Tests**: the new behaviour is tested at the level the project usually tests
  at, and the tests would fail without the change. No test was weakened, skipped
  or bent to fit the code.
- **Simplicity**: it is the smallest change that meets the spec. Flag
  speculative options, dead code, needless layers, silent fallbacks and
  swallowed errors.
- **Reuse**: it uses the helpers, components and patterns that already exist
  instead of copying them.
- **No workarounds**: nothing copies a library's internal markup or classes,
  bends code around a test, or disables a check where a clean solution exists.
- **Protected changes**: if the gate's checks.md lists protected files changed
  with the spec's permission, each change does only what the spec's
  `## Protected changes` describes and weakens nothing else.
- **Fit**: naming, structure, formatting and comment density match the
  neighbouring code and the project's conventions.
- **Docs**: docs describing the changed behaviour are updated.
- **Hygiene**: focused commits with short messages; no stray files, debug
  output or commented-out code.

Use CHANGES for bugs, missing or toothless tests, gaps against the spec, and
clear breaks with the project's conventions. Style preferences go under Notes.
