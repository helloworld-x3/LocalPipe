# Evidence-Constrained Candidate Selection Design

## Goal

Upgrade LocalPipe's core generation mechanism from single-output retry to evidence-constrained multi-route competition, deterministic selection, and uncertainty-aware review while preserving the public `localize(source_text, market_code, brand=None, verbose=True)` interface and the four-layer responsibilities.

## Scope

- Modify only the internals of pipeline layers 2 and 3 plus final decision assembly.
- Keep layer 1 deconstruction and layer 4 taboo checking responsibilities unchanged.
- Generate three candidates from one deconstructed Brief: `product_proof`, `scene_fit`, and `brand_emotion`.
- Run the existing fidelity and taboo checks for every candidate.
- Apply hard gates before numeric ranking.
- Return the winning candidate in the existing top-level fields and expose all evaluated candidates in new additive fields.
- Preserve legacy behavior behind `LOCALPIPE_SELECTION_MODE=legacy`.
- Use only the Python standard library and existing project modules.

## Architecture

`localize()` continues to load the profile and deconstruct once. In competitive mode it builds three route contracts from the deconstructed elements and profile entries, then calls `recreate()` with a private `_creative_route` hint injected into a copy of the elements. Each candidate is evaluated with the existing strict fidelity verification and taboo checker. A new pure selection module converts these results into comparable scores, filters hard failures, ranks eligible candidates, and calculates the score margin between first and second place.

The selected candidate populates the existing `copy`, `copy_zh`, `used_entries`, `profile_trace`, `fidelity`, `taboo`, and `final_status` fields. New fields are additive:

- `candidates`: all evaluated route results and scores;
- `selection_trace`: selected route, ranking, weights, hard-gate reasons and score margin;
- `uncertainty`: level, margin and reason;
- `review_policy`: `sample`, `mandatory`, or `block`.

## Route contracts

Routes are deterministic and profile-derived rather than new market facts:

1. `product_proof`: prioritize the first selling point and require concrete product evidence.
2. `scene_fit`: place the selling points in a real usage scene drawn from non-taboo profile entries.
3. `brand_emotion`: preserve the selling points while changing the opening emphasis toward the source emotional hook and brand tone.

The route contract is included in the layer-2 prompt. It may change emphasis, hook, scene, and presentation, but may not remove a source selling point, CTA, protected term, or invent a product fact.

## Hard gates

A candidate is ineligible when any condition holds:

- `taboo.risk_level == high`;
- verified weighted fidelity is below `FIDELITY_THRESHOLD`;
- fidelity structure is invalid;
- cultural alignment fails;
- `profile_trace` contains invalid IDs, taboo IDs, or no valid reference;
- layer-2 or layer-3 execution fails.

Medium risk remains eligible for comparison but forces mandatory review. If every candidate is ineligible, the highest-scoring candidate is returned for diagnosis with `final_status=needs_review` or `error`, `review_policy=block`, and the gate reasons exposed.

## Selection score

Eligible candidates are ranked with a deterministic score from 0 to 1:

```text
score =
  verified_fidelity       * 0.45
+ cultural_alignment      * 0.20
+ evidence_trace_quality  * 0.15
+ taboo_safety            * 0.15
+ route_distinctiveness   * 0.05
```

- `verified_fidelity`: existing program-recomputed weighted recovery rate.
- `cultural_alignment`: 1.0 unless the explicit alignment check fails.
- `evidence_trace_quality`: ratio of unique valid profile references to the selected route's available positive evidence, capped at 1.0; invalid/taboo/empty references are hard-gated.
- `taboo_safety`: low=1.0, medium=0.4, high=0.0.
- `route_distinctiveness`: deterministic route prior: product proof=0.90, scene fit=1.00, brand emotion=0.95. This is a small tie-breaker, not a claim about market performance.

The weights and component values appear in `selection_trace`; no model self-reported score is accepted.

## Uncertainty and review policy

Let `margin = top_score - second_score` among eligible candidates.

- high uncertainty: fewer than two eligible candidates or margin `< 0.03`;
- medium uncertainty: margin `< 0.08`;
- low uncertainty: margin `>= 0.08`.

Review policy:

- `block`: no eligible candidate or selected candidate has high risk;
- `mandatory`: selected result is not fully passing, taboo risk is medium/unknown, or uncertainty is high/medium;
- `sample`: selected result passes all gates and uncertainty is low.

The existing `final_status` remains conservative: only a gate-clean, low-risk winner can be `pass`; uncertainty affects review intensity but does not turn a failed candidate into a pass.

## Compatibility and rollback

- Default mode: `LOCALPIPE_SELECTION_MODE=competitive`.
- Emergency rollback: `LOCALPIPE_SELECTION_MODE=legacy` restores the existing single-candidate retry behavior.
- The function signature is unchanged.
- Existing top-level output fields preserve their meanings.
- New fields are optional additions; existing callers can ignore them.
- Model selection remains entirely controlled through the existing environment variables in `model.py`.

## Failure handling

Failure of one candidate does not abort other routes. Candidate errors are recorded inside that candidate. Profile load or layer-1 deconstruction failure still aborts the pipeline because later layers cannot proceed. If all candidate routes fail, the output returns the best diagnostic candidate if available, otherwise `final_status=error`.

## Verification

- Pure scoring tests cover weights, hard gates, ties, uncertainty thresholds, and review policy.
- Pipeline tests prove one deconstruction leads to three route generations and preserves the `localize()` signature.
- Legacy-mode tests prove old single-generation behavior remains available.
- Regression tests prove no high-risk or invalid-trace candidate can win.
- Full `unittest`, syntax compilation, and `git diff --check` must pass.
