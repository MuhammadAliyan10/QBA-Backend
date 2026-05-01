# backend/apps/execution-plane/src/core/heuristics/router.py

from typing import List, Dict, Optional
import logging
from .playbooks import (
    SearchPlaybook,
    FilterPlaybook,
    AuthPlaybook,
    PaginationPlaybook,
    ModalDismissPlaybook,
    FormActionPlaybook,
    EcommercePlaybook
)

logger = logging.getLogger("heuristics")

# Universal mapping of intents to their deterministic playbooks
INTENT_MAP = {
    "search": SearchPlaybook,
    "find": SearchPlaybook,
    "filter": FilterPlaybook,
    "sort": FilterPlaybook,
    "login": AuthPlaybook,
    "sign in": AuthPlaybook,
    "auth": AuthPlaybook,
    "next page": PaginationPlaybook,
    "paginate": PaginationPlaybook,
    "close": ModalDismissPlaybook,
    "dismiss": ModalDismissPlaybook,
    "accept cookies": ModalDismissPlaybook,
    "submit": FormActionPlaybook,
    "save": FormActionPlaybook,
    "cancel": FormActionPlaybook,
    "add to cart": EcommercePlaybook,
    "checkout": EcommercePlaybook
}

def evaluate_heuristics(intent: str, action_map: List[Dict]) -> Optional[Dict]:
    """
    Attempts to match the user's intent to a deterministic playbook.

    Returns:
        Execution instruction dict if a heuristic matches, else None.
    """
    intent_lower = intent.lower()

    # 1. Routing Logic (The Detective)
    target_id = None
    playbook_cls = None

    # Find the longest matching key in INTENT_MAP for precision
    for key in sorted(INTENT_MAP.keys(), key=len, reverse=True):
        if key in intent_lower:
            playbook_cls = INTENT_MAP[key]
            break

    if playbook_cls:
        target_id = playbook_cls.match(action_map)

    # 2. Execution Instruction Generation
    if target_id:
        # Determine likely action based on the playbook
        suggested_action = "click"
        if playbook_cls == SearchPlaybook or (playbook_cls == AuthPlaybook and any(k in intent_lower for k in ("user", "email"))):
            suggested_action = "type"

        logger.info(f"[Heuristics] Match found for intent '{intent}' using {playbook_cls.__name__}: {target_id}")
        return {
            "action": suggested_action,
            "target_id": target_id,
            "status": "success",
            "source": "heuristic"
        }

    logger.debug(f"[Heuristics] No match found for intent: {intent}")
    return None
