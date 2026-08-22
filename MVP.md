# Quanta MVP — Scraping Engine Status

> Based on direct codebase analysis: `execution-plane/src/`, `control-plane/`, and all sub-modules.
> Last audited: 2026-08-10

---

## Architecture Overview (What Exists)

```
User Request
    │
    ▼
Control Plane (Go/Gin)          ← API, Auth, Job Persistence, WebSocket
    │  POST /v1/sighted/sync
    │  POST /v1/execute
    ▼
Temporal Workflow Engine        ← Orchestration, Retry, State Machine
    │
    ▼
Execution Plane (Python)        ← Browser + AI Engine
    ├── Preflight Pipeline      → HTTP Check → Oracle LLM → RAG Memory → Planner
    ├── Session Manager         → BYOS (Redis + Fernet AES encrypted)
    ├── Browser Layer           → Playwright Chromium (headless)
    ├── Navigation Engine       → SmartFinder + Heuristics Router + axTree
    ├── Network Sniffer         → XHR/Fetch intercept, JSON payload capture
    ├── DOM Extractor           → JS Schema Walker + LLM fallback
    ├── RAG / Recipe Cache      → pgvector (Postgres) + OpenAI embeddings
    └── Planner                 → NVIDIA NIM (llama-3.1-8b) → DAG recipe
```

---

## 1. What Is FULLY Working

| Component | Status | Notes |
|---|---|---|
| **BYOS Session** (Redis + Fernet) | ✅ Complete | Save, restore, verify, delete. Cookie scoping fixed. |
| **URL Preflight** (HTTP HEAD check) | ✅ Complete | Normalize, HEAD, fail-fast on 404/410/451 |
| **Cloudflare 200 Honeypot Detection** | ✅ Complete | Scans body for `cf-browser-verification`, `__cf_chl_opt` etc. |
| **Prompt Structural Validation** | ✅ Complete | Rejects SQL/CLI commands, too-short prompts, bare URLs |
| **Network Sniffer** | ✅ Complete | XHR/Fetch intercept, JSON hijack bypass (Meta `for(;;)`, Google `)]}'`) |
| **DOM Schema Extractor** | ✅ Complete | JS schema walker for list/single modes, LLM fallback per-field |
| **Recipe RAG Cache** (pgvector) | ✅ Exists | `RAGService` — embed → pgvector cosine search → save on success |
| **Planner** (NVIDIA NIM) | ✅ Exists | Generates `QuantaPlan` DAG → `StepDirection` steps |
| **Heuristics Router** | ✅ Exists | `INTENT_MAP` → deterministic playbooks for search/auth/filter/pagination |
| **Self-Healing (LLM repair)** | ✅ Exists (basic) | `healWorkflowActivity` — LLM patches failed step using DOM snapshot |
| **Parallel URL Processing** | ✅ Complete | Semaphore-controlled, isolated browser contexts per URL |
| **Control Plane API** | ✅ Complete | Jobs CRUD, Cancel, Delete, Logs, WebSocket, API Keys, Vault |
| **Temporal Integration** | ✅ Complete | Workflow dispatch, status updates via NATS → WebSocket |
| **File Downloads + S3** | ✅ Complete | Download handler → local disk → S3/MinIO upload (feature-flagged) |

---

## 2. What Is INCOMPLETE or BROKEN

### 2.1 — Multi-Page Plan Staleness ✅ FIXED

**Solution implemented:** `core/checkpoint/checkpoint_manager.py`
- `CheckpointManager` captures a multi-dimensional DOM fingerprint (5 weighted dimensions: interactive count, text volume, unique links, heading count, list count) after each plan step.
- `validate_landing()` computes a divergence score and applies three gates: structural minimum, interactive element delta, text volume delta.
- On divergence, `patch_plan()` calls the Planner with augmented context (current URL + nav axTree + completed-step summary) and splices the new steps back into the running `active_plan` list in-place.
- Guards: max 3 re-plans per job, fully non-fatal (always returns something executable).
- Integrated into `executeUniversalAgent.py` guided-mode step loop.

---

### 2.2 — Semantic Feasibility Check ✅ FIXED

