import logging
import numpy as np
import faiss
from sentence_transformers import SentenceTransformer
from playwright.async_api import ElementHandle, Page

logger = logging.getLogger("healer")

class TheHealer:
    """
    The Zero-Cost Self-Healing Engine.
    Uses Local Vector Embeddings (FAISS) instead of OpenAI.
    """
    _model = None

    def __init__(self):
        # Load a tiny, fast model (80MB) onto the CPU.
        # This runs LOCALLY. No API cost.
        if TheHealer._model is None:
            logger.info("🧠 Loading Local Embedding Model (all-MiniLM-L6-v2)...")
            TheHealer._model = SentenceTransformer('all-MiniLM-L6-v2')

        self.model = TheHealer._model
        # Dimension of MiniLM is 384
        self.index = faiss.IndexFlatL2(384)
        self.stored_elements = []

    async def fingerprint_element(self, element: ElementHandle) -> str:
        """
        Creates a 'Semantic String' representing the element.
        """
        text = await element.inner_text() or ""
        aria = await element.get_attribute("aria-label") or ""
        tag = await element.evaluate("el => el.tagName.toLowerCase()")
        eid = await element.get_attribute("id") or ""
        classes = await element.get_attribute("class") or ""

        # We combine these features into one string
        # e.g. "button search submit icon-btn"
        fingerprint = f"{tag} {text} {aria} {eid} {classes}".strip().lower()
        return fingerprint

    def vectorize(self, text: str) -> np.ndarray:
        """
        Converts text to a Math Vector (384 float numbers).
        """
        return self.model.encode([text])[0]

    async def scan_page_into_memory(self, page: Page, candidates: list[ElementHandle]):
        """
        Takes all buttons on the page and saves them into the Vector Index.
        """
        logger.info(f"🧠 Healer: Memorizing {len(candidates)} elements...")
        self.index.reset() # Clear old memory
        self.stored_elements = candidates

        fingerprints = []
        for el in candidates:
            fp = await self.fingerprint_element(el)
            fingerprints.append(fp)

        # Convert all strings to Vectors
        vectors = self.model.encode(fingerprints)

        # Add to FAISS Index
        self.index.add(vectors)

    async def heal(self, missing_intent: str) -> ElementHandle:
        """
        The Magic: Finds the closest match to the 'Intent' purely via Math.
        """
        # 1. Convert "Search Button" to a Vector
        intent_vector = self.model.encode([missing_intent])

        # 2. Search FAISS for the nearest neighbor
        # k=1 means "Give me the single best match"
        distances, indices = self.index.search(intent_vector, k=1)

        best_index = indices[0][0]
        distance = distances[0][0]

        # 3. Threshold Check (Lower distance = Better match)
        # Distance > 1.5 usually means "Not found"
        if best_index != -1 and distance < 1.5:
            logger.info(f"❤️ Healer: Found match via Vector Math! (Dist: {distance:.4f})")
            return self.stored_elements[best_index]

        logger.warning(f"💔 Healer: No mathematical match found (Best Dist: {distance:.4f})")
        return None
