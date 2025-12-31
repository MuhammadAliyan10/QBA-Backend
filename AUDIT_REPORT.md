# 🔴 SYSTEM ARCHITECTURE AUDIT REPORT

**Date:** 2025-12-26
**Auditor:** Principal AI Engineer
**Verdict:** ⚠️ **CRITICAL GAPS FOUND**

---

## Executive Summary

The Quanta platform has **excellent component architecture** but suffers from **integration disconnects**. Individual modules are well-designed, but they don't fully connect to each other.

| Area                   | Status        | Severity |
| ---------------------- | ------------- | -------- |
| Preflight Pipeline     | ✅ Built      | -        |
| RecipeEngine           | ✅ Built      | -        |
| Activities Integration | 🔴 **BROKEN** | CRITICAL |
| RAG Learning Hook      | 🟡 Not Wired  | HIGH     |
| Frontend API Route     | 🟡 Missing    | HIGH     |
| Planner AI             | 🟡 Stub/Mock  | MEDIUM   |
| Error Taxonomy         | 🟡 Incomplete | MEDIUM   |

---

## 🔴 CRITICAL ISSUES

### Issue 1: Preflight Output Not Consumed

**The Problem:**

- `preflight.py` outputs `Recipe Schema v2.0` (nodes, edges, DAG structure)
- `activities.py` expects OLD format: `steps: [{action, params}]`
- **These are incompatible formats. The preflight output is never used.**

```python
# preflight.py outputs:
{
    "nodes": [...],
    "edges": [...],
    "entry_point": "node_login"
}

# activities.py expects:
{
    "steps": [
        {"action": "GOTO", "params": {"url": "..."}},
        {"action": "CLICK", "params": {"intent": "..."}}
    ]
}
```

**Impact:** The entire preflight pipeline is dead code. It generates hardened recipes that are never executed.

**Fix Required:**

```python
# Option A: Update activities.py to use RecipeEngine
from core.recipe.recipeEngine import RecipeEngine

async def browser_automation_activity(payload: dict):
    recipe = payload.get("recipe")  # From preflight
    engine = RecipeEngine(job_id=payload["job_id"])
    await engine.load_recipe(recipe)
    return await engine.run()

# Option B: Add converter in preflight
def recipe_to_steps(recipe: dict) -> List[dict]:
    """Convert Recipe Schema v2.0 to legacy steps format"""
    steps = []
    for node in recipe["nodes"]:
        for action in node.get("actions", []):
            steps.append({
                "action": action["type"].upper(),
                "params": {...}
            })
    return steps
```

---

### Issue 2: RAG Learning Hook Not Wired

**The Problem:**

- `RAGService.save_template()` exists but is never called
- When a job completes successfully, we don't save the recipe
- **The system never learns from experience**

**Where it should be:**

```python
# In activities.py, after successful execution:
if workflow_succeeded:
    from core.rag import get_rag_service
    await get_rag_service().save_template(
        recipe_json=recipe,
        category=classification.category,
        domain=target_domain,
        task_type=workflow_id
    )
```

---

### Issue 3: No API Route for Preflight

**The Problem:**

- `preflight.py` has `handle_preflight_request()` but no route
- Frontend cannot call the preflight pipeline
- No Flask/FastAPI endpoint exists

**Fix Required:**

```python
# apps/control-plane/cmd/server/main.go
// Add route:
// POST /api/engine/preflight

# OR apps/execution-plane:
from fastapi import FastAPI
from core.rag.preflight import handle_preflight_request

app = FastAPI()

@app.post("/api/engine/preflight")
async def preflight_endpoint(payload: dict):
    return await handle_preflight_request(payload)
```

---

## 🟡 HIGH PRIORITY ISSUES

### Issue 4: PlannerAI is a Stub

**The Problem:**

- `preflight.py._generate_recipe()` returns a minimal hardcoded recipe
- No actual LLM integration for recipe generation
- Comment says `# TODO: Integrate with actual PlannerAI`

**Current Code:**

```python
async def _generate_recipe(self, prompt, url, classification):
    # Returns hardcoded minimal recipe
    return {
        "nodes": [{"id": "node_navigate", ...}],
        ...
    }
```

**Fix Required:**

- Integrate with OpenAI/Anthropic for recipe generation
- Use classification context in prompt
- Include retrieved similar templates as few-shot examples

---

### Issue 5: Two Execution Paths Exist

**The Problem:**

- `activities.py` has its own execution loop (step-based)
- `recipeEngine.py` has a full DAG execution engine
- **They are completely separate systems**

| Component         | Uses           | Format      |
| ----------------- | -------------- | ----------- |
| `activities.py`   | `steps[]` loop | Legacy      |
| `recipeEngine.py` | DAG traversal  | Schema v2.0 |

**Recommendation:**

