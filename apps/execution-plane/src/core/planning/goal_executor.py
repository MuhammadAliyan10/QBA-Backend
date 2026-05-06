# core/planning/goalExecutor.py
"""
Goal Executor v3.0 — Tactical Epoch Binder with StateDesync Detection.

Carries out GoalAction intents emitted by the SightedPlanner. Uses Semantic
Late Binding via SmartFinder to resolve intents to live ElementHandles at
the millisecond of execution.

Raises StateDesyncException when:
  - SmartFinder cannot resolve an intent (element missing / stale).
  - A navigation event fires mid-epoch (page URL changed unexpectedly).
  - A new tab opens that the current epoch did not anticipate.

The pipeline catches StateDesyncException, re-harvests, and re-plans.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from playwright.async_api import (
    BrowserContext,
    ElementHandle,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from core.planning.sighted_planner import GoalAction, ActionEnum, EpochPlan
from core.extraction.validator import validate_extraction
from core.nervous_system import NervousSystem

logger = logging.getLogger("goalExecutor")


# =============================================================================
# CUSTOM EXCEPTION — TRIGGERS RE-PLAN IN THE PIPELINE
# =============================================================================

class StateDesyncException(Exception):
    """
    Raised when the live browser state diverges from the epoch's assumptions.

    Triggers:
      - SmartFinder cannot resolve an intent (element vanished or mutated).
      - Navigation fires mid-epoch (URL changed under our feet).
      - An unexpected new tab opens.

    The SightedPipeline catches this, re-harvests, and asks the planner
    for a fresh epoch — implementing the Reflex Arc.
    """

    def __init__(self, reason: str, goal_id: str = "", context: Optional[Dict] = None):
        self.reason = reason
        self.goal_id = goal_id
        self.desync_context = context or {}
        super().__init__(f"StateDesync[{goal_id}]: {reason}")


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class GoalResult:
    """Result of executing a single tactical intent."""
    goal_id: str
    success: bool
    action: str = ""
    duration_ms: int = 0
    extracted_data: Any = None
    error: Optional[str] = None
    healed: bool = False
    transition_detected: bool = False


@dataclass
class EpochReport:
    """Aggregate result of executing a full SightedEpoch."""
    success: bool
    strategic_objective: str = ""
    results: List[GoalResult] = field(default_factory=list)
    extracted_data: Dict[str, Any] = field(default_factory=dict)
    transition_verified: bool = False
    is_final: bool = False
    error: Optional[str] = None
    aborted_by_desync: bool = False


# =============================================================================
# GOAL EXECUTOR
# =============================================================================

class GoalExecutor:
    """
    Tactical Epoch Binder.

    Resolves semantic intents to live DOM elements via SmartFinder, executes
    browser actions, and monitors for state desync mid-epoch.
    """

    ACTION_SETTLE_MS = 800
    NAVIGATION_WAIT_MS = 8000
    SMART_FINDER_TIMEOUT_MS = 5000

    def __init__(self, context: BrowserContext, job_id: str = ""):
        self.context = context
        self.job_id = job_id
        self.active_tabs: Dict[str, Page] = {}
        self._smart_finder_cache: Dict[int, Any] = {}
        self.max_concurrent_tabs = int(os.getenv("MAX_CONCURRENT_TABS", "5"))

    # ------------------------------------------------------------------
    # SMART FINDER ACCESSOR
    # ------------------------------------------------------------------

    def _get_smart_finder(self, page: Page) -> Any:
        page_id = id(page)
        if page_id not in self._smart_finder_cache:
            from core.selector.smart_finder import SmartFinder
            self._smart_finder_cache[page_id] = SmartFinder(page)
        return self._smart_finder_cache[page_id]

    # ------------------------------------------------------------------
    # EPOCH EXECUTION (public entry point)
    # ------------------------------------------------------------------

    async def execute_epoch(self, epoch: EpochPlan, active_page: Page) -> EpochReport:
        """
        Execute all tactical intents in an epoch. Supports parallel batching.
        """
        # Register the primary page
        primary_id = "primary"
        if primary_id not in self.active_tabs:
            self.active_tabs[primary_id] = active_page

        report = EpochReport(
            success=True,
            strategic_objective=epoch.strategic_objective,
            is_final=epoch.is_final_step,
        )
        
        # Check if this epoch is a fan-out intent (simple heuristic for parallel)
        is_fan_out = any(g.action in (ActionEnum.NEW_TAB, ActionEnum.SWITCH_TAB) for g in epoch.intents) and len(epoch.intents) > 3

        if is_fan_out:
            logger.info(f"[GoalExecutor] Fan-out intent detected. Executing {len(epoch.intents)} goals concurrently.")
            batch_results = await self._execute_parallel_batch(epoch.intents, active_page)
            for res in batch_results:
                report.results.append(res)
                if res.extracted_data is not None:
                    # simplistic store mapping for batch
                    report.extracted_data.setdefault("batch_data", []).append(res.extracted_data)
                if not res.success:
                    report.success = False
                    report.error = res.error
        else:
            current_page = active_page
            pre_url = current_page.url
            pre_tab_count = len(self.context.pages)

            for goal in epoch.intents:
                logger.info(
                    f"[GoalExecutor] [{goal.goal_id}] {goal.action.value} → "
                    f"intent='{goal.intent}' value='{goal.value}'"
                )

                await NervousSystem.publish_update(
                    self.job_id, "RUNNING",
                    f"[Epoch] {goal.action.value}: {goal.intent or goal.value}",
                    goal.goal_id,
                )

                result = await self._execute_single(goal, current_page)
                report.results.append(result)

                if not result.success:
                    report.success = False
                    report.error = result.error
                    break

                # --- State Desync Detection ---
                await self._detect_desync(goal, current_page, pre_url, pre_tab_count)

                # Update references for next iteration
                pre_url = current_page.url
                pre_tab_count = len(self.context.pages)

                if result.extracted_data is not None and goal.store_as:
                    # PHASE 5: Strict Type Validation
                    validated_data = validate_extraction({goal.store_as: result.extracted_data})
                    clean_value = validated_data.get(goal.store_as)
                    
                    if clean_value is not None:
                        report.extracted_data[goal.store_as] = clean_value
                        # Fire-and-forget to NATS
                        asyncio.create_task(NervousSystem.publish(
                            f"quanta.data.extracted.{self.job_id}", 
                            json.dumps({goal.store_as: clean_value})
                        ))
                    else:
                        logger.warning(f"[GoalExecutor] [{goal.goal_id}] Extraction rejected by validator: {result.extracted_data}")

        # Verify expected transition
        if report.success and epoch.expected_outcome:
            report.transition_verified = await self._verify_transition(epoch.expected_outcome, active_page)

        return report

    async def _execute_parallel_batch(self, goals: List[GoalAction], base_page: Page) -> List[GoalResult]:
        semaphore = asyncio.Semaphore(self.max_concurrent_tabs)
        results = []
        
        async def _bounded_execute(goal: GoalAction, context_id: str):
            async with semaphore:
                page = None
                try:
                    if goal.action == ActionEnum.NEW_TAB and goal.value:
                        page = await self.context.new_page()
                        self.active_tabs[context_id] = page
                        await page.goto(goal.value, wait_until="domcontentloaded", timeout=self.NAVIGATION_WAIT_MS)
                        
                        # Execute extraction or other actions defined in this parallel goal
                        # Here we assume a composite intent or we treat NEW_TAB as the entry
                        res = await self._execute_single(goal, page)
                        return res
                    else:
                        return await self._execute_single(goal, base_page)
                except Exception as exc:
                    return GoalResult(goal_id=goal.goal_id, success=False, error=str(exc))
                finally:
                    if page and not page.is_closed():
                        await page.close()
                    if context_id in self.active_tabs:
                        del self.active_tabs[context_id]

        tasks = [_bounded_execute(goal, f"tab_{i}_{goal.goal_id}") for i, goal in enumerate(goals)]
        completed = await asyncio.gather(*tasks, return_exceptions=True)
        
        for c in completed:
            if isinstance(c, GoalResult):
                results.append(c)
                if c.extracted_data is not None:
                    # Fire-and-forget to NATS
                    asyncio.create_task(NervousSystem.publish(
                        f"quanta.data.extracted.{self.job_id}", 
                        json.dumps(c.extracted_data)
                    ))
            elif isinstance(c, Exception):
                results.append(GoalResult(goal_id="unknown", success=False, error=str(c)))
                
        return results

    # ------------------------------------------------------------------
    # SINGLE INTENT DISPATCH
    # ------------------------------------------------------------------

    async def _execute_single(self, goal: GoalAction, page: Page) -> GoalResult:
        start = time.time()
        try:
            handler = self._ACTION_DISPATCH.get(goal.action)
            if handler is None:
                return GoalResult(
                    goal_id=goal.goal_id, success=False,
                    error=f"Unsupported action: {goal.action.value}",
                )
            result = await handler(self, goal, page)
            result.duration_ms = int((time.time() - start) * 1000)
            result.action = goal.action.value
            return result

        except StateDesyncException:
            raise
        except Exception as exc:
            logger.error(f"[GoalExecutor] [{goal.goal_id}] Error: {exc}", exc_info=True)
            return GoalResult(
                goal_id=goal.goal_id, success=False,
                error=str(exc)[:300], action=goal.action.value,
                duration_ms=int((time.time() - start) * 1000),
            )

    # ------------------------------------------------------------------
    # ACTION HANDLERS
    # ------------------------------------------------------------------

    async def _action_click(self, goal: GoalAction, page: Page) -> GoalResult:
        element = await self._resolve_intent(goal, page)
        await element.scroll_into_view_if_needed()

        if goal.value:
            tag = await element.evaluate("el => el.tagName.toLowerCase()")
            if tag in ("input", "textarea"):
                logger.info(
                    f"[GoalExecutor] [{goal.goal_id}] Auto-promoting click→type "
                    f"(element is <{tag}>, value='{goal.value}')"
                )
                await element.click()
                await element.fill("")
                await element.type(goal.value, delay=30)
                await page.wait_for_timeout(self.ACTION_SETTLE_MS)
                return GoalResult(goal_id=goal.goal_id, success=True)

        try:
            await element.click(timeout=5000)
        except Exception as click_err:
            err_msg = str(click_err).lower()
            if "intercepts pointer events" in err_msg or "timeout" in err_msg:
                logger.warning(
                    f"[GoalExecutor] [{goal.goal_id}] Click intercepted by overlay, retrying with force=True"
                )
                await element.click(force=True, timeout=5000)
            else:
                raise
        await page.wait_for_timeout(self.ACTION_SETTLE_MS)
        return GoalResult(goal_id=goal.goal_id, success=True)

    async def _action_type(self, goal: GoalAction, page: Page) -> GoalResult:
        element = await self._resolve_intent(goal, page)
        await element.scroll_into_view_if_needed()
        try:
            await element.click(timeout=5000)
        except Exception:
            await element.click(force=True, timeout=5000)
        await element.fill("")
        await element.type(goal.value or "", delay=30)
        await page.wait_for_timeout(self.ACTION_SETTLE_MS)
        return GoalResult(goal_id=goal.goal_id, success=True)

    async def _action_select_option(self, goal: GoalAction, page: Page) -> GoalResult:
        element = await self._resolve_intent(goal, page)
        await element.select_option(label=goal.value)
        await page.wait_for_timeout(self.ACTION_SETTLE_MS)
        return GoalResult(goal_id=goal.goal_id, success=True)

    async def _action_extract_text(self, goal: GoalAction, page: Page) -> GoalResult:
        element = await self._resolve_intent(goal, page)
        text = await element.inner_text()
        return GoalResult(goal_id=goal.goal_id, success=True, extracted_data=text.strip())

    async def _action_extract_list(self, goal: GoalAction, page: Page) -> GoalResult:
        element = await self._resolve_intent(goal, page)
        items = await element.query_selector_all("li, tr, [role='listitem'], article")
        texts = []
        for item in items[:50]:
            t = await item.inner_text()
            stripped = t.strip()
            if stripped:
                texts.append(stripped)
        return GoalResult(goal_id=goal.goal_id, success=True, extracted_data=texts)

    async def _action_extract_table(self, goal: GoalAction, page: Page) -> GoalResult:
        element = await self._resolve_intent(goal, page)
        rows = await element.query_selector_all("tr")
        table_data: List[List[str]] = []
        for row in rows[:100]:
            cells = await row.query_selector_all("td, th")
            row_data = []
            for cell in cells:
                t = await cell.inner_text()
                row_data.append(t.strip())
            table_data.append(row_data)
        return GoalResult(goal_id=goal.goal_id, success=True, extracted_data=table_data)

    async def _action_hover(self, goal: GoalAction, page: Page) -> GoalResult:
        element = await self._resolve_intent(goal, page)
        await element.hover()
        await page.wait_for_timeout(self.ACTION_SETTLE_MS)
        return GoalResult(goal_id=goal.goal_id, success=True)

    async def _action_check(self, goal: GoalAction, page: Page) -> GoalResult:
        element = await self._resolve_intent(goal, page)
        is_checked = await element.is_checked()
        if not is_checked:
            await element.click()
        await page.wait_for_timeout(self.ACTION_SETTLE_MS)
        return GoalResult(goal_id=goal.goal_id, success=True)

    async def _action_navigate(self, goal: GoalAction, page: Page) -> GoalResult:
        target_url = goal.value
        if not target_url:
            return GoalResult(goal_id=goal.goal_id, success=False, error="Navigate requires a URL in 'value'.")
        await page.goto(target_url, wait_until="domcontentloaded", timeout=self.NAVIGATION_WAIT_MS)
        return GoalResult(goal_id=goal.goal_id, success=True)

    async def _action_scroll(self, goal: GoalAction, page: Page) -> GoalResult:
        direction = (goal.value or "down").lower()
        delta = -500 if direction == "up" else 500
        await page.mouse.wheel(0, delta)
        await page.wait_for_timeout(self.ACTION_SETTLE_MS)
        return GoalResult(goal_id=goal.goal_id, success=True)

    async def _action_wait(self, goal: GoalAction, page: Page) -> GoalResult:
        ms = 2000
        if goal.value and goal.value.isdigit():
            ms = min(int(goal.value), 10000)
        await page.wait_for_timeout(ms)
        return GoalResult(goal_id=goal.goal_id, success=True)

    async def _action_switch_tab(self, goal: GoalAction, page: Page) -> GoalResult:
        pages = self.context.pages
        idx = goal.target_tab_index
        if idx < 0 or idx >= len(pages):
            return GoalResult(
                goal_id=goal.goal_id, success=False,
                error=f"Tab index {idx} out of range (have {len(pages)} tabs).",
            )
        target_page = pages[idx]
        await target_page.bring_to_front()
        await target_page.wait_for_timeout(self.ACTION_SETTLE_MS)
        self._smart_finder_cache.pop(id(target_page), None)
        return GoalResult(goal_id=goal.goal_id, success=True)

    async def _action_new_tab(self, goal: GoalAction, page: Page) -> GoalResult:
        new_page = await self.context.new_page()
        if goal.value:
            await new_page.goto(goal.value, wait_until="domcontentloaded", timeout=self.NAVIGATION_WAIT_MS)
        self.active_tabs[f"tab_{goal.goal_id}"] = new_page
        await new_page.bring_to_front()
        return GoalResult(goal_id=goal.goal_id, success=True)

    async def _action_close_tab(self, goal: GoalAction, page: Page) -> GoalResult:
        pages = self.context.pages
        idx = goal.target_tab_index
        if idx < 0 or idx >= len(pages):
            return GoalResult(
                goal_id=goal.goal_id, success=False,
                error=f"Tab index {idx} out of range.",
            )
        target = pages[idx]
        await target.close()
        return GoalResult(goal_id=goal.goal_id, success=True)

    async def _action_press_key(self, goal: GoalAction, page: Page) -> GoalResult:
        key = goal.value or "Enter"
        await page.keyboard.press(key)
        await page.wait_for_timeout(self.ACTION_SETTLE_MS)
        return GoalResult(goal_id=goal.goal_id, success=True)

    # Dispatch table
    _ACTION_DISPATCH = {
        ActionEnum.CLICK: _action_click,
        ActionEnum.TYPE: _action_type,
        ActionEnum.SELECT_OPTION: _action_select_option,
        ActionEnum.EXTRACT_TEXT: _action_extract_text,
        ActionEnum.EXTRACT_LIST: _action_extract_list,
        ActionEnum.EXTRACT_TABLE: _action_extract_table,
        ActionEnum.HOVER: _action_hover,
        ActionEnum.CHECK: _action_check,
        ActionEnum.NAVIGATE: _action_navigate,
        ActionEnum.SCROLL: _action_scroll,
        ActionEnum.WAIT: _action_wait,
        ActionEnum.SWITCH_TAB: _action_switch_tab,
        ActionEnum.NEW_TAB: _action_new_tab,
        ActionEnum.CLOSE_TAB: _action_close_tab,
        ActionEnum.PRESS_KEY: _action_press_key,
    }

    # ------------------------------------------------------------------
    # SEMANTIC LATE BINDING
    # ------------------------------------------------------------------

    async def _resolve_intent(self, goal: GoalAction, page: Page) -> ElementHandle:
        """
        Resolve a semantic intent string to a live ElementHandle via SmartFinder.
        """
        finder = self._get_smart_finder(page)
        result = await finder.find(
            intent=goal.intent,
            timeout=self.SMART_FINDER_TIMEOUT_MS,
        )

        if result.found and result.element:
            return result.element

        raise StateDesyncException(
            reason=f"SmartFinder failed to resolve intent: '{goal.intent}'",
            goal_id=goal.goal_id,
            context={"url": page.url, "intent": goal.intent},
        )

    # ------------------------------------------------------------------
    # STATE DESYNC DETECTION
    # ------------------------------------------------------------------

    async def _detect_desync(
        self, goal: GoalAction, page: Page, pre_url: str, pre_tab_count: int
    ) -> None:
        """
        Check if a navigation or new-tab event fired unexpectedly.
        """
        navigational_actions = {ActionEnum.NAVIGATE, ActionEnum.SWITCH_TAB, ActionEnum.NEW_TAB}

        if goal.action in navigational_actions:
            return

        current_url = page.url
        current_tab_count = len(self.context.pages)

        url_changed = current_url != pre_url
        new_tab_opened = current_tab_count > pre_tab_count

        if url_changed or new_tab_opened:
            reason_parts = []
            if url_changed:
                reason_parts.append(f"URL changed: {pre_url} → {current_url}")
            if new_tab_opened:
                reason_parts.append(f"New tab opened (was {pre_tab_count}, now {current_tab_count})")

            raise StateDesyncException(
                reason=" | ".join(reason_parts),
                goal_id=goal.goal_id,
                context={"pre_url": pre_url, "current_url": current_url},
            )

    # ------------------------------------------------------------------
    # TRANSITION VERIFICATION
    # ------------------------------------------------------------------

    async def _verify_transition(self, expected_outcome: str, page: Page) -> bool:
        """Best-effort check that the epoch's expected outcome occurred."""
        outcome_lower = expected_outcome.lower()
        deadline = time.time() + 5.0

        while time.time() < deadline:
            if "url" in outcome_lower:
                url_lower = page.url.lower()
                for token in outcome_lower.split():
                    if "/" in token and token in url_lower:
                        return True

            try:
                title = await page.title()
                if any(word in title.lower() for word in outcome_lower.split() if len(word) > 3):
                    return True
            except Exception:
                pass

            await asyncio.sleep(0.5)

        return False
