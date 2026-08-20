# Day 7 Smoke Test Learning Notes

## Scope

The latest real end-to-end run used one Google Play financial-services policy
source, the Mifos Mobile Android repository, and the Apache Fineract repository.
It treated H5/WebView, Play Console, regulator, and other external surfaces
according to the AppProfile instead of silently pretending they were present.

## Verified Outcome

`mifos-real-final-v4` generated a Snapshot and Markdown report with 12
obligations, 12 controls, 38 coverage units, and 2 formal Reviewer Work Items:
one Android and one backend-code Work Item. The final CI status is `BLOCK`,
which is the correct result because several applicability decisions remain
unknown, required surfaces are missing or manual-only, and the available
static evidence is partial rather than complete.

## Reliability Lessons

- Chat Completions tool-call arguments must be replayed as JSON strings, not
  Python dictionaries. Every assistant tool-call message must also retain
  `content: null`, and every tool result must match a preceding call id. The
  Provider now validates this transcript before sending it, because a
  Chat-Completions-to-Responses relay otherwise reports a vague HTTP 400 such
  as `No tool call found for function call output`.
- The configured `gpt-5.6-luna` relay does not support reasoning effort
  `minimal`; supported values are `none`, `low`, `medium`, `high`, `xhigh`, and
  `max`. The project uses `medium` for navigation, `low` for capture passes,
  and a 180-second transport timeout by default.
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

- targeted Phase 2 regression tests: passed
- real Mifos full review: completed and produced a report
- `ruff check src tests`: passed
- `mypy src`: passed with no issues
- full `pytest -q`: passed
