from dataclasses import dataclass, field
from typing import List, Dict, Optional


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


def normalized_similarity(s1: str, s2: str) -> float:
    s1, s2 = s1.lower().strip(), s2.lower().strip()
    if not s1 or not s2:
        return 0.0
    # Standard Levenshtein Implementation
    if len(s1) < len(s2):
        return normalized_similarity(s2, s1)
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = [current_row[j] + 1]
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    dist = previous_row[-1]
    return 1.0 - (dist / max(len(s1), len(s2)))


class HeuristicScorer:
    def __init__(self):
        self.INTENT_MAP = {
            "login": ["sign in", "log in", "next", "submit", "continue"],
            "search": ["search", "find", "query"],
        }

    def find_best_candidate(
        self, elements: List[DOMElement], intent: str
    ) -> Optional[ScoredElement]:
        keywords = self.INTENT_MAP.get(intent.lower(), [intent])
        candidates = []

        for el in elements:
            score = 0.0
            for kw in keywords:
                sim = normalized_similarity(el.text, kw)
                if sim > score:
                    score = sim
            eid = el.attributes.get("id", "").lower()
            if any(kw in eid for kw in keywords):
                score += 0.3

            if score > 0.4:
                candidates.append(
                    ScoredElement(el, min(score, 1.0), f"Text: {el.text}")
                )
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates[0] if candidates else None
