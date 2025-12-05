import Levenshtein

class LevenshteinScorer:
    """
    Calculates semantic distance between User Intent and DOM Elements.
    """

    def __init__(self):
        # A tiny "Pocket Dictionary" of common web terms
        self.SYNONYMS = {
            "login": ["sign in", "log in", "enter", "auth"],
            "buy": ["purchase", "checkout", "add to cart", "order"],
            "submit": ["send", "post", "apply", "go", "search"],
            "download": ["get", "save", "export", "pdf"]
        }

    def score(self, user_intent: str, element_text: str) -> float:
        """
        Returns a score from 0.0 to 1.0.
        1.0 = Perfect Match.
        """
        intent = user_intent.lower().strip()
        text = element_text.lower().strip()

        if not text:
            return 0.0

        # 1. Direct Match Check
        if intent in text or text in intent:
            return 1.0

        # 2. Synonym Check (The "Smart" Layer)
        # If user says "Login", we check if text is "Sign In"
        if intent in self.SYNONYMS:
            for syn in self.SYNONYMS[intent]:
                if syn in text:
                    return 0.95

        # 3. Levenshtein Ratio (The "Fuzzy" Layer)
        # Calculates edit distance ratio
        return Levenshtein.ratio(intent, text)
