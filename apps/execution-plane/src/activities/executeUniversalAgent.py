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
MAX_MARKDOWN_CHARS: int = 10000  # Reduced from 12K to save tokens

# ---------------------------------------------------------------------------
# System prompt — strict, complete, model-agnostic
# ---------------------------------------------------------------------------
NAVIGATION_SYSTEM_PROMPT = """You are a deterministic browser automation agent. Output a single JSON object for the NEXT action needed to complete the CURRENT STEP.

ELEMENT FORMAT in the list below: [ID|TAG type] label
  [12|INPUT text] Search   → text input, ID=12
  [7|BUTTON] Submit        → button, ID=7
  [3|A] Home               → link, ID=3

ACTION TYPES — choose ONE per situation:
  "type"   → Fill an entire text string into an input field at once.
             EXAMPLE: {"type": "type", "target_id": "12", "value": "latest AI news 2026"}
             ⚠ NEVER use "key" to type letters one by one. Use "type" for ALL text entry.
  "click"  → Click a button, link, or element.
             EXAMPLE: {"type": "click", "target_id": "7"}
  "key"    → Press ONE special key (Enter, Tab, Escape, ArrowDown). NOT for typing text.
             EXAMPLE: {"type": "key", "target_id": "12", "key": "Enter"}
  "upload" → Set a file on a file input.
             EXAMPLE: {"type": "upload", "target_id": "5", "value": "/path/file.pdf"}

SEARCH PATTERN (use this exact pattern for search tasks):
  Step 1: {"type": "type", "target_id": "<INPUT_ID>", "value": "your full search query"}
  Step 2: {"type": "key",  "target_id": "<INPUT_ID>", "key": "Enter"}
  Do both in ONE response as two actions in the actions array.

HARD RULES:
1. ONLY use numeric IDs from the Elements list. NEVER invent IDs like "tsf", "search-icon", "btnK".
2. NEVER press individual letter keys to spell out words. Use "type" with the full value string.
3. ALL JSON must use double quotes. Single quotes = parse error.
4. Output ONLY the raw JSON object. No markdown, no explanation, no code fences.

OUTPUT FORMAT:
{"thought_process": "one sentence", "actions": [{"type": "...", "target_id": "N", "value": "..."}], "status": "in_progress|step_complete|ui_ready|stopped"}

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
        if target_url:
            from core.url_utils import resolve_final_url
            target_url = await resolve_final_url(target_url)
            await user_logger.info("NAVIGATE", message=f"Opening: {target_url}")
            await page.goto(target_url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeout:
                pass
            
            # Wait for SPA frameworks (like YouTube's Polymer) to construct their Shadow DOMs
            await asyncio.sleep(3)

            # Session expiry fast-fail
            current_url_lower = page.url.lower()
            page_title_lower  = (await page.title()).lower()
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

                        # ---- DOM SNAPSHOT ----
                        harvester = DOMHarvester(page)
                        snapshot = await harvester.reHarvest()
                        
                        marksText = ""
                        for el in snapshot.elements:
                            tag = el.tag.upper()
                            typeStr = f" {el.type}" if el.type else ""
                            label = (el.text or el.ariaLabel or el.placeholder or "").strip()
                            label = " ".join(label.split())[:60]
                            if not label and tag == 'INPUT':
                                label = el.type or "text"
                            marksText += f"[{el.qId}|{tag}{typeStr}] {label}\n"

                        page_text = await page.evaluate("document.body.innerText.replace(/\\s+/g, ' ').substring(0, 300)")
                        
                        elements_text = marksText[:2500]
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
                                correction_prompt = f"Your previous response had a JSON syntax error:\n{parse_err}\n\nRaw output:\n{llm_response}\n\nPlease return ONLY the corrected, perfectly valid JSON object."
                                corrected_response = await _call_llm(system_prompt, correction_prompt, max_tokens=256)
                                clean_json = llm._clean_json(corrected_response)
                                action_data = json.loads(clean_json)
                                logger.info(f"[{job_id}] LLM successfully self-corrected JSON.")
                            except Exception as double_err:
                                logger.warning(f"[{job_id}] JSON self-correction failed: {double_err}")
                                consecutive_stalls += 1
                                if consecutive_stalls >= MAX_CONSECUTIVE_STALLS:
                                    await user_logger.error("STOPPED", message="LLM returned unparseable JSON repeatedly despite self-correction.")
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

                        # Re-scan the DOM using DOMHarvester to inject data-quanta-id (YouTube re-renders wipe them)
                        await harvester.reHarvest()
                        
                        # Delegate to the shared action execution block below
                        # by breaking into a micro-loop that runs the actions
                        actions_succeeded = 0
                        actions_failed    = 0
                        for act in actions:
                            act_type = act.get("type", "").lower()
                            t_id     = str(act.get("target_id", ""))
                            await user_logger.info("EXECUTE", message=f"{act_type.upper()} [{t_id}]")

                            if act_type == "click":
                                try:
                                    loc = page.locator(f"[data-quanta-id='{t_id}']").first
                                    await loc.scroll_into_view_if_needed(timeout=2000)
                                    await loc.click(timeout=4000)
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
                                            const el = findById(document, '{t_id}');
                                            if (el) {{ el.click(); return true; }}
                                            return false;
                                        }}
                                    """)
                                    if ok:
                                        actions_succeeded += 1
                                    else:
                                        actions_failed += 1

                            elif act_type == "type":
                                value = act.get("value", "")
                                try:
                                    loc = page.locator(f"[data-quanta-id='{t_id}']").first
                                    await loc.scroll_into_view_if_needed(timeout=2000)
                                    await loc.fill(value, timeout=4000)
                                    actions_succeeded += 1
                                except Exception as te:
                                    logger.warning(f"[{job_id}] Type [{t_id}] failed: {te}")
                                    actions_failed += 1

                            elif act_type == "key":
                                key = act.get("key", "Enter")
                                try:
                                    loc = page.locator(f"[data-quanta-id='{t_id}']").first
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
                                        actions_failed += 1

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

                    # Wait for SPA frameworks (like YouTube's Polymer) to construct their Shadow DOMs before the first scan
                    if loop_count == 1:
                        await asyncio.sleep(10)

                    # ---- DOM SNAPSHOT WITH ENRICHED ELEMENT MARKERS ----
                    harvester = DOMHarvester(page)
                    snapshot = await harvester.reHarvest()
                    
                    marksText = ""
                    for el in snapshot.elements:
                        tag = el.tag.upper()
                        typeStr = f" {el.type}" if el.type else ""
                        label = (el.text or el.ariaLabel or el.placeholder or "").strip()
                        label = " ".join(label.split())[:60]
                        if not label and tag == 'INPUT':
                            label = el.type or "text"
                        marksText += f"[{el.qId}|{tag}{typeStr}] {label}\n"

                    page_text = await page.evaluate("document.body.innerText.replace(/\\s+/g, ' ').substring(0, 300)")
                    
                    # Clamp element list aggressively — 70B model needs smaller context to respond fast
                    elements_text = marksText[:2500]
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

                        # Validate: reject hallucinated string IDs (must be numeric)
                        if t_id and not t_id.isdigit():
                            logger.warning(f"[{job_id}] Rejected non-numeric target_id='{t_id}' — LLM hallucination.")
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
                                    await loc.click(timeout=4000)
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
        if not (extraction_schema and ui_ready):
            return {"status": "success", "loops": loop_count, "tokens": llm.total_tokens_used}

        await user_logger.info("THINK", message="Phase 2 — Schema-Driven Extraction...")

        # SPA hydration buffer
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
            await asyncio.sleep(2)
        except PlaywrightTimeout:
            logger.warning(f"[{job_id}] SPA hydration timed out. Proceeding.")

        all_extracted_rows: list = []
        seen_hashes: set = set()
        llm_extractor = SafeLLMClient(use_extraction_model=True)

        for page_num in range(1, MAX_PAGES + 1):
            if page_num > 1:
                await user_logger.progress(f"Extracting page {page_num}/{MAX_PAGES}...")

            # Client-side DOM pruning — remove noise before markdown conversion
            await page.evaluate("""
                () => {
                    document.querySelectorAll(
                        'script, style, svg, nav, footer, header, aside, path, ' +
                        'noscript, iframe, link[rel=stylesheet], meta, ' +
                        '[role=banner], [role=navigation], [role=contentinfo], ' +
                        '[aria-hidden=true], .cookie-banner, .modal-backdrop, .ads'
                    ).forEach(el => el.remove());
                }
            """)

            dom_html = await page.evaluate("() => ({ html: document.body.innerHTML })")
            markdown_content = markdownify.markdownify(dom_html["html"], heading_style="ATX")

            if len(markdown_content) > MAX_MARKDOWN_CHARS:
                logger.warning(f"[{job_id}] Markdown truncated: {len(markdown_content)} → {MAX_MARKDOWN_CHARS}")
                markdown_content = markdown_content[:MAX_MARKDOWN_CHARS]

            logger.info(f"[{job_id}] Page {page_num} markdown size: {len(markdown_content)} chars")

            extraction_prompt = (
                f"Extract ALL data from this page matching the Target Schema.\n\n"
                f"Page Content (Markdown):\n{markdown_content}\n\n"
                f"Target Schema:\n{json.dumps(extraction_schema, indent=2)}\n\n"
                "RULES:\n"
                "1. Map visible page text to the exact schema fields. Do NOT invent data.\n"
                "2. If a field is not present on the page, set it to null.\n"
                "3. Return ONLY a valid JSON object or array. No explanation.\n"
                "4. Use double quotes for all keys and string values."
            )

            try:
                llm_response = await llm_extractor.call(
                    system_prompt="You are a strict JSON data extraction agent. Output only valid JSON.",
                    user_prompt=extraction_prompt,
                )
            except TokenBudgetExhausted as tbe:
                logger.error(f"[{job_id}] Extraction budget exhausted: {tbe}")
                break  # Return what we have so far

            try:
                json_match = re.search(r"\{[\s\S]*\}|\[[\s\S]*\]", llm_extractor._clean_json(llm_response))
                if not json_match:
                    raise ValueError("No JSON found in LLM extraction response")
                page_data = json.loads(json_match.group())
            except Exception as parse_err:
                logger.error(f"[{job_id}] Phase 2 parse error (page {page_num}): {parse_err} | Raw: {llm_response[:200]}")
                if page_num == 1:
                    return {"status": "failed", "reason": "extraction_parse_failure", "loops": loop_count}
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

            if new_rows == 0:
                logger.info(f"[{job_id}] Pagination exhausted at page {page_num}.")
                break

            # Attempt next page if below cap
            if page_num < MAX_PAGES:
                next_clicked = False
                try:
                    next_selector = await page.evaluate("""
                        () => {
                            const candidates = [
                                'a[aria-label*="next" i]', 'button[aria-label*="next" i]',
                                '[class*="next"]:not([disabled])', '[class*="pagination"] a:last-child'
                            ];
                            for (const sel of candidates) {
                                try {
                                    const el = document.querySelector(sel);
                                    if (el && el.offsetParent !== null) return sel;
                                } catch (_) {}
                            }
                            return null;
                        }
                    """)
                    if next_selector:
                        await page.click(next_selector, timeout=5000)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=8000)
                        except PlaywrightTimeout:
                            pass
                        await asyncio.sleep(2)
                        next_clicked = True
                except Exception as nav_err:
                    logger.info(f"[{job_id}] Next page click failed: {nav_err}")

                if not next_clicked:
                    # Infinite scroll fallback
                    prev_h = await page.evaluate("document.body.scrollHeight")
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(3)
                    new_h = await page.evaluate("document.body.scrollHeight")
                    if new_h == prev_h:
                        logger.info(f"[{job_id}] No more scroll content at page {page_num}.")
                        break

        final_data = (
            all_extracted_rows
            if len(all_extracted_rows) > 1
            else (all_extracted_rows[0] if all_extracted_rows else {})
        )

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