**Solution implemented:**
1. **`core/rag/domain_heuristics.py`** — zero-LLM static feasibility layer:
   - `DOMAIN_REGISTRY`: 30+ curated domains with category, auth requirements, dynamic flags, API-friendly flags, and per-domain blocked intent substrings.
   - `GLOBAL_BLOCK_PATTERNS`: regex patterns for universally impossible requests (credential extraction, hacking, PII scraping).
   - Returns `ALLOWED | BLOCKED | BYOS_REQUIRED | UNKNOWN`. Only `UNKNOWN` escalates to the Oracle LLM.
2. **`core/rag/preflight.py`** — critical `byos_active` NameError **fixed**: renamed to `is_byos_session` (the actual parameter).
3. **New pipeline flow**: Stage 0 (static heuristic, ~0ms) → Phase 1a (HTTP check) → Phase 1b (Oracle LLM — only for UNKNOWN domains).

---

### 2.3 — Content Extraction for Arbitrary Fields ✅ FIXED

**Solution implemented:** `core/extraction/selector_synthesizer.py`
- At plan time, injects `_DOM_PREVIEW_JS` into the live page to capture: pruned HTML (noise-removed), all class names, all `data-*` attributes.
- One LLM call generates a `{ field_name → [css_selector...] }` map with 1–3 selectors per field, ordered most-specific first.
- `_validate_selectors()` runs each synthesized selector against the live DOM, removes ones that match 0 elements or throw CSS parse errors.
- `extract_with_dom()` updated with `synthesized_selectors` parameter: merges synthesized selectors into the heuristic library. Synthesized selectors take priority, heuristic library is the fallback, LLM is last resort per-field only.
- Integrated into `executeUniversalAgent.py`: synthesis runs once before the pagination loop, reuses the already-instantiated `llm_extractor` client.

---

### 2.4 — RAG Recipe Cache ✅ FIXED

**Solution implemented:** `core/rag/unified_recipe_store.py`
- **Adapter pattern** wraps both stores behind a single interface — no DB migration required, zero downtime.
- `find()`: queries Qdrant + pgvector concurrently (`asyncio.gather`), returns the highest-confidence match above 0.75 similarity.
- `save()`: writes to BOTH stores simultaneously on every successful job completion.
- **Intent normalization** applied before embedding: collapses numeric qualifiers (`10`, `100` → `<NUM>`), strips filler words, lowercases. "get top 10 products" and "fetch first 10 results" now map to the same vector neighborhood.
- **Schema versioning**: each recipe carries a `schema_hash = sha256(domain + step_actions)`. Superseded recipes are overwritten in-place (Qdrant upsert), not lost.
- Wired into `core_workflow.py`: replaced `RecipeManager`-only save with `await unified_store.save()`.

---

### 2.5 — Browser Fingerprinting ✅ FIXED

**Solution implemented:** `core/browser/stealth.py`
- **Group A**: Navigator API surface — webdriver=false, vendor=Google Inc., platform=Win32, 3 real plugins, 2 MIME types, languages=[en-US,en], hardwareConcurrency=8, deviceMemory=8, connection={effectiveType:4g, rtt:50}.
- **Group B**: Chrome globals — full `window.chrome` with runtime, app, csi, loadTimes shaped exactly like Chrome 120.
- **Group C**: Headless heuristics — outerHeight = innerHeight+88, screen dimensions 1920×1080, Permissions API patched, Notification.permission = 'default'.
- **Group D**: Canvas fingerprint noise — sub-pixel LSB noise added to `toDataURL`/`toBlob` output without visible artefacts.
- **Group E**: WebGL spoofing — UNMASKED_VENDOR/RENDERER overridden to Intel Iris (covers both WebGL 1 and 2 contexts).
- **Group F**: `performance.memory` — injected when missing.
- **Group G/H/I/J**: iframe, self/top guards, Error stack normalization.
- Applied via `apply_stealth_to_context(context)` on both the main context and every parallel URL sub-context. Replaces the 3-line shim.

---

### 2.6 — Pagination Handling ✅ FIXED

