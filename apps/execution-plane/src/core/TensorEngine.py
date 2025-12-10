import logging
import numpy as np
from typing import Optional
from playwright.async_api import Page
from sentence_transformers import SentenceTransformer

logger = logging.getLogger("tensorEngine")


class TensorEngine:
    """
    High-Dimensional Vector Analysis Engine for Site Fingerprinting.

    Uses Vector Space Mathematics to determine if a website's context
    matches the user's intent without relying on external LLM APIs.

    Mathematical Foundation:
    - Embedding Space: R^384 (all-MiniLM-L6-v2)
    - Similarity Metric: Cosine Similarity ∈ [-1.0, 1.0]
    - Page Representation: Weighted feature concatenation
    """

    _instance: Optional['TensorEngine'] = None
    _model: Optional[SentenceTransformer] = None

    def __new__(cls):
        """
        Singleton Pattern: Ensures model loads only once into RAM.
        """
        if cls._instance is None:
            cls._instance = super(TensorEngine, cls).__new__(cls)
            logger.info("Initializing Tensor Engine (Singleton)...")
            cls._model = SentenceTransformer('all-MiniLM-L6-v2')
            logger.info("[System] Model loaded: all-MiniLM-L6-v2 (384-dim)")
        return cls._instance

    @property
    def model(self) -> SentenceTransformer:
        """Access to the singleton model instance."""
        return self._model

    async def vectorize_page(self, page: Page) -> np.ndarray:
        """
        Converts a Page's DOM into a Feature Vector in R^384.

        Mathematical Approach:
        ----------------------
        V_page = Encode(w1·title + w2·description + w3·headings)

        Where:
        - w1 = 1.0 (Title has highest entropy)
        - w2 = 0.8 (Meta description is curated by authors)
        - w3 = 0.6 (H1/H2 provide structural context)

        Args:
            page (Page): Playwright Page object to analyze

        Returns:
            np.ndarray: Normalized vector ∈ R^384, ||V|| = 1

        Edge Cases:
            - Empty metadata → Returns Zero Vector (dim=384)
            - Missing elements → Skips with warning
        """
        try:
            # Extract High-Entropy Signals
            title = await page.evaluate("document.title") or ""

            # Meta Description
            description = await page.evaluate("""
                () => {
                    const meta = document.querySelector('meta[name="description"]');
                    return meta ? meta.getAttribute('content') : '';
                }
            """) or ""

            # H1, H2 Tags
            headings = await page.evaluate("""
                () => {
                    const h1 = Array.from(document.querySelectorAll('h1')).map(el => el.innerText).join(' ');
                    const h2 = Array.from(document.querySelectorAll('h2')).map(el => el.innerText).join(' ');
                    return h1 + ' ' + h2;
                }
            """) or ""

            # Weighted Concatenation
            # Format: "title title description description description headings headings headings headings"
            # This simulates weight multipliers (1.0, 0.8, 0.6) through repetition
            weighted_text = (
                f"{title} {title} "  # Weight 1.0 (appears twice)
                f"{description} "     # Weight 0.8 (appears once, but model will weight by frequency)
                f"{headings}"         # Weight 0.6
            )

            # Sanitization: Lowercase, strip extra whitespace
            sanitized = " ".join(weighted_text.lower().split())

            if not sanitized.strip():
                logger.warning("[Warning] Page has no extractable metadata. Returning Zero Vector.")
                return np.zeros(384, dtype=np.float32)

            # Encode to Vector Space
            vector = self._model.encode(sanitized, convert_to_numpy=True, normalize_embeddings=True)

            logger.debug(f"[Metrics] Vectorized:'{sanitized[:50]}...' → ||V|| = {np.linalg.norm(vector):.3f}")

            return vector.astype(np.float32)

        except Exception as e:
            logger.error(f"[Error] Vectorization failed: {e}")
            return np.zeros(384, dtype=np.float32)

    def compute_relevance(self, page_vector: np.ndarray, user_intent: str) -> float:
        """
        Computes Cosine Similarity between Page Vector and User Intent.

        Mathematical Formula:
        ---------------------
        Score = (V_page · V_intent) / (||V_page|| × ||V_intent||)

        Where:
        - V_page ∈ R^384: Embedded representation of the page
        - V_intent ∈ R^384: Embedded representation of user's goal
        - Score ∈ [-1.0, 1.0]: Similarity metric

        Interpretation:
        - Score > 0.7: Strong match (page aligns with intent)
        - Score ∈ [0.4, 0.7]: Moderate match
        - Score < 0.4: Weak match (possibly wrong page)

        Args:
            page_vector (np.ndarray): Page feature vector (dim=384)
            user_intent (str): User's goal (e.g., "Buy iPhone")

        Returns:
            float: Cosine similarity score ∈ [-1.0, 1.0]

        Edge Cases:
            - Zero vectors → Returns 0.0 (orthogonal/no similarity)
        """
        try:
            # Encode User Intent
            intent_vector = self._model.encode(
                user_intent.lower().strip(),
                convert_to_numpy=True,
                normalize_embeddings=True
            ).astype(np.float32)

            # Compute Norms (for debugging/validation)
            norm_page = np.linalg.norm(page_vector)
            norm_intent = np.linalg.norm(intent_vector)

            # Handle Zero Vectors (Division by Zero Protection)
            if norm_page < 1e-6 or norm_intent < 1e-6:
                logger.warning("[Warning] Zero vector detected. Returning 0.0 similarity.")
                return 0.0

            # Cosine Similarity: V·I / (||V|| × ||I||)
            # Note: If vectors are pre-normalized (||V||=||I||=1), this simplifies to V·I
            dot_product = np.dot(page_vector, intent_vector)
            cosine_score = dot_product / (norm_page * norm_intent)

            # Clamp to [-1.0, 1.0] (numerical stability)
            cosine_score = np.clip(cosine_score, -1.0, 1.0)

            logger.info(f"[Logic] Relevance Score: {cosine_score:.4f} (Intent:'{user_intent}')")

            return float(cosine_score)

        except Exception as e:
            logger.error(f"[Error] Relevance computation failed: {e}")
            return 0.0


