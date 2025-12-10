# 🧠 Core Logic & Algorithms

### _Inside the "Glass Box" Engine_

This document reveals the proprietary algorithms that make the e2e-Platform "smart." We don't use fragile XPaths. We use **Neuro-Symbolic AI**.

---

## 📚 Key Libraries

| Library                  | Purpose             | Why we chose it                                                                    |
| ------------------------ | ------------------- | ---------------------------------------------------------------------------------- |
| **Playwright**           | Browser Automation  | Faster and more reliable than Selenium. Supports modern web features (Shadow DOM). |
| **SentenceTransformers** | Vector Embeddings   | Runs locally (no OpenAI cost). High-performance semantic understanding.            |
| **Faiss**                | Vector Search       | Facebook's library for efficient similarity search.                                |
| **Simhash**              | Page Fingerprinting | Google's algorithm for detecting near-duplicate pages/structures.                  |
| **NATS.py**              | Messaging           | Ultra-low latency communication with the Control Plane.                            |

---

## 🤖 The SmartFinder Algorithm

The `SmartFinder` is the heart of the system. When you ask it to find "the search button", it doesn't just look for `id="search"`. It uses a **Hybrid Scoring System**.

### The Hybrid Scorer

We calculate a final confidence score (0.0 - 1.0) based on 5 signals:

1.  **Exact Match (Weight: 1.0)**

    - Does the text match exactly?
    - _Example_: `<button>Search</button>` matches "Search".

2.  **Levenshtein Distance (Weight: 0.8)**

    - Fuzzy string matching for typos or variations.
    - _Example_: "Submit" matches "Sumit" (typo).

3.  **Word Overlap (Jaccard) (Weight: 0.6)**

    - Do they share key words?
    - _Example_: "Add to Cart" matches "Add Item to Cart".

4.  **Vector Cosine Similarity (Weight: 0.9)**

    - **The "Brain"**. Uses BERT embeddings to understand meaning.
    - _Example_: "Purchase" matches "Buy Now" (Semantic match, no shared words).

5.  **N-Gram Overlap (Weight: 0.5)**
    - Character-level pattern matching.

### The Formula

$$ Score = \frac{\sum (Weight_i \times Score_i)}{\sum Weight_i} $$

If the final score is **> 0.65** (Threshold), we click it.

---

## 🧠 Intent Expander (The "Common Sense" Module)

Users often give vague instructions. The `IntentExpander` translates vague user intent into specific technical terms **without calling an external LLM**.

It uses internal knowledge bases:

- **Role Mapping**: "Person who runs the company" → `["CEO", "Founder", "President"]`
- **Action Synonyms**: "Save" → `["Submit", "Update", "Confirm", "Apply"]`
- **Content Patterns**: "Email" → `input[type='email']`

**Example Flow**:

1.  **User says**: "Find the boss"
2.  **Expander**: Expands to `["CEO", "Chief Executive Officer", "Founder"]`
3.  **SmartFinder**: Scans page for all these terms.
4.  **Result**: Finds "Jensen Huang, CEO".

---

## 🛡️ The Nervous System (Telemetry)

We don't just log text. We emit structured events.

- **`NervousSystem.publish_update()`**:
  - Sends a Protobuf/JSON message to NATS.
  - Includes: `Job ID`, `Timestamp`, `Status` (RUNNING/FAILED), `Message`.
  - This decouples the worker from the API. The worker fires and forgets; the API listens and broadcasts.

---

## 🔮 Self-Healing (The Healer)

If an element is not found, the system doesn't crash immediately.

1.  **Snapshot**: It takes a DOM snapshot.
2.  **Analyze**: It looks for elements that _used_ to work (using `PatternDB`).
3.  **Heal**: If it finds a similar element (e.g., ID changed from `#btn-1` to `#btn-2` but text is same), it updates the recipe automatically.
    _(Note: Full self-healing persistence is in the roadmap)_