**Solution implemented:** `core/extraction/pagination_engine.py`
- **Strategy 1 — Next Button**: 12-candidate CSS probe including `aria-label`, `rel=next`, class patterns. Validates that clicking actually produces new DOM content before marking as success.
- **Strategy 2 — Load More**: Text-content scan of all `button/a/[role=button]` elements against 10 keyword patterns. Resets after infinite scroll reveals new button.
- **Strategy 3 — Infinite Scroll**: Two-phase poll — scrolls to bottom, then polls for page height increase OR item count increase every 400ms. Configurable settle time.
- **Strategy 4 — URL Parameter**: Detects `page`, `p`, `offset`, `start`, `from`, `skip`, `pg` parameters in current URL. Increments by 1 (page params) or by item-count (offset params). Falls back to appending `?page=N`.
- **Quantity-aware stopping**: Parses integers from the navigation objective; if `10 ≤ N ≤ 100,000`, stops pagination at N rows regardless of strategy.
- **DOM content gate**: Compares item-like node counts before/after each strategy to prevent false advancement.
- Wired into `executeUniversalAgent.py`: replaces the hardcoded single-strategy block. Emits `THINK` log with strategy name for observability.

---

### 2.7 — Output Schema and Format

**What exists:** `extraction_schema` is passed through the pipeline. `dom_extractor.py` returns `dict | list`. The export logic in `core_workflow.py` does CSV serialization for S3 upload.

