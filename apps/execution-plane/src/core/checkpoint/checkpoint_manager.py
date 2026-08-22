# src/core/checkpoint/checkpoint_manager.py
"""
CheckpointManager — Multi-Page Plan Stabilization (Fix for 2.1)

Problem being solved:
  The planner generates a QuantaPlan DAG from page 1's axTree. When the user
  navigates to page 2, 3, N... the DOM is fundamentally different. The original
  plan becomes stale, navigation guesses, and extraction accuracy degrades.

Solution — Structural Checkpoint Protocol:
  1. After every navigation action that results in a URL or DOM change, compute
     a multi-dimensional DOM fingerprint of the landing page.
  2. Compare it against the EXPECTED fingerprint stored in the checkpoint for
     that plan step.
  3. If divergence is beyond threshold → trigger LOCAL re-plan for remaining
     steps only (not a full restart). One targeted LLM call.
  4. Update the running plan in-place. Execution continues uninterrupted.

Key design decisions:
  - Math-first: fingerprint comparison is zero-LLM
  - Re-plan is surgical: we only patch REMAINING steps, not completed ones
  - Non-fatal: if re-planning fails, execution continues in best-effort mode
  - Idempotent: multiple calls with the same fingerprint produce no action
  - Thread-safe: all state is per-instance, no globals
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from playwright.async_api import Page

logger = logging.getLogger("checkpoint_manager")


# ---------------------------------------------------------------------------
# Tuning constants — adjust via environment variables at the worker level
# ---------------------------------------------------------------------------
# Interactive element count divergence that triggers re-plan (fraction of total)
INTERACTIVE_DIVERGENCE_THRESHOLD: float = 0.40
# Text-length divergence fraction that triggers re-plan
TEXT_DIVERGENCE_THRESHOLD: float = 0.60
# Minimum structural score (0..1) below which we always re-plan
MIN_STRUCTURAL_SCORE: float = 0.50


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DOMFingerprint:
    """
    Multi-dimensional structural fingerprint of a rendered page.

    Intentionally COARSE — we want to detect meaningful page transitions,
    not every micro-mutation (tooltip appears, accordion expands, etc.).
    """
    interactive_count: int  # a, button, input, select, textarea
    text_length: int         # body.innerText length (proxy for content volume)
    unique_links: int        # distinct non-anchor hrefs
    heading_count: int       # h1-h4 elements (structural depth signal)
    list_count: int          # ul, ol, dl (data-list proxy)
    url: str                 # URL at fingerprint time

    def divergence_score(self, other: "DOMFingerprint") -> float:
        """
        Compute a [0..1] divergence score against another fingerprint.
        0.0 = identical structure, 1.0 = completely different.

        We weight each dimension:
          - interactive_count: 0.35 (nav controls, forms — most structural)
          - text_length:       0.30 (content volume — strong page-type signal)
          - unique_links:      0.20 (navigation options — pagination indicator)
          - heading_count:     0.10 (content depth)
          - list_count:        0.05 (data density)
        """
        WEIGHTS = [0.35, 0.30, 0.20, 0.10, 0.05]

        def _dim_divergence(a: int, b: int) -> float:
            """Normalized distance for a single dimension."""
            if a == 0 and b == 0:
                return 0.0
            maximum = max(a, b)
            return abs(a - b) / maximum

        dimensions = [
            _dim_divergence(self.interactive_count, other.interactive_count),
            _dim_divergence(self.text_length, other.text_length),
            _dim_divergence(self.unique_links, other.unique_links),
            _dim_divergence(self.heading_count, other.heading_count),
            _dim_divergence(self.list_count, other.list_count),
        ]

        return sum(w * d for w, d in zip(WEIGHTS, dimensions))

    def structural_similarity(self, other: "DOMFingerprint") -> float:
        """Returns [0..1] where 1.0 is identical."""
        return 1.0 - self.divergence_score(other)


@dataclass
class CheckpointRecord:
    """
    Stores the expected fingerprint for a plan step, captured when the step
    was first planned. Updated on successful execution.
    """
    step_index: int
    step_intent: str
    expected_fingerprint: Optional[DOMFingerprint] = None
    actual_fingerprint: Optional[DOMFingerprint] = None
    was_replanned: bool = False


class CheckpointDivergence(Exception):
    """
    Raised when DOM divergence exceeds threshold and re-planning is required.
    Carries the divergence score and current fingerprint for the caller.
    """
    def __init__(
        self,
        divergence: float,
        current_fingerprint: DOMFingerprint,
        step_index: int,
    ):
        super().__init__(
            f"DOM divergence {divergence:.2%} at step {step_index} "
            f"exceeds threshold — re-plan triggered"
        )
        self.divergence = divergence
        self.current_fingerprint = current_fingerprint
        self.step_index = step_index


# ---------------------------------------------------------------------------
# JavaScript injected into the page — coarse structural probe
# ---------------------------------------------------------------------------
_FINGERPRINT_JS = """
() => {
    const interactive = document.querySelectorAll(
        'a, button, input, select, textarea, [role="button"], [role="combobox"]'
    ).length;

    const textLen = (document.body && document.body.innerText || '').length;

    const uniqueLinks = new Set(
        Array.from(document.querySelectorAll('a[href]'))
            .map(a => a.getAttribute('href'))
            .filter(h => h && !h.startsWith('#') && !h.startsWith('javascript'))
    ).size;

    const headingCount = document.querySelectorAll('h1, h2, h3, h4').length;

    const listCount = document.querySelectorAll('ul, ol, dl').length;

    return { interactive, textLen, uniqueLinks, headingCount, listCount };
}
"""


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class CheckpointManager:
    """
    Lifecycle: one instance per job execution.

    Usage in the execution loop:
        manager = CheckpointManager(job_id=job_id)

        # Before step executes: record entry fingerprint
        await manager.record_entry(page, step_index, step_intent)

        # After navigation settles: validate landing page
        # This either passes silently or raises CheckpointDivergence
        diverged, score = await manager.validate_landing(page, step_index)

        # If diverged: caller calls patch_plan() to get updated remaining steps
        if diverged:
            remaining_steps = await manager.patch_plan(
                page=page,
                current_step_index=step_index,
                remaining_steps=remaining_plan_steps,
                objective=navigation_objective,
                job_id=job_id,
            )
    """

    def __init__(self, job_id: str):
        self._job_id = job_id
        self._checkpoints: Dict[int, CheckpointRecord] = {}
        self._baseline: Optional[DOMFingerprint] = None
        self._replan_count: int = 0
        self._max_replans: int = 3  # Guard against infinite replan loops

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def record_entry(
        self,
        page: Page,
        step_index: int,
        step_intent: str,
    ) -> DOMFingerprint:
        """
        Capture and store the DOM fingerprint BEFORE a step executes.
        This becomes the 'expected' state for the PREVIOUS step's outcome.

        Called after navigation settles, before the next step starts.
        """
        fp = await self._capture(page)
        self._checkpoints[step_index] = CheckpointRecord(
            step_index=step_index,
            step_intent=step_intent,
            expected_fingerprint=fp,
        )
        logger.debug(
            f"[{self._job_id}] Checkpoint [{step_index}] recorded: "
            f"interactive={fp.interactive_count}, text={fp.text_length}, "
            f"links={fp.unique_links}, url={fp.url[:60]}"
        )
        return fp

    async def validate_landing(
        self,
        page: Page,
        step_index: int,
        *,
        interactive_threshold: float = INTERACTIVE_DIVERGENCE_THRESHOLD,
        text_threshold: float = TEXT_DIVERGENCE_THRESHOLD,
        structural_min: float = MIN_STRUCTURAL_SCORE,
    ) -> tuple[bool, float]:
        """
        Capture current DOM and compare against expected fingerprint.

        Returns:
            (diverged: bool, divergence_score: float)

        diverged=True means: caller should trigger re-plan.
        diverged=False means: plan is still valid, continue normally.

        Never raises — all errors produce diverged=False (fail-open, not fail-close).
        """
        checkpoint = self._checkpoints.get(step_index)
        if not checkpoint or not checkpoint.expected_fingerprint:
            # No baseline for this step — cannot validate, proceed
            return False, 0.0

        try:
            current_fp = await self._capture(page)
            checkpoint.actual_fingerprint = current_fp
            expected_fp = checkpoint.expected_fingerprint

            divergence = expected_fp.divergence_score(current_fp)
            similarity = 1.0 - divergence

            logger.info(
                f"[{self._job_id}] Checkpoint [{step_index}] validation: "
                f"similarity={similarity:.2%}, divergence={divergence:.2%} | "
                f"expected_url={expected_fp.url[:50]} | current_url={current_fp.url[:50]}"
            )

            # Fail-open: same URL with minor variation → not diverged
            if expected_fp.url == current_fp.url and divergence < 0.20:
                return False, divergence

            # Gate 1: structural similarity below minimum → always re-plan
            if similarity < structural_min:
                logger.warning(
                    f"[{self._job_id}] Checkpoint [{step_index}]: structural similarity "
                    f"{similarity:.2%} below minimum {structural_min:.0%} — flagging divergence"
                )
                return True, divergence

            # Gate 2: interactive element count changed dramatically
            inter_div = abs(
                expected_fp.interactive_count - current_fp.interactive_count
            ) / max(expected_fp.interactive_count, 1)
            if inter_div > interactive_threshold:
                logger.warning(
                    f"[{self._job_id}] Checkpoint [{step_index}]: interactive element "
                    f"divergence {inter_div:.2%} > threshold {interactive_threshold:.0%}"
                )
                return True, divergence

            # Gate 3: text volume changed dramatically (new content type)
            text_div = abs(
                expected_fp.text_length - current_fp.text_length
            ) / max(expected_fp.text_length, 1)
            if text_div > text_threshold:
                logger.warning(
                    f"[{self._job_id}] Checkpoint [{step_index}]: text length "
                    f"divergence {text_div:.2%} > threshold {text_threshold:.0%}"
                )
                return True, divergence

            return False, divergence

        except Exception as exc:
            logger.warning(
                f"[{self._job_id}] Checkpoint validation error at step {step_index} "
                f"(non-fatal, proceeding): {exc}"
            )
            return False, 0.0

    async def patch_plan(
        self,
        page: Page,
        current_step_index: int,
        remaining_steps: List[Dict[str, Any]],
        objective: str,
        job_id: str,
    ) -> List[Dict[str, Any]]:
        """
        Trigger a surgical local re-plan for the remaining steps.

        This is NOT a full restart. It:
          1. Captures the current axTree (navigation-only elements)
          2. Calls the Planner with: current DOM context + original objective
             + hint about what has already been completed
          3. Returns a NEW list of remaining steps to replace the old ones
          4. Falls back to original remaining steps if re-planning fails

        Guards:
          - Max 3 re-plans per job (prevents infinite replan loops)
          - Non-fatal: always returns something executable
        """
        if self._replan_count >= self._max_replans:
            logger.warning(
                f"[{job_id}] Max re-plans ({self._max_replans}) reached. "
                "Continuing with original remaining steps."
            )
            return remaining_steps

        self._replan_count += 1
        logger.info(
            f"[{job_id}] Checkpoint re-plan #{self._replan_count} triggered at step "
            f"{current_step_index}. {len(remaining_steps)} steps to re-plan."
        )

        try:
            # Build a compact axTree context for the re-planner
            current_url = page.url
            page_context = await self._extract_nav_context(page)

            from core.rag.planner import get_planner

            planner = get_planner()

            # Summarize what was already done (for context, not re-execution)
            completed_count = current_step_index
            completed_context = (
                f"{completed_count} steps already completed successfully. "
                f"Now on page: {current_url}. "
                f"Page structure has changed — adapting remaining plan."
            )

            # Augmented objective: tells the planner what context we're in
            augmented_objective = (
                f"{objective}\n\n"
                f"CONTEXT: {completed_context}\n\n"
                f"CURRENT PAGE ELEMENTS (for reference):\n{page_context[:800]}"
            )

            new_steps = await planner.plan_objective(
                objective=augmented_objective,
                url=current_url,
                job_id=job_id,
            )

            if new_steps and len(new_steps) > 0:
                # Mark checkpoints as re-planned
                for idx in range(current_step_index, current_step_index + len(remaining_steps)):
                    if idx in self._checkpoints:
                        self._checkpoints[idx].was_replanned = True

                logger.info(
                    f"[{job_id}] Re-plan success: {len(new_steps)} new steps "
                    f"(replaced {len(remaining_steps)} original steps)"
                )
                return new_steps
            else:
                logger.warning(f"[{job_id}] Re-planner returned empty steps. Keeping originals.")
                return remaining_steps

        except Exception as replan_err:
            logger.error(
                f"[{job_id}] Re-plan failed (non-fatal): {replan_err}. "
                "Continuing with original remaining steps."
            )
            return remaining_steps

    def get_stats(self) -> Dict[str, Any]:
        """Return diagnostic stats for telemetry."""
        return {
            "total_checkpoints": len(self._checkpoints),
            "total_replans": self._replan_count,
            "replanned_steps": [
                idx for idx, cp in self._checkpoints.items() if cp.was_replanned
            ],
        }

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    async def _capture(self, page: Page) -> DOMFingerprint:
        """
        Inject the fingerprint JS probe and parse its result.
        Falls back to a minimal fingerprint if the page is crashed/closed.
        """
        try:
            result: dict = await page.evaluate(_FINGERPRINT_JS)
            return DOMFingerprint(
                interactive_count=result.get("interactive", 0),
                text_length=result.get("textLen", 0),
                unique_links=result.get("uniqueLinks", 0),
                heading_count=result.get("headingCount", 0),
                list_count=result.get("listCount", 0),
                url=page.url,
            )
        except Exception as exc:
            logger.debug(f"[{self._job_id}] Fingerprint capture failed: {exc}")
            return DOMFingerprint(
                interactive_count=0,
                text_length=0,
                unique_links=0,
                heading_count=0,
                list_count=0,
                url=getattr(page, "url", ""),
            )

    async def _extract_nav_context(self, page: Page) -> str:
        """
        Extract a compact navigation-only axTree for the re-planner.
        Only returns interactive elements (buttons, links, inputs) — NOT content.
        """
        try:
            elements: list = await page.evaluate("""
                () => {
                    const SELECTORS = 'a[href], button, input, select, textarea, [role="button"]';
                    return Array.from(document.querySelectorAll(SELECTORS))
                        .filter(el => {
                            const s = window.getComputedStyle(el);
                            return s.display !== 'none' && s.visibility !== 'hidden';
                        })
                        .slice(0, 60)
                        .map(el => {
                            const tag = el.tagName.toLowerCase();
                            const text = (el.innerText || el.placeholder || el.aria-label || '').trim().slice(0, 50);
                            const href = el.getAttribute('href') || '';
                            return `[${tag}] ${text}${href ? ' → ' + href.slice(0, 40) : ''}`;
                        });
                }
            """)
            return "\n".join(elements)
        except Exception:
            return ""
