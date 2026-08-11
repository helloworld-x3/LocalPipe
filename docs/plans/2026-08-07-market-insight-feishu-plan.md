# Market Insight Feishu Integration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a structured, traceable market-insight layer to the existing Feishu → LocalPipe → Feishu workflow.

**Architecture:** Keep `pipeline.py` unchanged. The connector will derive a deterministic insight card from the selected market profile and task metadata, merge it into the existing result payload, and write the new fields to the result table. This improves the business story without introducing unstable live web research or a callback deployment.

**Tech Stack:** Python standard library, existing LocalPipe profiles, Feishu Bitable REST API, unittest.

---

### Task 1: Add insight-card generation

**Files:**
- Modify: `feishu_connector.py`
- Test: `test_pipeline.py` or a focused connector test

Add a pure function that maps market profile entries and task metadata into: opportunity summary, audience pain points, platform preference, creative direction, risk notes, evidence IDs, and confidence. Do not call external services.

### Task 2: Include insight fields in output payload

**Files:**
- Modify: `feishu_connector.py`

Extend the result payload with the seven insight fields while preserving all existing fields and the dual-App-Token behavior.

### Task 3: Verify locally

Run Python compilation and the existing test suite. Add a focused assertion for the insight card shape and evidence IDs.

### Task 4: Add Feishu result columns

In the configured result table, add the seven text fields plus one numeric confidence field, then run one DEMO task and verify the insight card is visible beside the localized copy.
