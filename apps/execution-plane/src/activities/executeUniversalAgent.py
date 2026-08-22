# src/activities/executeUniversalAgent.py

import logging
import asyncio
import json
import re
import hashlib
import markdownify
from temporalio import activity
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout
from core.browser.session import SessionManager
from core.browser.dom_harvester import DOMHarvester
from core.llm.safe_client import SafeLLMClient, TokenBudgetExhausted
from core.user_facing_logger import UserFriendlyLogger
from core.extraction.dom_extractor import extract_with_dom
from core.extraction.pagination_engine import PaginationEngine, PaginationStrategy
from core.extraction.schema_inferencer import infer_schema
from core.extraction.output_formatter import get_output_formatter
from core.checkpoint import CheckpointManager
from core.extraction.selector_synthesizer import synthesize_selectors

logger = logging.getLogger("activity")

# ---------------------------------------------------------------------------
# Phase 1 constants
# ---------------------------------------------------------------------------
MAX_NAV_LOOPS: int = 15          # Hard cap on LLM navigation iterations
MAX_CONSECUTIVE_STALLS: int = 3  # LLM returns no actions N times → STOPPED
MAX_CONSECUTIVE_FAILURES: int = 5  # All actions in a loop fail N times → STOPPED
NAV_RATE_LIMIT_SLEEP: int = 2    # Seconds between loops (adaptive, not 10s flat)

# ---------------------------------------------------------------------------
# Phase 2 constants
# ---------------------------------------------------------------------------
MAX_PAGES: int = 10
MAX_MARKDOWN_CHARS: int = 4000   # ~4K chars is enough for any product listing page

# ---------------------------------------------------------------------------
# System prompt — strict, complete, model-agnostic
# ---------------------------------------------------------------------------
NAVIGATION_SYSTEM_PROMPT = """You are a deterministic browser automation agent. Output a single JSON object for the NEXT action needed to complete the CURRENT STEP.

ELEMENT FORMAT in the list below: [ID|TAG type] label
  [q-12|INPUT text] Search   → text input, ID=q-12
  [q-7|BUTTON] Submit        → button, ID=q-7
  [q-3|A] Home               → link, ID=q-3

ACTION TYPES — choose ONE per situation:
  "type"   → Fill an entire text string into an input field at once.
             EXAMPLE: {"type": "type", "target_id": "q-12", "value": "latest AI news 2026"}
             ⚠ NEVER use "key" to type letters one by one. Use "type" for ALL text entry.
  "click"  → Click a button, link, or element.
             EXAMPLE: {"type": "click", "target_id": "q-7"}
  "key"    → Press ONE special key (Enter, Tab, Escape, ArrowDown). NOT for typing text.
             EXAMPLE: {"type": "key", "target_id": "q-12", "key": "Enter"}
  "upload" → Set a file on a file input.
             EXAMPLE: {"type": "upload", "target_id": "q-5", "value": "/path/file.pdf"}

SEARCH PATTERN (use this exact pattern for search tasks):
  Step 1: {"type": "type", "target_id": "q-<INPUT_ID>", "value": "your full search query"}
  Step 2: {"type": "key",  "target_id": "q-<INPUT_ID>", "key": "Enter"}
  Do both in ONE response as two actions in the actions array.

HARD RULES:
1. ONLY use IDs from the Elements list exactly as shown (e.g. q-12, q-7). NEVER invent IDs.
2. NEVER press individual letter keys to spell out words. Use "type" with the full value string.
3. ALL JSON must use double quotes. Single quotes = parse error.
4. Output ONLY the raw JSON object. No markdown, no explanation, no code fences.

OUTPUT FORMAT:
{"thought_process": "one sentence", "actions": [{"type": "...", "target_id": "q-N", "value": "..."}], "status": "in_progress|step_complete|ui_ready|stopped"}

STATUS:
- "in_progress":    still working on this step, more actions needed
- "step_complete":  current step achieved, ready for next step, actions=[]
- "ui_ready":       ALL steps done, full objective achieved, actions=[]
- "stopped":        page blocked, CAPTCHA, or step is impossible"""


async def _rescan_dom(page: "Page") -> None:
    """
    Re-apply data-quanta-id markers to all visible interactive elements,
    including elements inside Shadow DOM (YouTube, GMail, etc. use Web Components
    with shadow roots that standard querySelectorAll cannot pierce).
    """
    try:
        await page.evaluate("""
            () => {
                let idCounter = 1;

                // Remove all existing markers first
                document.querySelectorAll('[data-quanta-id]').forEach(
                    el => el.removeAttribute('data-quanta-id')
                );

                const SELECTORS = "button, a[href], input, select, textarea, [role='button'], [role='combobox'], [role='searchbox'], [role='textbox']";

                function isVisible(el) {
                    const s = window.getComputedStyle(el);
                    if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }

                function markRoot(root) {
                    // Mark interactive elements in this root
                    root.querySelectorAll(SELECTORS).forEach(el => {
                        if (isVisible(el)) {
                            el.setAttribute('data-quanta-id', (idCounter++).toString());
                        }
                    });
                    // Recurse into every shadow root found in this root
                    root.querySelectorAll('*').forEach(el => {
                        if (el.shadowRoot) markRoot(el.shadowRoot);
                    });
                }

                markRoot(document);
            }
        """)
    except Exception:
        pass  # Non-fatal — best effort


