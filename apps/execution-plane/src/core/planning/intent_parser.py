"""
intentParser.py — Rule-Based Natural Language Intent Parser

PURPOSE:
  Converts a plain English automation goal into an ordered list of
  Intent objects without any LLM call. Zero API cost.

APPROACH:
  - Splits the prompt on sentence/clause boundaries.
  - Matches action verbs against known action categories (click, type, scrape…).
  - Extracts the target noun phrase and optional typed value.
  - Detects qualifiers (first, second, last…) for element ranking.
  - Always prepends a NAVIGATE intent for the target URL.

EDGE CASES HANDLED:
  - Quoted values: type "search term" → value = "search term"
  - Compound sentences joined by "then", "and", "after"
  - Implicit navigate: if user says "go to X" we capture it
  - Scrape with target: "get the title" → action=scrape, target=title
  - Wait intents: "wait 3 seconds" → action=wait, value="3000"
  - Select/dropdown: "select Premium Plan" → action=select

LIMITATIONS (by design — AI handles these):
  - Multi-conditional logic ("if X then click Y else click Z") → LLM
  - Deeply ambiguous targets ("click it") → LLM
"""

import re
import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger("intentParser")


# ─── DATA CLASS ───────────────────────────────────────────────────────────────

@dataclass
class Intent:
    """
    Represents a single automation step parsed from natural language.
    One Intent → one WorkflowNode in the final output.
    """
    stepNumber: int
    action: str              # click | type | scrape | navigate | wait | select | scroll | check
    targetDescription: str   # "search bar", "login button", "username field" …
    value: Optional[str]     # Text to type, URL to navigate to, wait duration
    qualifier: Optional[str] # "first" | "second" | "last" | "all" | "nth:<n>"
    rawSentence: str         # Original sentence (for debugging / LLM fallback)
    confidence: float = 1.0  # Parser confidence — low-confidence passes to LLM

    def requiresElementMatch(self) -> bool:
        """Returns True for actions that need a DOM element."""
        return self.action in ("click", "type", "scrape", "select", "check", "hover")

    def isNavigational(self) -> bool:
        """Returns True for actions that don't need a DOM element."""
        return self.action in ("navigate", "wait", "scroll", "log", "format", "transform", "llm", "http")


# ─── VOCABULARY ───────────────────────────────────────────────────────────────

# Order matters: longer phrases must be checked before their sub-phrases.
ACTION_VERBS: list[tuple[str, str]] = [
    # Scrape / Extract / Fetch
    ("list all",        "scrape"),
    ("list of",         "scrape"),
    ("list",            "scrape"),
    ("fetch each",      "scrape"),
    ("fetch the",       "scrape"),
    ("fetch",           "scrape"),
    ("scrape",          "scrape"),
    ("extract the",     "scrape"),
    ("extract",         "scrape"),
    ("get the",         "scrape"),
    ("count the",       "scrape"),  # user query
    ("grab",            "scrape"),
    ("collect",         "scrape"),

    # Navigate
    ("go to",           "navigate"),
    ("navigate to",     "navigate"),
    ("open the",        "navigate"),
    ("open",            "navigate"),
    ("visit",           "navigate"),
    ("load",            "navigate"),

    # Type / Search
    ("search for",      "type"),
    ("search of",       "type"),   # user query
    ("search",          "type"),
    ("type",            "type"),
    ("enter",           "type"),
    ("fill",            "type"),
    ("input",           "type"),

    # Format / Map
    ("make it in",      "format"),
    ("format as",       "format"),
    ("format the",      "format"),
    ("format",          "format"),
    ("convert",         "format"),

    # Network / Send
    ("send a request",  "http"),
    ("send to",         "http"),
    ("send me to",      "http"),
    ("send me",         "http"),
    ("sned me to",      "http"),   # user typo
    ("sned me",         "http"),   # user typo
    ("sned",            "http"),
    ("share to",        "http"),
    ("mail me",         "http"),
    ("email",           "http"),
    ("notify",          "http"),
    ("post to",         "http"),

    # Click (most generic — checked last)
    ("click on",        "click"),
    ("click",           "click"),
    ("press",           "click"),
    ("tap",             "click"),
    ("follow",          "click"),      # "follow the link"
    ("submit",          "click"),
    ("hit",             "click"),
    ("find and click",  "click"),

    # Select (dropdown)
    ("select",          "select"),
    ("choose",          "select"),
    ("pick",            "select"),

    # Check / Uncheck
    ("check",           "check"),
    ("uncheck",         "check"),
    ("tick",            "check"),
    ("enable",          "check"),

    # Scroll
    ("scroll",          "scroll"),
    ("scroll down",     "scroll"),
    ("scroll up",       "scroll"),

    # Wait
    ("wait",            "wait"),
    ("pause",           "wait"),
    ("hold",            "wait"),
    ("delay",           "wait"),

    # Transform
    ("map the",         "transform"),
    ("transform",       "transform"),

]