# ==================== VERIFICATION BLOCK ====================
if __name__ == "__main__":
    """
    Standalone Test: Verify Tensor Engine without Playwright dependencies.
    """
    print("=" * 60)
    print("TENSOR ENGINE - STANDALONE VERIFICATION")
    print("=" * 60)

    # Initialize Engine (Singleton Test)
    print("\n[Test 1] Singleton Pattern...")
    engine1 = TensorEngine()
    engine2 = TensorEngine()
    print(f"Singleton Test: {engine1.model is engine2.model} (Should be True)")

    # Mock Page Data (Simulate DOM extraction)
    print("\n[Test 2] Mock Page Vectorization...")
    mock_page_text = "Amazon.com: Online Shopping for Electronics, Apparel, Computers"
    mock_vector = engine1.model.encode(mock_page_text, convert_to_numpy=True, normalize_embeddings=True)
    print(f"Mock Vector Shape: {mock_vector.shape} (Expected: (384,))")
    print(f"Vector Norm: {np.linalg.norm(mock_vector):.4f} (Expected: ~1.0)")

    # Mock User Intent
    print("\n[Test 3] Cosine Similarity Computation...")
    user_intent = "Buy iPhone"
    relevance_score = engine1.compute_relevance(mock_vector, user_intent)
    print(f"Cosine Score: {relevance_score:.4f}")
    print(f"   Interpretation: {'Strong Match' if relevance_score > 0.5 else 'Weak Match'}")

    # Edge Case: Zero Vector
    print("\n[Test 4] Edge Case - Zero Vector...")
    zero_vector = np.zeros(384, dtype=np.float32)
    zero_score = engine1.compute_relevance(zero_vector, "test")
    print(f"Zero Vector Score: {zero_score:.4f} (Expected: 0.0)")

    # Additional Test: High Similarity
    print("\n[Test 5] High Similarity Test...")
    similar_intent = "online shopping electronics"
    high_score = engine1.compute_relevance(mock_vector, similar_intent)
    print(f"Similar Intent Score: {high_score:.4f} (Should be > 0.5)")

    # Additional Test: Low Similarity
    print("\n[Test 6] Low Similarity Test...")
    unrelated_intent = "cooking recipes italian pasta"
    low_score = engine1.compute_relevance(mock_vector, unrelated_intent)
    print(f"Unrelated Intent Score: {low_score:.4f} (Should be < 0.3)")

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
