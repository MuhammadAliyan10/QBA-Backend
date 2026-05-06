import re
import logging

logger = logging.getLogger("security")

# Pre-compiled highly optimized Regex patterns for PII detection
# 1. Social Security Numbers (SSN): XXX-XX-XXXX
SSN_REGEX = re.compile(r'\b\d{3}[-.\s]?\d{2}[-.\s]?\d{4}\b')

# 2. Credit Cards (PAN): 13-16 digit standard formats (Visa, MasterCard, Amex)
CC_REGEX = re.compile(
    r'\b(?:\d{4}[ -]?){3}\d{4}\b|'    # 16-digit blocks (e.g., 1234-5678-9012-3456)
    r'\b3[47]\d{13}\b|'               # Amex (15 digits starting with 34 or 37)
    r'\b\d{15,16}\b'                  # Raw 15-16 digit numbers
)

# 3. Email Addresses: Standard RFC 5322 compliant regex
EMAIL_REGEX = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b')

# 4. Phone Numbers: US and standard International formats
PHONE_REGEX = re.compile(r'\b(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}\b')


def sanitize_payload(text: str) -> str:
    """
    Strict zero-trust sanitization layer to prevent PII leakage.
    Intercepts and masks sensitive data patterns with standard placeholders.
    """
    if not text or not isinstance(text, str):
        return text

    scrubbed = text

    # Apply redactions sequentially
    scrubbed = CC_REGEX.sub('[REDACTED_CC]', scrubbed)
    scrubbed = SSN_REGEX.sub('[REDACTED_SSN]', scrubbed)
    scrubbed = EMAIL_REGEX.sub('[REDACTED_EMAIL]', scrubbed)
    scrubbed = PHONE_REGEX.sub('[REDACTED_PHONE]', scrubbed)

    return scrubbed
