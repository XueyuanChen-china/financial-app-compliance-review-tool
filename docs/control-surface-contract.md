# Control Surface Contract

## Responsibility split

Phase 2 does not know the concrete application profile. Control compilation
therefore produces policy-level candidate evidence surfaces, not the final
review queue.

```text
policy source -> obligation -> Control candidates
                         + EvidenceRequirement.condition
                         -> Applicability Resolver
                         -> resolved_required_surfaces
                         -> CoverageUnit / WorkItem
```

`candidate_surfaces` answers: "Which evidence surfaces could be relevant to
this policy requirement?"

`resolved_required_surfaces` answers: "Which of those surfaces are required
for this application after reading the confirmed AppProfile?"

The final value is produced by deterministic validation of the structured
Applicability result. The Reviewer cannot add, remove, or reinterpret a
surface requirement.

## Conditional surface example

```yaml
candidate_surfaces:
  - android_native
  - frontend_h5
evidence_requirements:
  android_native:
    minimum_strength: static_proof
    rationale: Native user-facing disclosure.
  frontend_h5:
    minimum_strength: static_proof
    rationale: H5 disclosure when the app has an H5 surface.
    condition:
      kind: atom
      fact: evidence_surfaces
      operator: includes
      value: frontend_h5
```

If the profile confirms only `android_native`, the resolver emits:

```yaml
resolved_required_surfaces:
  - android_native
not_required_surfaces:
  - frontend_h5
```

The legacy `required_surfaces` field remains readable and mirrors
`candidate_surfaces` during migration. Runtime planning must use the resolved
surface decisions, not that compatibility field.

## fin-001 guidance

The regional financial-services control should not unconditionally require
H5, backend API documentation, and backend source code. Its candidate
requirements should be separated into:

- user-facing disclosure: `android_native` or conditional `frontend_h5`;
- target-region/listing evidence: `play_console` and `regulator_external`;
- backend enforcement: only when an obligation explicitly requires server-side
  enforcement, persistence, or validation.

Existing v1 artifacts are accepted through the compatibility adapter. New
Control compilation must emit the candidate/condition form above.