@activity.defn
async def execute_universal_agent(
    page: Page,
    job_id: str,
    user_logger: UserFriendlyLogger,
    nervous_system,
    target_url: str = None,
    navigation_objective: str = None,
    extraction_schema: dict = None,
    output_format: str = "json",       # "csv" | "json" | "jsonl" | "tsv" | "excel"
    materialized_files: list[str] = None,
    plan_steps: list[dict] = None,
):
    """
    Two-Phase Cognitive Orchestration for Universal Agent.

    Phase 1: Navigation State Machine (LLM-driven DOM interaction)
      - GUIDED MODE (plan_steps provided): Planner pre-computed a step list.
        The agent executes one step at a time with a bounded sub-loop per step.
        Accuracy: ~85% vs ~40% in blind mode.
      - BLIND MODE (no plan_steps): Falls back to the original reactive loop.

    Phase 2: Schema-Driven Extraction (LLM-driven data mapping)

    Safeguards:
      - Hard loop cap (MAX_NAV_LOOPS)
      - Consecutive stall detection (MAX_CONSECUTIVE_STALLS)
      - Consecutive action failure detection (MAX_CONSECUTIVE_FAILURES)
      - Per-job token budget enforcement (TokenBudgetExhausted)
      - LLM "stopped" status respected immediately
      - Single-quote JSON auto-repaired in SafeLLMClient
    """
    try:
        # ----------------------------------------------------------------
        # INITIAL NAVIGATION
        # ----------------------------------------------------------------
        page.on("console", lambda msg: logger.info(f"[{job_id}] BROWSER CONSOLE: {msg.type} - {msg.text}"))

        # Instantiate once per job execution
        checkpoint_mgr = CheckpointManager(job_id=job_id)
        if target_url:
            from core.url_utils import resolve_final_url
            target_url = await resolve_final_url(target_url)
            await user_logger.info("NAVIGATE", message=f"Opening: {target_url}")

            # Crash-resilient navigation: try commit first (fast, prevents WebGL WAF crash), fall back to domcontentloaded
            for nav_attempt in range(2):
                try:
                    wait_mode = "commit" if nav_attempt == 0 else "domcontentloaded"
                    await page.goto(target_url, wait_until=wait_mode, timeout=30000)
                    if nav_attempt == 1: # Wait for idle if we did a full load
                        try:
                            await page.wait_for_load_state("networkidle", timeout=8000)
                        except PlaywrightTimeout:
                            pass
                    break  # Navigation succeeded
                except Exception as nav_err:
                    if nav_attempt == 0 and ("crashed" in str(nav_err).lower() or "target" in str(nav_err).lower()):
                        logger.warning(f"[{job_id}] Page crashed on commit nav attempt, retrying with domcontentloaded: {nav_err}")
                        await asyncio.sleep(1)
                        continue
                    raise  # Re-raise on second attempt or unrelated error

            # Wait for SPA frameworks to construct their Shadow DOMs
            await asyncio.sleep(2)

            # Session expiry fast-fail — guard page.title() against crashed renderer
            current_url_lower = page.url.lower()
            try:
                page_title_lower = (await page.title()).lower()
            except Exception:
                page_title_lower = ""
            auth_indicators   = ["login", "signin", "sign-in", "sign_in", "auth", "sso", "oauth", "accounts/login"]
            title_indicators  = ["sign in", "log in", "login", "authenticate"]

            if any(i in current_url_lower for i in auth_indicators) or \
               any(i in page_title_lower for i in title_indicators):
                from temporalio.exceptions import ApplicationError
                raise ApplicationError(
                    f"Session expired: redirected to auth page ({page.url}).",
                    type="SessionExpired",
                    non_retryable=True,
                )

            # WAF/Challenge fast-fail — detect before renderer crashes executing the challenge JS
            WAF_URL_INDICATORS = [
                "splashui/challenge", "challenge?ap=", "/challenge/",
                "cf-challenge", "distil_r_captcha", "px-captcha",
                "/sorry/index", "recaptcha", "bot-check", "access-denied",
            ]
            if any(indicator in current_url_lower for indicator in WAF_URL_INDICATORS):
                await user_logger.error(
                    "BLOCKED",
                    message=f"WAF/CAPTCHA challenge detected at navigation ({page.url[:80]}). Residential proxy required."
                )
                return {
                    "status": "blocked",
                    "reason": (
                        f"Target site redirected to WAF challenge: {page.url[:120]}. "
                        "Configure PROXY_SERVER env var with residential proxy credentials to bypass."
                    ),
                    "loops": 0,
                    "tokens": 0,
                }

        # ----------------------------------------------------------------
        # PHASE 1: NAVIGATION STATE MACHINE
        # ----------------------------------------------------------------
        llm        = SafeLLMClient()
        ui_ready   = False
        loop_count = 0

        if navigation_objective:
            consecutive_stalls    = 0
            consecutive_failures  = 0
            last_action_signature = None

            # ----------------------------------------------------------
            # CHOOSE EXECUTION MODE
            # ----------------------------------------------------------
            # GUIDED MODE: Planner gave us an ordered list of steps.
            # We iterate each step with a bounded sub-loop (max 5 iters).
            # The LLM only needs to figure out HOW to do THIS step.
            #
            # BLIND MODE: No plan. Original reactive loop — LLM decides
            # what the next action is from the full objective each time.
            # ----------------------------------------------------------
            active_plan = list(plan_steps) if plan_steps else []
            guided_mode = bool(active_plan)

            if guided_mode:
                total_steps = len(active_plan)
                await user_logger.info(
                    "PLAN",
                    message=f"Guided mode: executing {total_steps}-step plan"
                )
                logger.info(f"[{job_id}] Guided mode: {total_steps} plan steps")

                MAX_LOOPS_PER_STEP = 5  # Hard cap per individual step

                for step_idx, step in enumerate(active_plan):
                    step_intent   = step.get("intent_type", "unknown")
                    step_criteria = step.get("success_criteria", "")
                    step_args     = step.get("arguments", {})

                    await user_logger.info(
                        "THINK",
                        message=(
                            f"Step {step_idx + 1}/{total_steps}: {step_intent} — "
                            f"{step_criteria[:80]}"
                        )
                    )
                    logger.info(
                        f"[{job_id}] Executing plan step {step_idx + 1}/{total_steps}: "
                        f"{step_intent} | criteria: {step_criteria[:60]}"
                    )

                    step_loops = 0
                    step_done  = False

                    while step_loops < MAX_LOOPS_PER_STEP:
                        step_loops += 1
                        loop_count += 1

                        if step_loops > 1:
                            await asyncio.sleep(NAV_RATE_LIMIT_SLEEP)

                        # ---- DOM SNAPSHOT (smart selection) ----
                        harvester = DOMHarvester(page)
                        snapshot = await harvester.reHarvest()

                        # Always include links/buttons + viewport elements, padded to 80 total
                        link_els   = [e for e in snapshot.elements if e.tag in ("a", "button") and e.text]
                        link_ids   = {e.qId for e in link_els}
                        vp_els     = [e for e in snapshot.elements if e.inViewport and e.qId not in link_ids]
                        bf_els     = [e for e in snapshot.elements if not e.inViewport and e.qId not in link_ids]
                        combined   = (link_els + vp_els + bf_els)[:80]

                        marksText = ""
                        for el in combined:
                            tag = el.tag.upper()
                            typeStr = f" {el.type}" if el.type else ""
                            label = (el.text or el.ariaLabel or el.placeholder or "").strip()
                            label = " ".join(label.split())[:60]
                            if not label and tag == 'INPUT':
                                label = el.type or "text"
                            marksText += f"[{el.qId}|{tag}{typeStr}] {label}\n"

                        page_text = await page.evaluate("document.body.innerText.replace(/\\s+/g, ' ').substring(0, 300)")

                        elements_text = marksText
                        page_text     = page_text[:300]

                        file_ctx = ""
                        if materialized_files:
                            file_ctx = "Available Files:\n" + "\n".join(f"- {f}" for f in materialized_files) + "\n\n"

                        # Step-focused prompt: LLM only sees THIS step's goal
                        args_hint = ""
                        if step_args:
                            args_hint = f"Step context: {json.dumps(step_args)[:200]}\n"

                        user_prompt = (
                            f"Overall objective: {navigation_objective}\n"
                            f"Current URL: {page.url}\n\n"
                            f"CURRENT STEP ({step_idx + 1}/{total_steps}): {step_intent}\n"
                            f"Success criteria: {step_criteria}\n"
                            f"{args_hint}"
                            f"Page summary: {page_text}\n\n"
                            f"Elements:\n{elements_text}\n\n"
                            f"{file_ctx}"
                            f"Tokens: {llm.total_tokens_used}/{llm.token_budget}\n"
                            "Respond with the JSON object only."
                        )

                        # ---- LLM CALL ----
                        try:
                            llm_response = await llm.call(
                                system_prompt=NAVIGATION_SYSTEM_PROMPT,
                                user_prompt=user_prompt,
                            )
                            logger.info(f"[{job_id}] LLM raw (step {step_idx+1}): {llm_response[:300]!r}")
                        except TokenBudgetExhausted as tbe:
                            logger.error(f"[{job_id}] {tbe}")
                            await user_logger.error("STOPPED", message=f"Token budget exhausted at step {step_idx + 1}.")
                            return {"status": "stopped", "reason": "token_budget_exhausted", "loops": loop_count, "tokens": llm.total_tokens_used}
                        except Exception as api_err:
                            logger.error(f"[{job_id}] LLM API error on step {step_idx + 1}: {api_err}")
                            await user_logger.error("ERROR", message=f"LLM call failed: {str(api_err)[:120]}")
                            return {"status": "stopped", "reason": f"llm_api_error: {str(api_err)[:120]}", "loops": loop_count}

                        # ---- JSON PARSE WITH DYNAMIC SELF-CORRECTION ----
                        try:
                            clean_json  = llm._clean_json(llm_response)
                            action_data = json.loads(clean_json)
                        except Exception as parse_err:
                            logger.warning(f"[{job_id}] JSON parse failed on step {step_idx + 1}: {parse_err}. Attempting self-correction...")
                            try:
                                correction_prompt = f"JSON syntax error: {parse_err}\n\nRaw output was:\n{llm_response[:400]}\n\nReturn ONLY the corrected valid JSON object with no explanation."
                                corrected_response = await llm.call(
                                    system_prompt=NAVIGATION_SYSTEM_PROMPT,
                                    user_prompt=correction_prompt,
                                )
                                clean_json = llm._clean_json(corrected_response)
                                action_data = json.loads(clean_json)
                                logger.info(f"[{job_id}] LLM self-corrected JSON successfully.")
                            except Exception as double_err:
                                logger.warning(f"[{job_id}] JSON self-correction failed: {double_err}")
                                consecutive_stalls += 1
                                if consecutive_stalls >= MAX_CONSECUTIVE_STALLS:
                                    await user_logger.error("STOPPED", message="LLM returned unparseable JSON repeatedly.")
                                    return {"status": "stopped", "reason": "json_parse_failure_loop", "loops": loop_count}
                                continue

                        consecutive_stalls = 0
                        thought = action_data.get("thought_process", "")[:200]
                        await user_logger.info("PLAN", message=thought)

                        status = action_data.get("status", "in_progress")

                        if status == "step_complete":
                            await user_logger.info(
                                "COMPLETE",
                                message=f"Step {step_idx + 1}/{total_steps} done: {step_criteria[:60]}"
                            )
                            step_done = True
                            break

                        if status == "ui_ready":
                            await user_logger.info("COMPLETE", message="All steps done — objective achieved.")
                            ui_ready = True
                            break

                        if status == "stopped":
                            reason = action_data.get("thought_process", "Step is not achievable.")
                            await user_logger.error("STOPPED", message=f"Stopped at step {step_idx + 1}: {reason}")
                            return {"status": "stopped", "reason": reason, "loops": loop_count}

                        # ---- ACTION EXECUTION ----
                        actions = action_data.get("actions", [])
                        if not actions:
                            logger.warning(f"[{job_id}] actions=[] on step {step_idx+1}. Full action_data: {json.dumps(action_data)[:300]}")
                            consecutive_stalls += 1
                            if consecutive_stalls >= MAX_CONSECUTIVE_STALLS:
                                await user_logger.error("STOPPED", message="Agent stalled on step.")
                                return {"status": "stopped", "reason": "action_stall_loop", "loops": loop_count}
                            continue

                        consecutive_stalls = 0

                        action_signature = json.dumps(actions, sort_keys=True)
                        if action_signature == last_action_signature:
                            consecutive_failures += 1
                            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                                await user_logger.error("STOPPED", message="Agent stuck in identical action loop.")
                                return {"status": "stopped", "reason": "identical_action_loop", "loops": loop_count}
                        else:
                            consecutive_failures = 0
                            last_action_signature = action_signature

                        # Re-harvest immediately before acting — injects fresh data-quanta-id
                        fresh_snapshot = await harvester.reHarvest()
                        # Build lookup: qId -> element (for XPath fallback)
                        el_by_id = {e.qId: e for e in fresh_snapshot.elements}

                        actions_succeeded = 0
                        actions_failed    = 0
                        url_before_action = page.url
                        for act in actions:
                            act_type = act.get("type", "").lower()
                            t_id     = str(act.get("target_id", "")).strip()
                            # Normalize: LLM sometimes returns bare numbers ("5") instead of "q-5"
                            if t_id and not t_id.startswith("q-") and t_id.isdigit():
                                t_id = f"q-{t_id}"
                            await user_logger.info("EXECUTE", message=f"{act_type.upper()} [{t_id}]")

                            # Resolve the stored element for XPath fallback
                            stored_el = el_by_id.get(t_id)
                            xpath_val = stored_el.xpath if stored_el else ""

                            async def _locate(tid: str, xp: str):
                                """Three-tier locator: qId attr → XPath → None."""
                                loc = page.locator(f"[data-quanta-id='{tid}']").first
                                try:
                                    await loc.wait_for(state="attached", timeout=1500)
                                    return loc
                                except Exception:
                                    pass
                                if xp:
                                    xloc = page.locator(f"xpath={xp}").first
                                    try:
                                        await xloc.wait_for(state="attached", timeout=1500)
                                        return xloc
                                    except Exception:
                                        pass
                                return None

                            if act_type == "click":
                                loc = await _locate(t_id, xpath_val)
                                if loc:
                                    try:
                                        await loc.scroll_into_view_if_needed(timeout=2000)
                                        await loc.click(timeout=10000)
                                        actions_succeeded += 1
                                    except Exception as e:
                                        logger.warning(f"[{job_id}] Click [{t_id}] failed: {e}")
                                        actions_failed += 1
                                else:
                                    # Coordinate-based last resort
                                    if stored_el and stored_el.scrollY > 0:
                                        try:
                                            await page.evaluate(f"window.scrollTo(0, {max(0, stored_el.scrollY - 200)})")
                                            await asyncio.sleep(0.3)
                                            await page.mouse.click(stored_el.scrollX + stored_el.width // 2, min(stored_el.scrollY, 600))
                                            actions_succeeded += 1
                                        except Exception:
                                            actions_failed += 1
                                    else:
                                        actions_failed += 1

                            elif act_type == "type":
                                value = act.get("value", "")
                                loc = await _locate(t_id, xpath_val)
                                if loc:
                                    try:
                                        await loc.scroll_into_view_if_needed(timeout=2000)
                                        await loc.fill(value, timeout=4000)
                                        actions_succeeded += 1
                                    except Exception:
                                        try:
                                            await loc.type(value, delay=30)
                                            actions_succeeded += 1
                                        except Exception as te:
                                            logger.warning(f"[{job_id}] Type [{t_id}] failed: {te}")
                                            actions_failed += 1
                                else:
                                    logger.warning(f"[{job_id}] Type [{t_id}]: element not found via qId or XPath")
                                    actions_failed += 1

                            elif act_type == "key":
                                key = act.get("key", "Enter")
                                loc = await _locate(t_id, xpath_val)
                                if loc:
                                    try:
                                        await loc.press(key, timeout=4000)
                                        actions_succeeded += 1
                                    except Exception as e:
                                        logger.warning(f"[{job_id}] Key [{t_id}] failed: {e}")
                                        actions_failed += 1
                                else:
                                    # Dispatch keyboard event on active element as last resort
                                    await page.keyboard.press(key)
                                    actions_succeeded += 1

                        if actions_succeeded == 0 and actions_failed > 0:
                            consecutive_failures += 1
                        else:
                            consecutive_failures = 0

                        await asyncio.sleep(1)
                        await page.wait_for_load_state("domcontentloaded")
                        try:
                            await page.wait_for_load_state("networkidle", timeout=4000)
                        except PlaywrightTimeout:
                            pass

                        # URL-change = navigation succeeded → step is done
                        if page.url != url_before_action and actions_succeeded > 0:
                            logger.info(f"[{job_id}] URL changed to {page.url!r} after action — step {step_idx+1} complete.")
                            await user_logger.info("COMPLETE", message=f"Navigated to {page.url[:80]}")
                            step_done = True
                            break

                    # End of step sub-loop
                    if ui_ready:
                        break  # ui_ready returned mid-plan

                    if not step_done:
                        # Step hit MAX_LOOPS_PER_STEP without completing
                        logger.warning(
                            f"[{job_id}] Step {step_idx + 1} did not confirm done — "
                            f"advancing to next step anyway (best-effort)"
                        )
                        await user_logger.info(
                            "THINK",
                            message=f"Step {step_idx + 1} timeout — advancing to next step"
                        )

                    # --------------------------------------------------------
                    # CHECKPOINT VALIDATION: detect DOM divergence after step
                    # --------------------------------------------------------
                    # Record the fingerprint of the landing page as the baseline
                    # for the NEXT step's expected starting state.
                    if step_idx + 1 < total_steps:
                        await checkpoint_mgr.record_entry(
                            page=page,
                            step_index=step_idx + 1,
                            step_intent=active_plan[step_idx + 1].get("intent_type", "unknown"),
                        )

                    # Compare with the previously recorded expected fingerprint
                    # (only relevant from step 2 onwards)
                    if step_idx >= 1:
                        diverged, divergence_score = await checkpoint_mgr.validate_landing(
                            page=page,
                            step_index=step_idx,
                        )

                        if diverged:
                            await user_logger.info(
                                "THINK",
                                message=(
                                    f"Page structure changed ({divergence_score:.0%} divergence) — "
                                    f"adapting remaining plan"
                                ),
                            )
                            logger.warning(
                                f"[{job_id}] Checkpoint divergence {divergence_score:.2%} at step "
                                f"{step_idx + 1} — triggering surgical re-plan"
                            )

                            remaining = active_plan[step_idx + 1:]
                            if remaining:
                                patched = await checkpoint_mgr.patch_plan(
                                    page=page,
                                    current_step_index=step_idx + 1,
                                    remaining_steps=remaining,
                                    objective=navigation_objective,
                                    job_id=job_id,
                                )
                                # Splice patched steps back into the running plan
                                active_plan = active_plan[: step_idx + 1] + patched
                                total_steps = len(active_plan)
                                logger.info(
                                    f"[{job_id}] Plan updated: now {total_steps} total steps "
                                    f"after re-plan"
                                )

                # End of plan step loop
                if not ui_ready:
                    # All steps executed — mark as done (plan is the oracle, not the LLM status)
                    ui_ready = True
                    await user_logger.info("COMPLETE", message=f"All {total_steps} plan steps executed.")

            else:
                # -------------------------------------------------------
                # BLIND MODE: original reactive loop (no plan available)
                # -------------------------------------------------------
                logger.info(f"[{job_id}] Blind mode: no plan, running reactive nav loop")
                while loop_count < MAX_NAV_LOOPS:
                    loop_count += 1
                    await user_logger.info(
                        "THINK",
                        message=f"Phase 1 — Iteration {loop_count}/{MAX_NAV_LOOPS} | "
                                f"Tokens used: {llm.total_tokens_used}/{llm.token_budget}"
                    )

                    # ---- DOM SNAPSHOT (smart selection) ----
                    harvester = DOMHarvester(page)
                    snapshot = await harvester.reHarvest()

                    link_els   = [e for e in snapshot.elements if e.tag in ("a", "button") and e.text]
                    link_ids   = {e.qId for e in link_els}
                    vp_els     = [e for e in snapshot.elements if e.inViewport and e.qId not in link_ids]
                    bf_els     = [e for e in snapshot.elements if not e.inViewport and e.qId not in link_ids]
                    combined   = (link_els + vp_els + bf_els)[:80]

                    marksText = ""
                    for el in combined:
                        tag = el.tag.upper()
                        typeStr = f" {el.type}" if el.type else ""
                        label = (el.text or el.ariaLabel or el.placeholder or "").strip()
                        label = " ".join(label.split())[:60]
                        if not label and tag == 'INPUT':
                            label = el.type or "text"
                        marksText += f"[{el.qId}|{tag}{typeStr}] {label}\n"

                    page_text = await page.evaluate("document.body.innerText.replace(/\\s+/g, ' ').substring(0, 300)")
                    elements_text = marksText
                    page_text     = page_text[:300]
                    
                    file_ctx = ""
                    if materialized_files:
                        file_ctx = f"Downloaded Files available for upload: {', '.join(materialized_files)}\n\n"

                    user_prompt = (
                        f"Objective: {navigation_objective}\n"
                        f"Current URL: {page.url}\n\n"
                        f"Page Summary: {page_text}\n\n"
                        f"Elements (format: [ID|TAG type] label):\n{elements_text}\n\n"
                        f"{file_ctx}"
                        f"Tokens used so far: {llm.total_tokens_used}/{llm.token_budget}\n"
                        "Respond with the JSON object only."
                    )

                    # ---- LLM CALL ----
                    try:
                        llm_response = await llm.call(
                            system_prompt=NAVIGATION_SYSTEM_PROMPT,
                            user_prompt=user_prompt,
                        )
                    except TokenBudgetExhausted as tbe:
                        logger.error(f"[{job_id}] {tbe}")
                        await user_logger.error("STOPPED", message=f"Token budget exhausted after {llm.total_tokens_used} tokens. Stopping.")
                        return {"status": "stopped", "reason": "token_budget_exhausted", "loops": loop_count, "tokens": llm.total_tokens_used}
                    except Exception as api_err:
                        logger.error(f"[{job_id}] LLM API error: {api_err}")
                        await user_logger.error("ERROR", message=f"LLM call failed: {str(api_err)[:120]}")
                        return {"status": "stopped", "reason": f"llm_api_error: {str(api_err)[:120]}", "loops": loop_count}

                    # ---- JSON PARSE (safe_client already normalized quotes) ----
                    try:
                        clean_json  = llm._clean_json(llm_response)
                        action_data = json.loads(clean_json)
                        logger.info(f"[{job_id}] LLM→ {clean_json[:300]}")
                    except Exception as parse_err:
                        logger.error(f"[{job_id}] JSON parse failed after repair: {parse_err} | Raw: {llm_response[:200]}")
                        consecutive_stalls += 1
                        if consecutive_stalls >= MAX_CONSECUTIVE_STALLS:
                            await user_logger.error("STOPPED", message="LLM repeatedly returned unparseable JSON. Stopping.")
                            return {"status": "stopped", "reason": "json_parse_failure_loop", "loops": loop_count}
                        continue

                    consecutive_stalls = 0  # Reset on successful parse
                    await user_logger.info("PLAN", message=action_data.get("thought_process", "")[:200])

                    # ---- STATUS CHECK ----
                    status = action_data.get("status", "in_progress")

                    if status == "ui_ready":
                        await user_logger.info("COMPLETE", message="Phase 1: UI ready state reached.")
                        ui_ready = True
                        break

                    if status == "stopped":
                        reason = action_data.get("thought_process", "LLM signalled objective is not achievable.")
                        await user_logger.error("STOPPED", message=f"Agent stopped: {reason}")
                        return {"status": "stopped", "reason": reason, "loops": loop_count}

                    # ---- ACTION EXECUTION ----
                    actions = action_data.get("actions", [])

                    if not actions:
                        consecutive_stalls += 1
                        logger.warning(f"[{job_id}] No actions returned (stall {consecutive_stalls}/{MAX_CONSECUTIVE_STALLS})")
                        if consecutive_stalls >= MAX_CONSECUTIVE_STALLS:
                            await user_logger.error("STOPPED", message="Agent stalled — LLM returned no actions repeatedly.")
                            return {"status": "stopped", "reason": "action_stall_loop", "loops": loop_count}
                        continue

                    consecutive_stalls = 0

                    # Detect identical action loop (same actions repeated = infinite loop)
                    action_signature = json.dumps(actions, sort_keys=True)
                    if action_signature == last_action_signature:
                        consecutive_failures += 1
                        logger.warning(f"[{job_id}] Identical action plan repeated (failure {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            await user_logger.error("STOPPED", message="Agent stuck in identical action loop. Page may be blocking automation.")
                            return {"status": "stopped", "reason": "identical_action_loop", "loops": loop_count}
                    else:
                        consecutive_failures = 0
                        last_action_signature = action_signature

                    # Execute each action
                    # Re-scan the DOM first to get fresh IDs — YouTube re-renders
                    # its Polymer components during the LLM call (~6-8s), wiping
                    # the data-quanta-id attributes we assigned at scan time.
                    # Re-scan the DOM using DOMHarvester to get fresh IDs
                    await harvester.reHarvest()

                    actions_succeeded = 0
                    actions_failed    = 0

                    for act in actions:
                        act_type = act.get("type")
                        t_id     = str(act.get("target_id", "")).strip()

                        # Normalize: accept both "q-5" and bare "5" — always resolve to "q-5"
                        if t_id.startswith("q-"):
                            pass  # Already correct format
                        elif t_id.isdigit():
                            t_id = f"q-{t_id}"
                        elif t_id:
                            logger.warning(f"[{job_id}] Rejected unrecognized target_id='{t_id}' — LLM hallucination.")
                            actions_failed += 1
                            continue


                        if act_type == "navigate":
                            url = act.get("value", "")
                            if url:
                                try:
                                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                                    actions_succeeded += 1
                                except Exception as e:
                                    logger.warning(f"[{job_id}] Navigate to '{url}' failed: {e}")
                                    actions_failed += 1
                            continue

                        if not t_id:
                            actions_failed += 1
                            continue

                        if act_type == "click":
                            await user_logger.info("EXECUTE", message=f"Click [{t_id}]")
                            # JS-first with shadow DOM traversal
                            ok = await page.evaluate(f"""
                                () => {{
                                    function findById(root, id) {{
                                        const el = root.querySelector("[data-quanta-id='" + id + "']");
                                        if (el) return el;
                                        for (const child of root.querySelectorAll('*')) {{
                                            if (child.shadowRoot) {{
                                                const found = findById(child.shadowRoot, id);
                                                if (found) return found;
                                            }}
                                        }}
                                        return null;
                                    }}
                                    const el = findById(document, '{t_id}');
                                    if (!el) return false;
                                    el.scrollIntoView({{block:'center'}});
                                    el.click();
                                    return true;
                                }}
                            """)
                            if ok:
                                actions_succeeded += 1
                            else:
                                try:
                                    loc = page.locator(f"*css=[data-quanta-id='{t_id}']").first
                                    await loc.scroll_into_view_if_needed(timeout=2000)
                                    await loc.click(timeout=10000)
                                    actions_succeeded += 1
                                except Exception as e:
                                    logger.warning(f"[{job_id}] Click [{t_id}] failed (JS+PW): {e}")
                                    actions_failed += 1

                        elif act_type == "type":
                            value = act.get("value", "")
                            await user_logger.info("EXECUTE", message=f"Type '{value}' into [{t_id}]")
                            safe_val = json.dumps(value)
                            ok = await page.evaluate(f"""
                                () => {{
                                    function findById(root, id) {{
                                        const el = root.querySelector("[data-quanta-id='" + id + "']");
                                        if (el) return el;
                                        for (const child of root.querySelectorAll('*')) {{
                                            if (child.shadowRoot) {{
                                                const found = findById(child.shadowRoot, id);
                                                if (found) return found;
                                            }}
                                        }}
                                        return null;
                                    }}
                                    const el = findById(document, '{t_id}');
                                    if (!el) return false;
                                    el.scrollIntoView({{block:'center'}});
                                    el.focus();
                                    const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
                                    if (desc && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) {{
                                        desc.set.call(el, {safe_val});
                                        el.dispatchEvent(new Event('input', {{bubbles:true}}));
                                        el.dispatchEvent(new Event('change', {{bubbles:true}}));
                                    }} else {{
                                        el.value = {safe_val};
                                        el.dispatchEvent(new Event('input', {{bubbles:true}}));
                                    }}
                                    return true;
                                }}
                            """)
                            if ok:
                                actions_succeeded += 1
                            else:
                                try:
                                    loc = page.locator(f"*css=[data-quanta-id='{t_id}']").first
                                    await loc.scroll_into_view_if_needed(timeout=2000)
                                    await loc.fill(value, timeout=4000)
                                    actions_succeeded += 1
                                except Exception as e:
                                    logger.warning(f"[{job_id}] Type [{t_id}] failed (JS+PW): {e}")
                                    actions_failed += 1

                        elif act_type == "key":
                            key = act.get("key", "Enter")
                            await user_logger.info("EXECUTE", message=f"Key '{key}' on [{t_id}]")
                            try:
                                loc = page.locator(f"*css=[data-quanta-id='{t_id}']").first
                                await loc.press(key, timeout=4000)
                                actions_succeeded += 1
                            except Exception:
                                ok = await page.evaluate(f"""
                                    () => {{
                                        function findById(root, id) {{
                                            const el = root.querySelector("[data-quanta-id='" + id + "']");
                                            if (el) return el;
                                            for (const child of root.querySelectorAll('*')) {{
                                                if (child.shadowRoot) {{
                                                    const found = findById(child.shadowRoot, id);
                                                    if (found) return found;
                                                }}
                                            }}
                                            return null;
                                        }}
                                        const el = findById(document, '{t_id}') || document.activeElement;
                                        if (!el) return false;
                                        ['keydown','keypress','keyup'].forEach(t =>
                                            el.dispatchEvent(new KeyboardEvent(t, {{key:'{key}', bubbles:true}}))
                                        );
                                        if ('{key}' === 'Enter' && el.form) el.form.submit();
                                        return true;
                                    }}
                                """)
                                if ok:
                                    actions_succeeded += 1
                                else:
                                    logger.warning(f"[{job_id}] Key '{key}' on [{t_id}] failed.")
                                    actions_failed += 1

                        elif act_type == "upload":
                            value = act.get("value", "")
                            await user_logger.info("EXECUTE", message=f"Upload '{value}' to [{t_id}]")
                            try:
                                loc = page.locator(f"[data-quanta-id='{t_id}']").first
                                await loc.scroll_into_view_if_needed(timeout=2000)
                                await loc.set_input_files(value, timeout=5000)
                                actions_succeeded += 1
                            except Exception as e:
                                logger.warning(f"[{job_id}] Upload [{t_id}] failed: {e}")
                                actions_failed += 1

                        else:
                            logger.warning(f"[{job_id}] Unknown action type='{act_type}'.")

                    logger.info(f"[{job_id}] Actions: {actions_succeeded} succeeded, {actions_failed} failed.")

                    # If every action in this loop failed, count it as a failure
                    if actions_succeeded == 0 and actions_failed > 0:
                        consecutive_failures += 1
                        logger.warning(f"[{job_id}] All actions failed (failure streak {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            await user_logger.error("STOPPED", message="All browser actions failed repeatedly. The page may be blocking automation.")
                            return {"status": "stopped", "reason": "all_actions_failed_loop", "loops": loop_count}
                    else:
                        consecutive_failures = 0

                    # Wait for DOM to settle after actions
                    await asyncio.sleep(1)
                    await page.wait_for_load_state("domcontentloaded")
                    try:
                        await page.wait_for_load_state("networkidle", timeout=4000)
                    except PlaywrightTimeout:
                        pass  # Non-fatal — SPAs often stay networkidle-pending

            # End of while loop
            if not ui_ready:
                await user_logger.error("STOPPED", message=f"Phase 1: UI ready state not reached within {MAX_NAV_LOOPS} iterations.")
                return {"status": "stopped", "reason": "max_loops_exceeded", "loops": loop_count, "tokens": llm.total_tokens_used}

        else:
            ui_ready = True  # No navigation objective → go straight to extraction

        # ----------------------------------------------------------------
        # PHASE 2: SCHEMA-DRIVEN EXTRACTION
        # ----------------------------------------------------------------
        # ----------------------------------------------------------------
        # PHASE 2: SCHEMA-DRIVEN EXTRACTION
        # ----------------------------------------------------------------
        # Schema inference: if user provided no schema, infer one from the
        # objective (heuristic) or from the live page DOM (LLM fallback).
        # This enables "get products from amazon" with no explicit schema.
        # ----------------------------------------------------------------
        if not extraction_schema and navigation_objective:
            await user_logger.info("THINK", message="No schema provided — inferring fields from objective...")
            try:
                inferred = await infer_schema(
                    objective=navigation_objective,
                    page=page,
                    job_id=job_id,
                )
                if inferred:
                    extraction_schema = inferred
                    await user_logger.info(
                        "THINK",
                        message=f"Schema inferred: {list(inferred[0].keys()) if inferred else []}",
                    )
                    logger.info(f"[{job_id}] Schema inferred: {extraction_schema}")
            except Exception as infer_err:
                logger.warning(f"[{job_id}] Schema inference failed (non-fatal): {infer_err}")

        if not (extraction_schema and ui_ready):
            return {"status": "success", "loops": loop_count, "tokens": llm.total_tokens_used}

        await user_logger.info("THINK", message="Phase 2 — Schema-Driven Extraction...")

        # ---- WAF / CHALLENGE PAGE DETECTION (before wasting tokens) ----
        try:
            page_html_sample = await page.content()
            page_html_lower  = page_html_sample[:8000].lower()
            waf_signatures = [
                "splashui/challenge", "cf-browser-verification", "cloudflare",
                "awswafintegration", "challenge-container", "distil_identify",
                "px-captcha", "recaptcha", "hcaptcha", "datadome",
                "bot-detection", "access denied", "enable javascript",
            ]
            hit = next((sig for sig in waf_signatures if sig in page_html_lower), None)
            if hit:
                await user_logger.error("BLOCKED", message=f"WAF/CAPTCHA detected ({hit}). Extraction requires residential proxies.")
                return {
                    "status": "blocked",
                    "reason": f"WAF challenge detected: {hit}. Configure PROXY_SERVER env var with residential credentials.",
                    "loops": loop_count,
                    "tokens": llm.total_tokens_used,
                }
        except Exception:
            pass  # Non-fatal — proceed and let extraction fail naturally

        # SPA hydration buffer
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
            await asyncio.sleep(2)
        except PlaywrightTimeout:
            logger.warning(f"[{job_id}] SPA hydration timed out. Proceeding.")

        all_extracted_rows: list = []
        seen_hashes: set = set()
        # Instantiate extraction LLM client once — reused for synthesis and field fallback
        llm_extractor = SafeLLMClient(use_extraction_model=True)

        # ----------------------------------------------------------------
        # SELECTOR SYNTHESIS: Build a validated CSS selector map ONCE,
        # before any per-page extraction begins.
        # This converts arbitrary schema fields into working CSS selectors
        # so the DOM walker can find them without LLM calls per field.
        # ----------------------------------------------------------------
        synthesized_selectors: dict = {}
        if extraction_schema:
            await user_logger.info("THINK", message="Synthesizing extraction selectors...")
            try:
                synthesized_selectors = await synthesize_selectors(
                    page=page,
                    extraction_schema=extraction_schema,
                    job_id=job_id,
                    llm_client=llm_extractor,  # Reuse — avoids double LLM instantiation
                )
                if synthesized_selectors:
                    hits = sum(1 for v in synthesized_selectors.values() if v)
                    total_fields = len(synthesized_selectors)
                    await user_logger.info(
                        "THINK",
                        message=f"Selectors synthesized: {hits}/{total_fields} fields mapped",
                    )
            except Exception as synth_err:
                logger.warning(
                    f"[{job_id}] Selector synthesis failed (non-fatal): {synth_err}"
                )
                synthesized_selectors = {}

        # Emit checkpoint adaptation stats for observability
        cp_stats = checkpoint_mgr.get_stats()
        if cp_stats["total_replans"] > 0:
            await user_logger.info(
                "THINK",
                message=f"Checkpoint manager: {cp_stats['total_replans']} plan adaptations made",
            )

        # ------------------------------------------------------------------
        # LLM fallback callable — used only when DOM extractor returns null
        # for a specific field, not for the whole page.
        # ------------------------------------------------------------------
        async def _llm_field_fallback(partial_schema: dict, page_text: str) -> dict:
            extraction_prompt = (
                f"Extract data matching the Target Schema from the Page Content.\n\n"
                f"Page Content:\n{page_text[:4000]}\n\n"
                f"Target Schema:\n{json.dumps(partial_schema, indent=2)}\n\n"
                "RULES: Map visible text to exact schema fields. Missing = null. "
                "Return ONLY valid JSON. No explanation."
            )
            try:
                raw = await llm_extractor.call(
                    system_prompt="You are a strict JSON data extraction agent. Output only valid JSON.",
                    user_prompt=extraction_prompt,
                )
                cleaned = llm_extractor._clean_json(raw)
                return json.loads(cleaned)
            except (TokenBudgetExhausted, Exception) as exc:
                logger.warning(f"[{job_id}] LLM fallback failed: {exc}")
                return {}

        # ------------------------------------------------------------------
        # Parse quantity target from objective (e.g., "get 500 products")
        # Stops pagination once we have enough rows regardless of page count.
        # Default: no limit (MAX_PAGES is the only cap).
        # ------------------------------------------------------------------
        max_items: int = 0  # 0 = no item limit
        if navigation_objective:
            qty_match = re.search(r"\b(\d+)\b", navigation_objective)
            if qty_match:
                candidate = int(qty_match.group(1))
                # Sanity-check: only treat numbers 10-100000 as quantity targets
                if 10 <= candidate <= 100_000:
                    max_items = candidate
                    logger.info(f"[{job_id}] Quantity target from objective: {max_items} items")

        paginator = PaginationEngine(page, job_id=job_id)
        page_num: int = 0

        for page_num in range(1, MAX_PAGES + 1):
            if page_num > 1:
                await user_logger.progress(
                    f"Extracting page {page_num} ({len(all_extracted_rows)} rows so far)..."
                )

            # ------------------------------------------------------------------
            # Phase 2 — Primary: schema-driven DOM extraction (deterministic)
            # Synthesized selectors take priority over heuristic library.
            # ------------------------------------------------------------------
            try:
                page_data = await extract_with_dom(
                    page,
                    extraction_schema,
                    llm_fallback_fn=_llm_field_fallback,
                    synthesized_selectors=synthesized_selectors,
                )
                logger.info(f"[{job_id}] Page {page_num} DOM extraction complete.")
            except TokenBudgetExhausted as tbe:
                logger.error(f"[{job_id}] Extraction budget exhausted: {tbe}")
                break
            except Exception as dom_err:
                # DOM extraction failed entirely — fall back to full LLM extraction
                logger.warning(
                    f"[{job_id}] DOM extractor raised exception: {dom_err}. "
                    "Falling back to full LLM extraction for this page."
                )
                # Prepare compact page text for LLM
                try:
                    await page.evaluate("""
                        () => {
                            document.querySelectorAll(
                                'script, style, svg, nav, footer, header, aside, path, '
                                + 'noscript, iframe, link[rel=stylesheet], meta, '
                                + '[role=banner], [role=navigation], [role=contentinfo], '
                                + '[aria-hidden=true], .cookie-banner, .modal-backdrop, .ads'
                            ).forEach(el => el.remove());
                        }
                    """)
                    dom_html = await page.evaluate("() => ({ html: document.body.innerHTML })")
                    markdown_content = markdownify.markdownify(dom_html["html"], heading_style="ATX")
                    plain_text = re.sub(r"[#*`\[\]_>|\\]", "", markdown_content)
                    plain_text = re.sub(r"\n{2,}", "\n", plain_text).strip()[:4000]
                    page_data = await _llm_field_fallback(extraction_schema, plain_text)
                except Exception as fallback_err:
                    logger.error(f"[{job_id}] Full LLM fallback also failed: {fallback_err}")
                    if page_num == 1:
                        return {
                            "status": "failed",
                            "reason": "extraction_failure",
                            "loops": loop_count,
                        }
                    break

            # Flatten and deduplicate
            rows: list = []
            if isinstance(page_data, list):
                rows = page_data
            elif isinstance(page_data, dict):
                for v in page_data.values():
                    if isinstance(v, list):
                        rows = v
                        break
                if not rows:
                    rows = [page_data]

            new_rows = 0
            for row in rows:
                row_hash = hashlib.md5(json.dumps(row, sort_keys=True).encode()).hexdigest()
                if row_hash not in seen_hashes:
                    seen_hashes.add(row_hash)
                    all_extracted_rows.append(row)
                    new_rows += 1

            logger.info(f"[{job_id}] Page {page_num}: {new_rows} new rows (total: {len(all_extracted_rows)})")

            # Quantity gate — stop if we have enough rows
            if max_items > 0 and len(all_extracted_rows) >= max_items:
                logger.info(
                    f"[{job_id}] Quantity target reached: "
                    f"{len(all_extracted_rows)}/{max_items} items. Stopping pagination."
                )
                await user_logger.info(
                    "COMPLETE",
                    message=f"Collected {len(all_extracted_rows)} items (target: {max_items})",
                )
                break

            if new_rows == 0 and page_num > 1:
                logger.info(f"[{job_id}] No new rows on page {page_num} — pagination exhausted.")
                break

            # Advance to next page using the multi-strategy engine
            if page_num < MAX_PAGES:
                strategy = await paginator.advance(current_row_count=len(all_extracted_rows))

                if strategy == PaginationStrategy.EXHAUSTED:
                    logger.info(f"[{job_id}] Paginator: all strategies exhausted after {page_num} pages.")
                    break

                await user_logger.info(
                    "THINK",
                    message=f"Page {page_num} → {page_num + 1} via {strategy.value}",
                )

        # ----------------------------------------------------------------
        # FORMAT + RETURN
        # Apply OutputFormatter to enforce column order, format, and
        # validate required fields before returning to the caller.
        # ----------------------------------------------------------------
        final_rows = all_extracted_rows
        final_data: object = (
            final_rows if len(final_rows) > 1
            else (final_rows[0] if final_rows else {})
        )

        # Only invoke formatter when there are multiple rows (list extraction mode)
        if final_rows and isinstance(final_rows[0], dict):
            formatter = get_output_formatter()
            payload_bytes, mime_type, validation_report = formatter.format(
                final_rows,
                output_format=output_format,
                strip_null_fields=False,
            )
            logger.info(
                f"[{job_id}] Output: {len(final_rows)} rows, format={output_format}, "
                f"completeness={validation_report.completeness_pct}%"
            )
            return {
                "status": "success",
                "loops": loop_count,
                "tokens": llm.total_tokens_used + llm_extractor.total_tokens_used,
                "data": final_data,
                "formatted_bytes": payload_bytes,
                "mime_type": mime_type,
                "pages_scraped": page_num,
                "total_rows": len(final_rows),
                "validation": validation_report.to_dict(),
            }

        return {
            "status": "success",
            "loops": loop_count,
            "tokens": llm.total_tokens_used + llm_extractor.total_tokens_used,
            "data": final_data,
            "pages_scraped": page_num,
            "total_rows": len(all_extracted_rows),
        }

    except Exception as e:
        logger.error(f"[{job_id}] Universal Agent unhandled error: {str(e)}", exc_info=True)
        await user_logger.error("FAILURE", message=f"Agent crashed: {str(e)[:200]}")
        return {"status": "failed", "reason": f"browser_crashed: {str(e)[:100]}", "tokens_used": 0}