**Missing:**
- User-specified output format enforcement (CSV column ordering, JSON array vs NDJSON, Excel)
- Schema inference when user provides no schema: LLM infers field list from prompt + DOM
- Schema validation before returning: ensure required fields are non-null
- Partial result streaming for large extractions (don't wait for 1000 items — stream batches)

---

### 2.8 — The Healer (Self-Healing)

**What exists:** `healWorkflowActivity` in `healing_activities.py` — takes `failed_map`, `error_trace`, `dom_state`, asks LLM to patch the JSON map.

**The problem:** This is a **reactive** healer, not a **proactive** one. It only fires after a job fails. The user mentioned:
> "We need a healer who know every parent div so if the child changes so we know where it changed."

**Missing:**
- **Structural fingerprinting per selector**: store parent hierarchy signature alongside each selector in the recipe. On replay, before using a selector, verify the parent context still matches.
- **Proactive drift detection**: periodic background job that replays recipes against known URLs, checks if selectors still resolve
- **Partial healing**: update only the broken selector in the RAG recipe, not the whole workflow map

---

### 2.9 — Playground / Chat UI (Not Built)

**What exists:** The DAG editor (n8n-style) exists in the frontend. API and SDK foundations exist.

**Missing entirely:**
- The ChatGPT-style playground: URL input + prompt input + format selector
- Connects to `/v1/sighted/sync` under the hood
- Real-time streaming output display as data comes in
- One-click "save as workflow" from a successful playground run

---

### 2.10 — Rate Limiting and Concurrency at Job Level

**What exists:** Semaphore in `_process_single_url` (5 concurrent URLs per job). Redis rate limiting at the API level in Go.

**Missing:**
- **Per-domain rate limiting**: 10 workers all hitting `amazon.com` = instant ban. No domain-level concurrency cap.
- **Adaptive backoff**: detect 429 from network sniffer (implemented in sniffer), but nothing acts on `self.rate_limited = True` in the execution loop
- **Job queue priority**: FIFO only, no priority lanes

---

## 3. What Is Completely Missing (Not Built At All)

| Feature | Priority | Notes |
|---|---|---|
| **Playground UI** (ChatGPT-style) | HIGH | Frontend missing entirely |
| **Domain Concurrency Guard** | HIGH | Multiple users hitting same domain will get banned |
| **Selector Synthesis at Plan Time** | HIGH | The core gap in extraction accuracy |
| **Checkpoint Re-planning** | HIGH | Multi-page scraping falls apart without this |
| **Proactive Drift Detection / Healer** | MEDIUM | Background job to verify recipe selectors periodically |
| **Pagination: Infinite Scroll** | MEDIUM | No scroll-to-bottom with item count tracking |
| **Output Streaming** (batch results) | MEDIUM | Large jobs deliver nothing until complete |
| **Schema Inference** (no-schema mode) | MEDIUM | User must always provide extraction_schema |
| **Intent Normalization** (RAG) | MEDIUM | Semantically same prompts create duplicate recipes |
| **Unified RAG Store** | HIGH | Two disconnected stores break the learning loop |
| **Browser Stealth** (full fingerprint) | HIGH | Current spoofing is minimal, won't pass advanced bot detection |
| **Pagination: Load More** | LOW | Rare but common in modern SPAs |

---

## 4. Known Bugs (Hard Errors)

| Location | Bug | Status |
|---|---|---|
| `preflight.py:228` | `byos_active` undefined variable | ✅ FIXED — renamed to `is_byos_session` |
| `healing_activities.py:89` | `BrowserPool.getPage()` — may not be initialized in all worker contexts | ⚠️ OPEN — healer is low priority for MVP |
| `core_workflow.py:564` | `fresh_account = account_mgr.lease_account()` — missing `await` | ✅ FIXED — added `await` |
| `preflight.py` | `is_byos_session` vs `byos_active` name mismatch | ✅ FIXED |
| `rag_service.py:104` | `embed()` calls sync OpenAI client inside async — blocks event loop | ✅ FIXED — wrapped in `asyncio.to_thread` |
| `rag_service.py` | `save_template()` signature mismatch vs unified store caller | ✅ FIXED — dual-convention signature |
| `rag_service.py` | SQLAlchemy `engine.connect/begin` called synchronously in async context | ✅ FIXED — wrapped in `asyncio.to_thread` |

---

## 5. MVP Milestone Checklist

### Phase 1 — Stability (Fix What's Broken)
- `[x]` Fix `byos_active` undefined variable in `preflight.py` — renamed to `is_byos_session`
- `[x]` Fix `await` missing on `account_mgr.lease_account()` in `core_workflow.py`
- `[x]` Fix `rag_service.py` sync call inside async method — wrapped `embed()`, `find_template()`, `save_template()` DB calls in `asyncio.to_thread`
- `[x]` Unify RAG stores: `UnifiedRecipeStore` adapter writes to both Qdrant + pgvector with intent normalization
- `[x]` Add `_dom_fingerprint()` checkpoint comparison in the execution loop — full `CheckpointManager` implemented

### Phase 2 — Core Accuracy (Make Scraping Reliable)
- `[x]` Selector synthesis at plan time: LLM generates CSS selectors per field from DOM preview, stored in recipe — `selector_synthesizer.py`
- `[x]` Checkpoint re-planning: on fingerprint divergence, trigger local re-plan for current page — `checkpoint_manager.py`
- `[x]` Multi-strategy pagination: Next Button, Load More, Infinite Scroll, URL Parameter — `pagination_engine.py`
- `[x]` Quantity-aware stopping: parse integer from objective, stop at N rows — wired in `executeUniversalAgent.py`
- `[x]` Domain concurrency guard: per-domain asyncio semaphore — `domain_semaphore.py`
- `[x]` Browser stealth JS layer: 9-group fingerprint suppression — `stealth.py` (TLS/JA3 requires `patchright`)

### Phase 3 — Latency / Cost (Make It Efficient)
- `[x]` Intent normalization before RAG embed — built into `UnifiedRecipeStore._normalize_intent()`
- `[x]` Cache preflight oracle results by domain: `preflight_cache.py` — 24h TTL, 1h for BLOCKED, BYOS bypass
- `[ ]` Network Sniffer → API replay: when sniffer finds the data API, skip browser entirely on page 2+
- `[ ]` Partial result streaming: yield batches every N items to WebSocket

### Phase 4 — Product (Make It Usable)
- `[ ]` Playground UI: URL + prompt + format selector → live result stream
- `[x]` Schema inference when user provides no schema — `schema_inferencer.py` (heuristic + LLM DOM fallback)
- `[x]` Output format enforcement: CSV column order, JSON arrays, NDJSON, TSV, Excel — `output_formatter.py`
- `[ ]` Proactive drift detection: background worker that periodically validates cached recipes

---

## 6. Performance Baseline (Current)

| Metric | Current State | Target |
|---|---|---|
| LLM calls per job (no recipe) | 3–5 (oracle + planner + extraction fallback) | 1 (planner only, rest math) |
| LLM calls per job (recipe hit) | 0 | 0 |
| Time to first result | 15–45s (browser launch + nav) | <10s (sniffer shortcut) |
| Cloudflare bypass rate | ~40% (minimal stealth) | >85% (full stealth + BYOS) |
| Multi-page accuracy | Degrades after page 1 (no re-plan) | Stable via checkpoint re-plan |
| RAG hit rate | Unknown (stores disconnected) | Target >60% on repeat domains |
