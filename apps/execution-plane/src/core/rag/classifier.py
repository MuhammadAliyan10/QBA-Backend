"""
classifier.py - Scout URL Classifier

Uses GPT-4o-mini to classify websites into categories for better
template matching and recipe generation.

Returns:
    {
        "category": "E-Commerce",
        "platform": "Shopify",
        "complexity": "High"
    }

Author: Quanta Box Paradox Engineering
Version: 1.0.0
"""

import os
import json
import logging
from typing import Dict, Optional
from dataclasses import dataclass

import openai

logger = logging.getLogger("classifier")


# =============================================================================
# CLASSIFICATION RESULT
# =============================================================================

@dataclass
class ClassificationResult:
    """Result of URL classification."""
    category: str  # ecommerce, social, banking, news, saas, portal
    platform: str  # Shopify, WordPress, React, Custom, etc.
    complexity: str  # Low, Medium, High
    confidence: float  # 0-1
    features: Dict[str, bool]  # auth_required, captcha_likely, etc.

    def to_dict(self) -> Dict:
        return {
            "category": self.category,
            "platform": self.platform,
            "complexity": self.complexity,
            "confidence": self.confidence,
            "features": self.features
        }


# =============================================================================
# KNOWN PATTERNS (Fast Path)
# =============================================================================

KNOWN_DOMAINS = {
    # E-Commerce
    "amazon.com": ("ecommerce", "Amazon", "High"),
    "ebay.com": ("ecommerce", "eBay", "High"),
    "shopify.com": ("ecommerce", "Shopify", "Medium"),
    "etsy.com": ("ecommerce", "Etsy", "Medium"),
    "walmart.com": ("ecommerce", "Walmart", "High"),

    # Social
    "linkedin.com": ("social", "LinkedIn", "High"),
    "twitter.com": ("social", "Twitter", "Medium"),
    "x.com": ("social", "Twitter", "Medium"),
    "facebook.com": ("social", "Facebook", "High"),
    "instagram.com": ("social", "Instagram", "High"),

    # Banking/Finance
    "chase.com": ("banking", "Chase", "High"),
    "bankofamerica.com": ("banking", "BankOfAmerica", "High"),
    "paypal.com": ("banking", "PayPal", "High"),

    # News
    "nytimes.com": ("news", "NYTimes", "Low"),
    "bbc.com": ("news", "BBC", "Low"),
    "cnn.com": ("news", "CNN", "Low"),

    # SaaS
    "notion.so": ("saas", "Notion", "Medium"),
    "slack.com": ("saas", "Slack", "Medium"),
    "salesforce.com": ("saas", "Salesforce", "High"),
    "hubspot.com": ("saas", "HubSpot", "Medium"),
}


# =============================================================================
# CLASSIFIER
# =============================================================================

class URLClassifier:
    """
    Scout Classifier for URL categorization.

    Two-layer approach:
    1. Fast Path: Known domain lookup (instant)
    2. AI Path: GPT-4o-mini classification (when needed)
    """

    def __init__(self, api_key: str = None):
        """Initialize classifier."""
        api_key = api_key or os.getenv("OPENAI_API_KEY")
        if api_key:
            self.client = openai.OpenAI(api_key=api_key)
        else:
            self.client = None
            logger.warning("[Classifier] OpenAI not configured")

    async def classify(
        self,
        url: str,
        html_meta: Optional[Dict] = None
    ) -> ClassificationResult:
        """
        Classify a URL.

        Args:
            url: Target URL
            html_meta: Optional metadata from page (title, meta tags)

        Returns:
            ClassificationResult
        """
        from urllib.parse import urlparse

        # Extract domain
        parsed = urlparse(url)
        domain = parsed.netloc.replace("www.", "")

        # Fast Path: Known domain
        if domain in KNOWN_DOMAINS:
            cat, plat, comp = KNOWN_DOMAINS[domain]
            logger.info(f"[Classifier] Fast path: {domain} -> {cat}")
            return ClassificationResult(
                category=cat,
                platform=plat,
                complexity=comp,
                confidence=0.95,
                features=self._infer_features(cat, comp)
            )

        # Check subdomain patterns
        for known_domain, values in KNOWN_DOMAINS.items():
            if domain.endswith(known_domain):
                cat, plat, comp = values
                return ClassificationResult(
                    category=cat,
                    platform=plat,
                    complexity=comp,
                    confidence=0.90,
                    features=self._infer_features(cat, comp)
                )

        # AI Path: Use GPT-4o-mini
        if self.client:
            return await self._ai_classify(url, domain, html_meta)

        # Fallback: Generic classification
        logger.warning(f"[Classifier] Unknown domain: {domain}, using generic")
        return ClassificationResult(
            category="portal",
            platform="Unknown",
            complexity="Medium",
            confidence=0.50,
            features={"auth_required": True, "captcha_likely": False}
        )

    async def _ai_classify(
        self,
        url: str,
        domain: str,
        html_meta: Optional[Dict]
    ) -> ClassificationResult:
        """Use GPT-4o-mini to classify unknown domains."""

        meta_context = ""
        if html_meta:
            meta_context = f"""
Page Title: {html_meta.get('title', 'Unknown')}
Meta Description: {html_meta.get('description', 'None')}
Technologies: {html_meta.get('technologies', [])}
"""

        prompt = f"""Classify this website for browser automation:

URL: {url}
Domain: {domain}
{meta_context}

Respond in JSON format:
{{
    "category": "ecommerce|social|banking|news|saas|portal|government|entertainment",
    "platform": "Shopify|WordPress|React|Angular|Custom|Unknown",
    "complexity": "Low|Medium|High",
    "auth_required": true|false,
    "captcha_likely": true|false,
    "has_anti_bot": true|false
}}

Only output valid JSON, no explanation."""

        try:
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=200
            )

            content = response.choices[0].message.content.strip()
            data = json.loads(content)

            logger.info(f"[Classifier] AI classified: {domain} -> {data.get('category')}")

            return ClassificationResult(
                category=data.get("category", "portal"),
                platform=data.get("platform", "Unknown"),
                complexity=data.get("complexity", "Medium"),
                confidence=0.85,
                features={
                    "auth_required": data.get("auth_required", True),
                    "captcha_likely": data.get("captcha_likely", False),
                    "has_anti_bot": data.get("has_anti_bot", False)
                }
            )

        except Exception as e:
            logger.error(f"[Classifier] AI classification failed: {e}")
            return ClassificationResult(
                category="portal",
                platform="Unknown",
                complexity="Medium",
                confidence=0.30,
                features={"auth_required": True, "captcha_likely": False}
            )

    def _infer_features(self, category: str, complexity: str) -> Dict[str, bool]:
        """Infer common features based on category."""
        return {
            "auth_required": category in ("banking", "social", "saas"),
            "captcha_likely": category in ("banking", "ecommerce") and complexity == "High",
            "has_anti_bot": complexity == "High"
        }


# =============================================================================
# CONVENIENCE FUNCTION
# =============================================================================

async def classify_url(url: str, html_meta: Optional[Dict] = None) -> Dict:
    """
    Convenience function for URL classification.

    Args:
        url: Target URL
        html_meta: Optional page metadata

    Returns:
        Classification dict
    """
    classifier = URLClassifier()
    result = await classifier.classify(url, html_meta)
    return result.to_dict()
