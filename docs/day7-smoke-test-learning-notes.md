# Day 7 Smoke Test Learning Notes

## Scope

The first real smoke run used one Google Play financial-services policy source,
the Mifos Mobile Android repository, and the Apache Fineract repository. It
intentionally excluded H5/WebView, Play Console, regulator, and other external
evidence surfaces.

## Verified Outcome

`day7-smoke-final-13` completed all four Android Reviewer Work Items and
generated a Snapshot and Markdown report. The final CI status is `BLOCK`, which
is the correct result because required evidence surfaces are missing and the
available static evidence does not prove every control completely.

## Reliability Lessons

- Chat Completions tool-call arguments must be replayed as JSON strings, not
  Python dictionaries. The relay accepted the initial tool call but returned
  HTTP 502 on the following request when this contract was violated.
- Tool outputs need independent bounds. Search, inventory, collector-fact and
  file-read results now have small limits so a single broad query cannot consume
  the Reviewer context.
- A Work Item budget is an exploration limit, not evidence that a control passes
  or fails. When a bounded investigation cannot safely request another model
  turn, the runtime emits a completed `indeterminate` result with the collected
  anchors and an explicit manual-follow-up reason.
- Context compression must be able to retire an older active round. Waiting only
  for the active window to overflow can cause a context failure before the
  compressor has anything to summarize.
- Model outputs from compatible relays may use a version wrapper or a
  single-control form. The parser accepts only known, normalized variants and
  still validates exact Work Item, control, and surface assignment.

## Report Interpretation

The report distinguishes completed Reviewer work from coverage with complete
evidence. A completed Work Item with partial or missing evidence remains
`indeterminate` and does not count as a fully evidenced coverage unit. This is
why a completed smoke run can correctly produce `BLOCK` and zero complete
evidence units.

## Verification

- `pytest -q`: 141 passed
- `ruff check .`: passed
- `mypy`: 54 source files, no issues
- `git diff --check`: passed
