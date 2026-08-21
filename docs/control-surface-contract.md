# Control Surface Contract

## Responsibility split

Phase 2 does not know the concrete application profile. Control compilation
therefore produces policy-level candidate evidence surfaces, not the final
review queue.

```text
policy source -> obligation -> atomic EvidenceClaim
                         + candidate ProofRoute[]
                         -> Applicability Resolver
                         -> selected route IDs
                         -> CoverageUnit / WorkItem
```

`EvidenceClaim` answers: "What independently verifiable statement must be
proven?"

`ProofRoute` answers: "Which surface can prove this claim, at what strength,
and with what limits?"

`selected_route_ids` answers: "Which candidate route is active for this
application after reading the confirmed AppProfile?"

The final value is produced by deterministic validation of the structured
Applicability result. The Reviewer cannot add, remove, or reinterpret a
surface requirement.

## Conditional surface example

```yaml
evidence_claims:
  - claim_id: disclosure-entry
    statement: A disclosure entry exists before the relevant action.
    proof_route_policy: any_one
    proof_routes:
      - route_id: android-disclosure
        surface: android_native
        claim_to_prove: Native disclosure entry exists.
        expected_evidence_strength: static_proof
        why_this_surface: The app may deliver the disclosure through native UI.
        proof_limits:
          - Static code does not prove runtime display.
      - route_id: h5-disclosure
        surface: frontend_h5
        claim_to_prove: H5 disclosure entry exists.
        expected_evidence_strength: static_proof
        why_this_surface: Use only when H5 is configured.
        proof_limits:
          - Static code does not prove runtime display
```

If the profile confirms only `android_native`, the resolver emits:

```yaml
selected_route_ids:
  - android-disclosure
```

The legacy `required_surfaces`, `candidate_surfaces`, and keyed
`evidence_requirements` fields remain readable for old artifacts. New runtime
planning uses selected proof routes and does not create units for unselected
surfaces.

## fin-001 guidance

The regional financial-services control should not unconditionally require
H5, backend API documentation, and backend source code. Its evidence claims
and candidate proof routes should be separated into:

- user-facing disclosure: `android_native` or conditional `frontend_h5`;
- target-region/listing evidence: `play_console` and `regulator_external`;
- backend enforcement: only when an obligation explicitly requires server-side
  enforcement, persistence, or validation.

Existing v1 artifacts are accepted through the compatibility adapter. New
Control compilation must emit the candidate/condition form above.
