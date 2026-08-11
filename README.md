# Financial App Compliance Review Tool

A Python-based, CI-oriented compliance review system for financial applications.

The project combines deterministic collectors and validators with parallel AI reviewers. Its coverage unit is `Control x Required Evidence Surface`; AI agents investigate and recommend, while ordinary program logic owns coverage, final resolution, snapshots, regression comparison, and CI gating.

## Status

The repository is in architecture and foundation setup. See [the implementation plan](docs/implementation-plan.md) for the full design and delivery roadmap.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
compliance-review --help
pytest
```

## Core Principles

- Controls define what must be reviewed.
- Evidence surfaces define where proof may exist.
- Generic collectors extract repeatable technical facts.
- Parallel reviewers investigate isolated work items.
- A single verifier performs targeted QA only for suspicious results.
- Deterministic code owns coverage and final CI decisions.
- Every run produces an auditable compliance snapshot.

## License

MIT
