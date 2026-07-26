# Quanta Backend Architecture & Health Audit Task Report

## 1. Executive Summary
This document serves as the live tracking report for the **Quanta Backend Execution & Control Plane**, specifically isolating the current critical failures preventing end-to-end data extraction on enterprise domains.

---

## 2. Active Blockers & Unresolved Issues

### 🔴 Critical Blocker #1: WAF/CAPTCHA Interception (Distil Networks)
- **Location:** `backend/apps/execution-plane/src/activities/executeUniversalAgent.py` & `coreWorkflow.py`
- **Impact:** Fatal. Extraction fails completely. `DOMHarvester` returns an empty payload (e.g., `Page 1 markdown size: 1 chars`) during Phase 2.
- **Root Cause:** Enterprise sites (like eBay) detect the headless Playwright instance originating from a Data Center IP. They intercept the request and redirect to a WebGL/JS fingerprinting challenge (`splashui/challenge`). Because no premium residential proxy is provided, the challenge acts as a hard wall.

### 🔴 Critical Blocker #2: Shadow DOM Re-render Wiping Attributes
- **Location:** `backend/apps/execution-plane/src/activities/executeUniversalAgent.py` (Action execution loop)
- **Impact:** Fatal. Agent navigation times out during interactions with complex UI components (e.g., search bars, dropdowns).
- **Root Cause:** Although `*css=` shadow DOM piercing is enabled, modern SPAs (React, Marko, Polymer) continuously re-render virtual DOMs upon focus/interaction. The custom `data-quanta-id` attributes injected by `DOMHarvester` are destroyed in the milliseconds between the `reHarvest()` scan and the Playwright locator execution (`Locator.scroll_into_view_if_needed: Timeout 2000ms exceeded`).

### 🟠 High Severity #3: LLM Context Overflow & JSON Hallucinations
- **Location:** `backend/apps/execution-plane/src/activities/executeUniversalAgent.py` & `core/safe_llm.py`
- **Impact:** Workflow latency and potential loop termination (`json_parse_failure_loop`).
- **Root Cause:** Unfiltered extraction of heavy enterprise DOMs routinely yields 500-600+ interactive elements. Feeding this massive serialized payload into the smaller `Llama 3.1 8B` planner causes token exhaustion and prompts the model to generate syntactically invalid JSON (missing brackets/commas). While dynamic self-correction mitigates this, it wastes significant time and API tokens.

### 🟠 High Severity #4: Enforcement of Residential Proxies
- **Location:** `backend/apps/execution-plane/src/activities/coreWorkflow.py` (Proxy Configuration)
- **Impact:** System defaults to cost-saving direct connections when proxy variables are missing, guaranteeing failure on tier-1 e-commerce sites.
- **Root Cause:** `PROXY_SERVER` and residential credentials are not actively enforced or injected into the Docker container environment for `quanta_execution_plane`, neutralizing the anti-bot evasion framework.

---

## 3. Structural & Architectural Overview

### 🏗 Component Breakdown
1. **Control Plane (`control-plane`)**
   - Serves API routes, handles auth/Clerk integration, orchestrates Temporal workflows, manages Postgres/Qdrant databases.
2. **Execution Plane (`execution-plane`)**
   - Temporal worker processing Playwright browser tasks, executing LLM navigation (Llama 3.1 8B) and schema extraction (Llama 3.3 70B).
3. **Recipe Planner (`core/rag/planner.py`)**
   - Converts natural language user goals into structured subtask DAGs (QuantaPlan).
- **Planner Integration:** Guided DAG execution is wired into `coreWorkflow.py` and `executeUniversalAgent.py`; full stability check required once container startup issue is resolved.

---

## 5. Ongoing Checklist & Audit Log

- [x] Pre-flight URL & Prompt Validation
- [x] Fix syntax error in `executeUniversalAgent.py`
- [x] Container stability & health check (`quanta_execution_plane`)
- [x] End-to-End benchmark workflow execution (YouTube / Web Scraping)