# Qualifier words that determine which of many matching elements to pick
QUALIFIERS: dict[str, str] = {
    "first":  "first",
    "1st":    "first",
    "top":    "first",
    "second": "second",
    "2nd":    "second",
    "third":  "third",
    "3rd":    "third",
    "last":   "last",
    "bottom": "last",
    "all":    "all",        # Scrape all matching elements
    "every":  "all",
}

# Filler words to strip from target extraction (articles, adjectives, prepositions)
FILLER_WORDS = {
    "the", "a", "an", "some", "any", "this", "that", "these", "those",
    "very", "really", "exactly", "please", "just", "now", "then",
    "on", "in", "at", "to", "for", "of", "with", "from", "by", "and", "into", "onto", "over",
    "it", "its", "there",
}

# Sentence-splitting conjunctions (separates multiple intents in one prompt)
SENTENCE_SPLITTERS = re.compile(
    r"\.\s+|\?\s+|!\s+|[,;]\s*(?:then|and|after(?: that)?|next|finally|also|extract|scrape|click|type|fill|get|log|format|summarize|analyze|send|notify|call|fetch|make|sned|share|mail)\s+|,\s*(?=\w)|\s+(?:then|and|after(?: that)?|next)\s+(?=(?:extract|scrape|click|type|fill|get|log|format|summarize|analyze|send|notify|call|fetch|make|sned|share|mail|list|fetch|sned|share|mail|filter))",
    re.IGNORECASE
)

# Secondary split: 'and [verb]' without preceding punctuation
# e.g., "scrape the age and scrape the club" → two clauses
# Built dynamically from ACTION_VERBS after the list is defined
_SECONDARY_SPLIT_PATTERN: re.Pattern = None  # Initialized after ACTION_VERBS

# Detects URLs in text (for navigate intent value extraction)
URL_PATTERN = re.compile(r"https?://[^\s]+|www\.[^\s]+|[a-z0-9-]+\.(?:com|org|net|io|gov|edu|uk|ca|de|jp|fr|au|co|in|bcc|bbc)\b", re.IGNORECASE)

# Detects quoted values: 'search term' or "search term"
QUOTED_VALUE_PATTERN = re.compile(r"['\"]([^'\"]+)['\"]")

# Detects wait durations: "wait 3 seconds", "pause for 2 minutes"
WAIT_DURATION_PATTERN = re.compile(r"(\d+)\s*(second|sec|s|minute|min|ms)", re.IGNORECASE)


def _buildSecondaryPattern() -> re.Pattern:
    """
    Builds a regex that matches " and <action_verb> " (or similar conjunctions)
    as a clause boundary.
    This catches: "scrape age and scrape club", "click login then type email",
    "and then extract his age" etc.
    """
    # Collect all unique verb strings from ACTION_VERBS, longest first
    verbs = sorted({phrase for phrase, _ in ACTION_VERBS}, key=len, reverse=True)
    escaped = [re.escape(v) for v in verbs]
    pattern = r"\s+(?:and\s+then|and\s+also|and|then)\s+(?=" + "|".join(escaped) + r")"
    return re.compile(pattern, re.IGNORECASE)

# Build once at module load time
_SECONDARY_SPLIT_PATTERN = _buildSecondaryPattern()


# ─── PARSER ───────────────────────────────────────────────────────────────────

