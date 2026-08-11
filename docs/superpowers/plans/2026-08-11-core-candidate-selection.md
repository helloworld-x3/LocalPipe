# Core Candidate Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add evidence-constrained multi-route generation, deterministic candidate selection, uncertainty gating, and risk-adjusted review policy inside `pipeline.py` without changing `localize()`'s public interface.

**Architecture:** Keep selection mathematics in a focused standard-library module and keep orchestration inside the existing four-layer pipeline. Competitive mode generates three route-guided candidates, evaluates each with existing quality gates, and maps the winner back to legacy top-level fields; an environment switch preserves the old path.

**Tech Stack:** Python standard library, existing LocalPipe modules, `unittest`.

---

### Task 1: Pure candidate scoring and hard gates

**Files:**
- Create: `candidate_selection.py`
- Modify: `test_pipeline.py`

- [ ] **Step 1: Write failing tests** for hard-gate rejection, deterministic weighted score, tie ordering, uncertainty thresholds, and review policy.
- [ ] **Step 2: Run** `python -m unittest test_pipeline.TestCoreCandidateSelection -v` and confirm failures are caused by the missing module/API.
- [ ] **Step 3: Implement** `evaluate_candidate(candidate, threshold)`, `rank_candidates(candidates, threshold)`, and `build_selection_decision(candidates, threshold)` with the exact weights and thresholds in the approved design.
- [ ] **Step 4: Re-run** the focused test class and confirm all tests pass.

### Task 2: Route-guided layer-2 generation

**Files:**
- Modify: `pipeline.py`
- Modify: `test_pipeline.py`

- [ ] **Step 1: Write failing tests** proving the same decomposed elements create the three stable route IDs and that route hints appear in the `recreate()` prompt contract without changing its signature.
- [ ] **Step 2: Run** the focused tests and confirm the current implementation cannot distinguish routes.
- [ ] **Step 3: Add** private route helpers in `pipeline.py`: build route contracts from source elements/profile entries and consume an optional `_creative_route` field inside `recreate()`.
- [ ] **Step 4: Re-run** the focused route tests.

### Task 3: Competitive orchestration inside `localize()`

**Files:**
- Modify: `pipeline.py`
- Modify: `test_pipeline.py`

- [ ] **Step 1: Write failing tests** proving competitive mode deconstructs once, generates/evaluates three candidates, rejects high-risk or invalid-trace candidates, and exposes `candidates`, `selection_trace`, `uncertainty`, and `review_policy`.
- [ ] **Step 2: Run** the focused tests and observe the missing competitive fields.
- [ ] **Step 3: Extract** the current single-candidate layer-2/3/4 flow into a private evaluator that preserves existing validation and retry behavior for one route.
- [ ] **Step 4: Implement** competitive orchestration, select the winner with `candidate_selection.py`, and populate existing top-level output fields from the winner.
- [ ] **Step 5: Re-run** competitive orchestration tests.

### Task 4: Legacy switch and conservative status compatibility

**Files:**
- Modify: `pipeline.py`
- Modify: `.env.example`
- Modify: `test_pipeline.py`

- [ ] **Step 1: Write failing tests** proving `LOCALPIPE_SELECTION_MODE=legacy` calls only one generation path and retains legacy output semantics.
- [ ] **Step 2: Implement** the environment switch with competitive as default and legacy as explicit fallback.
- [ ] **Step 3: Add** `LOCALPIPE_SELECTION_MODE=competitive` with a short comment to `.env.example`.
- [ ] **Step 4: Re-run** compatibility tests.

### Task 5: Demonstration and regression verification

**Files:**
- Modify: `generate_demo_meta_fr_fashion.py` only if additive competitive fields need preservation.
- Modify: `README.md` to describe the new core mechanism truthfully.
- Modify: `test_pipeline.py`

- [ ] **Step 1: Add regression assertions** that the France fashion demo retains full candidate quality evidence and selection trace.
- [ ] **Step 2: Run** `python -m unittest discover -q`.
- [ ] **Step 3: Run** `python -m py_compile candidate_selection.py pipeline.py generate_demo_meta_fr_fashion.py test_pipeline.py`.
- [ ] **Step 4: Run** `git diff --check` and confirm no secrets or unrelated files were added.
- [ ] **Step 5: Run a real France fashion sample** only if the configured LLM API is available; label its result as a sample, not proof of ROI or superiority.
