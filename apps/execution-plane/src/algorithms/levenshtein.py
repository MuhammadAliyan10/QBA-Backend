from dataclasses import dataclass, field
from typing import List, Dict, Optional
import Levenshtein

@dataclass
class DOMElement:
    tag_name: str
    text: str
    attributes: Dict[str, str] = field(default_factory=dict)
    is_visible: bool = True

@dataclass
class ScoredElement:
    element: DOMElement
    score: float
    match_reason: str

class LevenshteinScorer:
    def __init__(self):
        self.INTENT_MAP = {
            "login": ["sign in", "log in", "next", "submit", "continue"],
            "search": ["search", "find", "query"],
        }

    def score_elements(self, target_text: str, elements: List[DOMElement]) -> List[ScoredElement]:
        """
        Scores elements based on Levenshtein distance and weighted heuristics.
        """
        target_text = target_text.lower().strip()
        keywords = self.INTENT_MAP.get(target_text, [target_text])
        scored_candidates = []

        for el in elements:
            base_score = 0.0
            best_keyword = ""
            
            # 1. Base Score: Levenshtein Similarity with Text
            el_text = el.text.lower().strip()
            if el_text:
                for kw in keywords:
                    sim = Levenshtein.ratio(el_text, kw)
                    if sim > base_score:
                        base_score = sim
                        best_keyword = kw

            # 2. Weighted Scoring (Bonus Points)
            final_score = base_score
            reasons = []

            if base_score > 0.4:
                reasons.append(f"Text match '{best_keyword}' ({base_score:.2f})")

            # Bonus: Tag Name (+20%)
            if el.tag_name.lower() == 'button':
                final_score += 0.2
                reasons.append("Tag is button (+0.2)")
            
            # Bonus: ID contains target (+30%)
            el_id = el.attributes.get('id', '').lower()
            if any(kw in el_id for kw in keywords):
                final_score += 0.3
                reasons.append("ID match (+0.3)")

            # Bonus: Aria-Label matches intent (+40%)
            aria_label = el.attributes.get('aria-label', '').lower()
            if any(kw in aria_label for kw in keywords):
                final_score += 0.4
                reasons.append("Aria-label match (+0.4)")

            # Cap score at 1.0 (or allow >1.0? User didn't specify, but usually probability is 0-1. 
            # However, weighted scores can exceed 1. Let's cap at 1.0 for consistency with confidence, 
            # OR keep it raw. The user said "Bonus Points", implying addition. 
            # I will cap at 1.0 for the final confidence return, but use raw for sorting.)
            
            if final_score > 0.1: # Only keep relevant ones
                scored_candidates.append(
                    ScoredElement(
                        element=el, 
                        score=min(final_score, 1.0), 
                        match_reason=", ".join(reasons)
                    )
                )

        # Sort by score descending
        scored_candidates.sort(key=lambda x: x.score, reverse=True)
        return scored_candidates

    def find_best_candidate(self, elements: List[DOMElement], intent: str) -> Optional[ScoredElement]:
        candidates = self.score_elements(intent, elements)
        return candidates[0] if candidates else None
