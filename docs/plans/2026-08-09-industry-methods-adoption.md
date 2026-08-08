# Industry Methods Adoption Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Adopt six mature localization and advertising methods without changing the `localize()` interface or the existing four-layer pipeline.

**Architecture:** Add small standard-library-only modules around the current pipeline: an MQM-inspired quality report, reusable language assets, a DCO-inspired creative matrix, blind-test metrics, a professional transcreation delivery contract, and structured in-country review categories. Existing LocalPipe results remain the source of truth and are enriched only after generation.

**Tech Stack:** Python standard library, existing `unittest` suite, Feishu Bitable connector, existing LocalPipe/Kreado adapters.

---

### Task 1: MQM-inspired advertising quality report

**Files:**
- Create: `quality_framework.py`
- Test: `test_pipeline.py`

Map fidelity failures, profile-trace failures, and taboo flags into stable error categories and severities. Produce release decisions (`publish`, `needs_review`, `block`) without replacing the pipeline's own `final_status`.

### Task 2: Reusable brand and terminology assets

**Files:**
- Create: `language_assets.py`
- Test: `test_pipeline.py`

Convert task/brand/profile metadata into an auditable language-asset contract and provide a deterministic audit for protected and forbidden terms.

### Task 3: DCO-inspired creative matrix

**Files:**
- Create: `creative_matrix.py`
- Modify: `generate_demo_meta_fr_fashion.py`
- Test: `test_pipeline.py`

Generate three differentiated, evidence-linked creative routes from selling points, scene, tone, and platform strategy instead of keeping the variant logic only as handwritten labels.

### Task 4: Blind-test experiment metrics

**Files:**
- Create: `experiment_metrics.py`
- Test: `test_pipeline.py`

Reveal X/Y responses with the private key, calculate LocalPipe/Baseline/tie counts, non-tie win rate, publication usability, and keep safety cases separate from the main quality average.

### Task 5: Professional transcreation delivery contract

**Files:**
- Create: `transcreation_delivery.py`
- Modify: `generate_demo_meta_fr_fashion.py`
- Modify: `feishu_connector.py`
- Test: `test_pipeline.py`

Package target copy, Chinese back translation, rationale, recommended use, language assets, quality report, and KreadoAI brief as one auditable deliverable.

### Task 6: Structured in-country review taxonomy

**Files:**
- Modify: `review_ai.py`
- Test: `test_pipeline.py`

Normalize review issues into stable categories covering naturalness, terminology, omission, hallucination, culture/compliance, platform fit, and downstream brief quality.

### Verification

Run:

```powershell
python -m unittest discover -q
python -m py_compile quality_framework.py language_assets.py creative_matrix.py experiment_metrics.py transcreation_delivery.py review_ai.py generate_demo_meta_fr_fashion.py feishu_connector.py
```

Then generate or structurally validate the France demo package and confirm each variant contains a creative-matrix route and transcreation delivery with full quality detail.