- Deprecate the `steps[]` format in `activities.py`
- Use `RecipeEngine` for all execution
- `activities.py` becomes a thin wrapper

---

### Issue 6: SmartFinder Has Two Versions

**The Problem:**

- `core/SmartFinder.py` exists (old version, used by activities)
- `core/selector/smartFinder.py` exists (new version with 4 layers)
- **activities.py imports the OLD one**

```python
# activities.py line 19:
from core.SmartFinder import SmartFinder  # OLD

# Should be:
from core.selector.smartFinder import SmartFinder  # NEW
```

---

## 🟢 MEDIUM PRIORITY ISSUES

### Issue 7: Justifier Doesn't Use SmartFinder

**The Problem:**

- `justifier.py` has its own element verification logic
- It should reuse `SmartFinder` from `selector/smartFinder.py`
- Duplicated Levenshtein logic

**Current:**

```python
# justifier.py - custom implementation
async def _verify_with_math(self, intent):
    for element in elements:
        score = levenshtein_ratio(intent_normalized, combined_normalized)
```

**Should Be:**

```python
# Reuse SmartFinder
from core.selector.smartFinder import SmartFinder
finder = SmartFinder(page)
result = await finder.find(intent)
return (result.selector, result.confidence)
```

---

### Issue 8: No Recipe Versioning

**The Problem:**

- Templates in `recipe_templates` have no version field
- If Recipe Schema changes from v2.0 to v2.1, old templates break
- No migration path

**Fix:**

```sql
ALTER TABLE recipe_templates
ADD COLUMN schema_version TEXT DEFAULT '2.0.0';
```

---

### Issue 9: Classification Not Persisted

**The Problem:**

- `URLClassifier.classify()` is called during preflight
- Result is used but not saved
- On next request to same URL, we re-classify (wasted API call)

**Fix:**

```sql
CREATE TABLE url_classifications (
    domain TEXT PRIMARY KEY,
    category TEXT,
    platform TEXT,
    complexity TEXT,
    classified_at TIMESTAMP DEFAULT NOW()
);
```

---

### Issue 10: No Health Check for Services

**The Problem:**

- RAGService depends on database + OpenAI
- Justifier depends on Playwright
- No startup health checks
- Failures happen at runtime, not startup

**Fix:**

```python
class RAGService:
    async def health_check(self) -> dict:
        return {
            "database": await self._check_db(),
            "embeddings": await self._check_openai(),
            "vector_search": await self._check_pgvector()
        }
```

---

## 📊 Missing Functionality Matrix

| Feature              | Preflight | RecipeEngine | Activities    | Status           |
| -------------------- | --------- | ------------ | ------------- | ---------------- |
| Recipe Generation    | ❌ Stub   | N/A          | N/A           | **BROKEN**       |
| Static Validation    | ✅        | ✅           | ❌            | Partial          |
| Browser Verification | ✅        | N/A          | N/A           | OK               |
| DAG Execution        | N/A       | ✅           | ❌ Old format | **DISCONNECT**   |
| SmartFinder 4-Layer  | N/A       | ✅           | ❌ Uses old   | **WRONG IMPORT** |
| Checkpointing        | N/A       | ✅           | ❌            | Not wired        |
| RAG Template Save    | ✅ Code   | N/A          | ❌ Not called | **NOT WIRED**    |
| NATS Events          | N/A       | ✅           | ✅            | OK               |

---

## 🛠️ Recommended Fix Priority

### Phase 1: Critical (This Week)

1. **Wire preflight to activities** - Make activities.py consume Recipe Schema v2.0
2. **Add preflight API route** - Flask/FastAPI endpoint
3. **Fix SmartFinder import** - Use new 4-layer version

### Phase 2: High (Next Sprint)

4. **Wire RAG learning hook** - Save successful recipes
5. **Implement PlannerAI** - Real LLM recipe generation
6. **Cache URL classifications** - Persist to DB

### Phase 3: Medium (Backlog)

7. **Unify SmartFinder usage** - Justifier should reuse it
8. **Add recipe versioning** - Schema migration support
9. **Add health checks** - Startup validation

---

## 🧪 Integration Test Checklist

```bash
# These tests should pass after fixes:

[ ] POST /api/engine/preflight returns hardened recipe
[ ] Recipe Schema v2.0 executes via RecipeEngine
[ ] Successful job triggers RAG save
[ ] SmartFinder 4-layer is used in execution
[ ] Classification is cached for repeat URLs
[ ] Vision fallback only triggers when Math < 80%
```

---

## Conclusion

The individual components are **well-engineered** but not **connected**. The system is like a car with a great engine, great wheels, and great steering - but no transmission connecting them.

**Estimated effort to fix critical issues:** 2-3 days
**Risk if not fixed:** Preflight pipeline is completely unused, zero learning capability
