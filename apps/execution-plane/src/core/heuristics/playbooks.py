# backend/apps/execution-plane/src/core/heuristics/playbooks.py

from typing import List, Dict, Optional
import logging

logger = logging.getLogger("heuristics")

class BasePlaybook:
    """Base interface for all heuristic interaction signatures."""
    @staticmethod
    def match(action_map: List[Dict]) -> Optional[str]:
        """Returns the qId of the matched element or None."""
        raise NotImplementedError

class SearchPlaybook(BasePlaybook):
    """Heuristics for finding search inputs and triggers."""
    @staticmethod
    def match(action_map: List[Dict]) -> Optional[str]:
        # Rule 1: Direct search input
        for node in action_map:
            if node.get("tag") == "input" and node.get("type") == "search":
                return node.get("qId")

        # Rule 2: Semantic search input
        for node in action_map:
            if node.get("tag") == "input":
                aria_label = (node.get("ariaLabel") or "").lower()
                placeholder = (node.get("placeholder") or "").lower()
                if "search" in aria_label or "search" in placeholder:
                    return node.get("qId")

        # Rule 3: Search trigger button
        for node in action_map:
            if node.get("tag") == "button":
                aria_label = (node.get("ariaLabel") or "").lower()
                if "search" in aria_label:
                    return node.get("qId")

        return None

class FilterPlaybook(BasePlaybook):
    """Heuristics for finding filters, faceted search, and toggles."""
    @staticmethod
    def match(action_map: List[Dict]) -> Optional[str]:
        for node in action_map:
            if node.get("tag") == "select":
                return node.get("qId")

        checkbox_nodes = []
        for node in action_map:
            if node.get("tag") == "input" and node.get("type") == "checkbox":
                if node.get("scrollX", 1000) < 300:
                    checkbox_nodes.append(node)

        if checkbox_nodes:
            return checkbox_nodes[0].get("qId")

        return None

class AuthPlaybook(BasePlaybook):
    """Heuristics for Authentication (Login/Sign-in) flows."""
    @staticmethod
    def match(action_map: List[Dict]) -> Optional[str]:
        # Rule 1: Email/User input
        for node in action_map:
            if node.get("tag") == "input":
                type_attr = (node.get("type") or "").lower()
                name_attr = (node.get("name") or "").lower()
                id_attr = (node.get("id") or "").lower()
                if type_attr == "email" or "user" in name_attr or "email" in name_attr or "user" in id_attr:
                    return node.get("qId")

        # Rule 2: Password input
        for node in action_map:
            if node.get("tag") == "input" and node.get("type") == "password":
                return node.get("qId")

        # Rule 3: Submit button
        for node in action_map:
            if node.get("tag") == "button":
                text = (node.get("text") or "").lower().replace(" ", "")
                if "login" in text or "signin" in text:
                    return node.get("qId")

        return None

class PaginationPlaybook(BasePlaybook):
    """Heuristics for Pagination (Next/Previous)."""
    @staticmethod
    def match(action_map: List[Dict]) -> Optional[str]:
        # Rule 1: Next Page link/button
        for node in action_map:
            if node.get("tag") in ("a", "button"):
                text = (node.get("text") or "").lower()
                aria_label = (node.get("ariaLabel") or "").lower()
                if "next" in text or "next page" in aria_label:
                    return node.get("qId")
        return None

class ModalDismissPlaybook(BasePlaybook):
    """Heuristics for Dismissing Modals, Cookies, and Banners."""
    @staticmethod
    def match(action_map: List[Dict]) -> Optional[str]:
        # Rule 1: Close (X) button
        for node in action_map:
            if node.get("tag") == "button":
                text = (node.get("text") or "")
                aria_label = (node.get("ariaLabel") or "").lower()
                if "close" in aria_label or text.strip() in ("X", "×"):
                    return node.get("qId")

        # Rule 2: Cookie acceptance
        for node in action_map:
            if node.get("tag") == "button":
                text = (node.get("text") or "").lower()
                if "accept all" in text or "allow cookies" in text:
                    return node.get("qId")
        return None

class FormActionPlaybook(BasePlaybook):
    """Heuristics for standard Form actions (Submit, Save, Cancel)."""
    @staticmethod
    def match(action_map: List[Dict]) -> Optional[str]:
        # Rule 1: Submit type button
        for node in action_map:
            if node.get("tag") == "button" and node.get("type") == "submit":
                return node.get("qId")

        # Rule 2: Text-based Save/Submit
        for node in action_map:
            if node.get("tag") == "button":
                text = (node.get("text") or "").lower()
                if "save" in text or "submit" in text:
                    return node.get("qId")
        return None

class EcommercePlaybook(BasePlaybook):
    """Heuristics for E-Commerce (Add to Cart, Checkout)."""
    @staticmethod
    def match(action_map: List[Dict]) -> Optional[str]:
        # Rule 1: Add to Cart
        for node in action_map:
            if node.get("tag") == "button":
                text = (node.get("text") or "").lower()
                if "add to cart" in text:
                    return node.get("qId")

        # Rule 2: Checkout
        for node in action_map:
            if node.get("tag") in ("a", "button"):
                text = (node.get("text") or "").lower()
                if "checkout" in text:
                    return node.get("qId")
        return None