class IntentParser:
    """
    Converts a natural language automation goal into an ordered list of Intent objects.
    No API calls. No ML models. Pure rule-based parsing.

    Usage:
        parser  = IntentParser()
        intents = parser.parse("Go to hacker news, search for AI agents, click the first result")
    """

    def parse(self, prompt: str, targetUrl: str) -> list[Intent]:
        """
        Main entry point.

        Args:
            prompt:    Natural language description of the automation goal.
            targetUrl: The URL the workflow should start at.

        Returns:
            Ordered list of Intent objects, starting with a NAVIGATE intent.
        """
        intents: list[Intent] = []

        # ── Always start with navigation ──────────────────────────────────
        triggerType = "MANUAL"
        cron = None

        # Simple schedule detection
        lower_prompt = prompt.lower()
        if any(w in lower_prompt for w in ["every", "daily", "hourly", "each day", "schedule"]):
            triggerType = "SCHEDULE"
            time_match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", lower_prompt)
            if "daily" in lower_prompt or "each day" in lower_prompt or "day" in lower_prompt:
                if time_match:
                    hour = int(time_match.group(1))
                    if time_match.group(3) == "pm" and hour < 12: hour += 12
                    cron = f"0 {hour} * * *"
                else:
                    cron = "0 9 * * *"
            elif "hourly" in lower_prompt:
                cron = "0 * * * *"
            else:
                cron = "0 0 * * *" # Default midnight

        navigateIntent = Intent(
            stepNumber=0,
            action="navigate",
            targetDescription=targetUrl,
            value=targetUrl,
            qualifier=None,
            rawSentence=f"Navigate to {targetUrl}",
            confidence=1.0,
        )
        # Store trigger info in the first intent's qualifier or as a special property?
        # Better to add a 'triggerConfig' property to the first intent
        navigateIntent.triggerType = triggerType
        navigateIntent.cronSchedule = cron

        intents.append(navigateIntent)

        # ── Split prompt into individual sentences / clauses ──────────────
        # Step 1: Primary split on punctuation + conjunctions (". ", ", then", ", and", etc.)
        rawClauses = [c.strip() for c in SENTENCE_SPLITTERS.split(prompt) if c.strip()]
        if not rawClauses:
            rawClauses = [prompt.strip()]

        # Step 2: Split on comma directly followed by an action verb: ", type", ", click" etc.
        # This is the MOST COMMON pattern from users: "type X, type Y, click Z"
        commaVerbClauses = []
        for raw in rawClauses:
            commaVerbClauses.extend(self._splitOnCommaVerb(raw))

        # Step 3: Secondary split within each clause on 'and [verb]' boundaries
        # This catches: "scrape the age and scrape the club he plays for"
        clauses = []
        for raw in commaVerbClauses:
            subClauses = [s.strip() for s in _SECONDARY_SPLIT_PATTERN.split(raw) if s.strip()]
            clauses.extend(subClauses if subClauses else [raw])

        # ── Parse each clause ─────────────────────────────────────────────
        for clauseIndex, clause in enumerate(clauses):
            intent = self._parseClause(clause, stepNumber=len(intents))
            if intent:
                # SPECIAL CASE: "scrape text, author, and date" in ONE clause.
                # If it's a SCRAPE action with commas, and no verbs follow, split it.
                if intent.action == "scrape" and "," in intent.targetDescription:
                    # Example: "quote text, author's name"
                    targets = [t.strip() for t in intent.targetDescription.split(",") if t.strip()]
                    # Also handle "and" in the last item
                    if targets and " and " in targets[-1]:
                        last_items = [t.strip() for t in targets[-1].split(" and ") if t.strip()]
                        targets = targets[:-1] + last_items

                    if len(targets) > 1:
                        # Re-create intents for each sub-target
                        for sub_idx, sub_target in enumerate(targets):
                            intents.append(Intent(
                                stepNumber=len(intents),
                                action="scrape",
                                targetDescription=sub_target,
                                value=intent.value,
                                qualifier=intent.qualifier or ("all" if "list of" in sub_target.lower() else None),
                                rawSentence=clause,
                                confidence=intent.confidence
                            ))
                        continue

                # OPTIMIZATION: Skip redundant navigate if it's the exact same as targetUrl
                # or if it's just repeating "go to [url]" that we already handle as trigger.
                if intent.action == "navigate" and len(intents) == 1:
                    # check if the navigate URL is similar to targetUrl
                    nav_url = intent.value or intent.targetDescription
                    if nav_url and targetUrl in nav_url or nav_url in targetUrl:
                        logger.info(f"Skipping redundant navigate to {nav_url}")
                        # Instead of appending, we just update the first intent (trigger)
                        # but we don't add this as a new step.
                        continue

                intent.stepNumber = len(intents)
                intents.append(intent)
            else:
                # Could not parse cleanly — create a low-confidence placeholder for LLM fallback
                logger.info(f"[IntentParser] Low-confidence clause: '{clause}'")
                intents.append(Intent(
                    stepNumber=len(intents),
                    action="click",           # Best guess
                    targetDescription=clause, # Entire clause as target — LLM will interpret
                    value=None,
                    qualifier=None,
                    rawSentence=clause,
                    confidence=0.3,           # Signal to use LLM for this step
                ))

        logger.info(
            f"[IntentParser] Parsed {len(intents)} intents from prompt: '{prompt[:60]}...'"
        )
        return intents

    # ── PRIVATE ────────────────────────────────────────────────────────────

    def _parseClause(self, clause: str, stepNumber: int) -> Optional[Intent]:
        """
        Attempts to extract action, target, value, and qualifier from a single clause.
        Returns None if no action verb was found (caller creates a fallback Intent).
        """
        lower = clause.lower()

        # ── 1. Extract action verb ────────────────────────────────────────
        action, verbEnd = self._extractAction(lower)
        if not action:
            return None

        # ── 2. Extract quoted value (highest priority) ────────────────────
        quotedMatch = QUOTED_VALUE_PATTERN.search(clause)
        value = quotedMatch.group(1) if quotedMatch else None

        # ── 3. Extract URL for navigate intents ───────────────────────────
        if action == "navigate":
            urlMatch = URL_PATTERN.search(clause)
            if urlMatch:
                value = urlMatch.group(0)
            elif not value:
                # e.g., "open the dashboard" — no URL → treat as click
                action = "click"

        # ── 4. Extract wait duration ──────────────────────────────────────
        if action == "wait":
            durationMatch = WAIT_DURATION_PATTERN.search(lower)
            if durationMatch:
                num  = int(durationMatch.group(1))
                unit = durationMatch.group(2).lower()
                ms   = num * 60000 if "min" in unit else (num * 1000 if unit not in ("ms",) else num)
                value = str(ms)
            else:
                value = "2000"  # Default 2-second wait

        # ── 5. Extract qualifier (first/last/nth) ─────────────────────────
        qualifier = self._extractQualifier(lower)

        # ── 6. Extract target noun phrase ─────────────────────────────────
        # Remove the matched verb phrase, quoted value, URL, and qualifier words
        remainder = clause[verbEnd:].strip()
        if quotedMatch:
            remainder = remainder.replace(quotedMatch.group(0), "").strip()
        if action == "navigate" and value and value in remainder:
            remainder = remainder.replace(value, "").strip()
        target = self._cleanTarget(remainder)

        # Fallback: if no clean target, use the full clause
        if not target:
            target = clause

        # ── 7. Compute confidence ─────────────────────────────────────────
        # Lower confidence = more ambiguous = may be routed to LLM
        confidence = 1.0
        if len(target.split()) > 5:
            confidence -= 0.2    # Long targets are harder to match
        if qualifier == "all":
            confidence -= 0.1    # "Scrape all" is complex

        return Intent(
            stepNumber=stepNumber,
            action=action,
            targetDescription=target,
            value=value,
            qualifier=qualifier,
            rawSentence=clause,
            confidence=max(confidence, 0.0),
        )

    def _splitOnCommaVerb(self, text: str) -> list[str]:
        """
        Splits on commas directly followed by an action verb.
        Handles: "type X into Y, type Z into W, click submit"
        Preserves quoted strings by not splitting inside quotes.
        """
        lower = text.lower()
        parts = []
        lastSplit = 0

        i = 0
        while i < len(lower):
            # Skip quoted content
            if lower[i] in ('"', "'"):
                quote = lower[i]
                i += 1
                while i < len(lower) and lower[i] != quote:
                    i += 1
                i += 1
                continue

            # Check for comma followed by optional whitespace and an action verb
            if lower[i] == ',':
                rest = lower[i + 1:].lstrip()
                for phrase, _ in ACTION_VERBS:
                    if rest.startswith(phrase):
                        # Found a comma+verb boundary
                        part = text[lastSplit:i].strip()
                        if part:
                            parts.append(part)
                        # Skip the comma and whitespace
                        offset = len(text) - len(text[i + 1:].lstrip())
                        lastSplit = offset
                        break
            i += 1

        # Add the remaining text
        remaining = text[lastSplit:].strip()
        if remaining:
            parts.append(remaining)

        return parts if parts else [text]

    def _extractAction(self, lower: str) -> tuple[Optional[str], int]:
        """
        Finds the first action verb phrase in the lowercased clause.
        Uses word boundaries to avoid matching sub-strings (e.g. 'ai' in 'mail').
        Returns (action_name, index_after_verb) or (None, 0).
        """
        # Sort verbs by length descending to catch longest match first
        sorted_verbs = sorted(ACTION_VERBS, key=lambda x: len(x[0]), reverse=True)

        for phrase, actionName in sorted_verbs:
            # Match with word boundary
            match = re.search(rf"\b{re.escape(phrase)}\b", lower)
            if match:
                return actionName, match.end()

        return None, 0

    def _extractQualifier(self, lower: str) -> Optional[str]:
        """
        Extracts qualifiers like 'all', 'first', or numerical limits ('top 5').
        Returns the qualifier name as a string.
        """
        if "all" in lower or "every" in lower or "list of" in lower:
            return "all"
        if "first" in lower:
            return "first"

        # Detect numerical "top X" or "first X"
        numMatch = re.search(r"(?:top|first|each|limited to)\s+(\d+)", lower)
        if numMatch:
            return f"limit:{numMatch.group(1)}"

        return None

    def _cleanTarget(self, text: str) -> str:
        """
        Strips filler words, qualifiers, and extra whitespace from a target phrase.
        Leaves the meaningful noun phrase.
        """
        words = text.lower().split()
        cleaned = [w for w in words if w not in FILLER_WORDS and w not in QUALIFIERS]
        return " ".join(cleaned).strip()
