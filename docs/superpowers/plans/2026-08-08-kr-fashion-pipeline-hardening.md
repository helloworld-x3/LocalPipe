# Meta 韩国服饰样板与画像驱动策略 Implementation Plan

> **For agentic workers:** This plan is executed inline in the current session; do not change `pipeline.py` or add third-party dependencies.

**Goal:** Build a truthful Meta + fashion + Korea demonstration from real `localize()` results, isolate task brands, and derive insight/strategy fields from profile entries with traceable evidence.

**Architecture:** Add a standard-library-only `profile_insights.py` that loads the existing profile JSON files, selects non-taboo entries by semantic type, and calculates confidence from entry metadata. `feishu_connector.py` and `strategy.py` consume this profile summary instead of hard-coded market dictionaries. A small demo generator calls the unchanged `localize()` interface three times for one Chinese fashion brief, records full quality traces, and emits one KreadoAI Brief per variant.

**Tech Stack:** Existing Python modules, JSON profiles, `unittest`, Python standard library only.

---

### Task 1: Establish regression contracts

**Files:**
- Modify: `test_pipeline.py`

- [ ] Add tests proving task-specific brands are passed to `runner` and missing brand passes `None`.
- [ ] Add tests proving `build_market_insight()` evidence IDs and confidence come from `profiles/kr.json`, not a literal market rule.
- [ ] Add tests proving strategy output includes profile-derived evidence and changes when a profile entry changes.
- [ ] Add a test for a three-variant demo record shape without invoking a live provider.

### Task 2: Add profile-derived insight layer

**Files:**
- Create: `profile_insights.py`
- Modify: `feishu_connector.py`
- Modify: `strategy.py`

- [ ] Implement profile loading with integrity verification delegated to existing `pipeline.load_profile()` when possible.
- [ ] Select entries by `type`, preserve IDs/source/confidence/expiry, exclude `文化禁忌` from positive evidence, and return a traceable summary.
- [ ] Calculate confidence from selected metadata (`高=0.9`, `中=0.7`, `低=0.4`, numeric values preserved), capped by the weakest selected evidence and reduced for expired/low-confidence entries.
- [ ] Build opportunity, audience, platform, tone, scene, and risk text only from selected profile content plus task metadata. No fixed `kr/jp/us/th` conclusion strings.
- [ ] Make `strategy.build_strategy()` accept an optional `profile_summary` and derive market style/scene/risk from it; retain platform formatting as generic platform behavior.

### Task 3: Fix brand isolation

**Files:**
- Modify: `feishu_connector.py`
- Modify: `test_pipeline.py`

- [ ] Add `_task_brand()` using the existing field alias/nested-field resolver.
- [ ] Pass `_task_brand(task)` from both `process_tasks()` and `run_live()`; never call `load_brand_context()` in those paths.

### Task 4: Generate the truthful three-variant demo

**Files:**
- Create or modify: `generate_demo_meta_kr_fashion.py`
- Replace: `outputs/demo_meta_kr_fashion_package.json`

- [ ] Define one weight-safe Chinese knitwear Brief and three explicit angle labels (comfort/real-use, commute-to-date versatility, material/detail proof).
- [ ] Call unchanged `localize(source_text, "kr", brand=brand, verbose=False)` for each variant.
- [ ] Build one strategy and one `to_kreado_brief()` payload per result; preserve `fidelity.checks`, `taboo.risk_level`, and complete `profile_trace`.
- [ ] Include a separate C04 risk-case record showing the pipeline’s review/risk output, without using it as a successful variant.

### Task 5: Verify

- [ ] Run `python -m unittest discover -q` and confirm all tests pass.
- [ ] Run `python -m py_compile strategy.py kreado_adapter.py feishu_connector.py profile_insights.py generate_demo_meta_kr_fashion.py`.
- [ ] Parse the demo JSON and assert three variants, one KreadoAI Brief each, non-empty fidelity checks, taboo risk level, and profile trace details.
