import json
import asyncio
import traceback
import logging
from typing import Dict, Any, List

from temporalio import activity
from core.llm.safe_client import SafeLLMClient
from core.nervous_system import NervousSystem
from activities.discovery_activities import BrowserPool, ENABLE_ELEMENT_LLM
from core.browser.dom_harvester import DOMHarvester
from core.planning.intent_parser import Intent
from core.planning.element_matcher import ElementMatcher
from activities.navigation import dismiss_overlays

logger = logging.getLogger("hybridActivities")

@activity.defn(name="generateIntentSequenceActivity")
async def generateIntentSequenceActivity(payload: dict) -> dict:
    """Phase 2: LLM generates pure logical sequence (0 CSS selectors)"""
    jobId = payload.get("job_id", "unknown")
    prompt = payload.get("prompt", "")
    url = payload.get("url", "")

    await NervousSystem.publish_update(jobId, "RUNNING", "Generating logical intent sequence via LLM...", "planning")

    llmClient = SafeLLMClient()
    system_prompt = """You are a master automation planner.
Break the user's natural language prompt into a strict, logical sequence of actions.
Return ONLY valid JSON matching this schema:
{
  "steps": [
    {
      "action": "navigate" | "click" | "type" | "scrape" | "wait" | "press_key" | "scroll_down" | "format",
      "target": "Human readable target element (e.g. 'Search bar', 'Login button', 'Price value'). Leave empty for navigate/wait",
      "value": "URL for navigate, text for type, time (ms) for wait, or 'Enter' for press_key"
    }
  ]
}
RULES:
1. ALWAYS start with "navigate" action to the target URL.
2. DO NOT GUESS CSS SELECTORS EVER. The target must be descriptive plain english.
3. If extracting/getting data, use "scrape" action with the target describing what to extract.
4. After submitting a form or clicking search, usually append a "wait" action (value "2000").
"""
    user_prompt = f"Target URL: {url}\nPrompt: {prompt}"

    try:
        response = await llmClient.call(system_prompt, user_prompt, temperature=0.1)
        if "```json" in response:
            response = response.split("```json")[1].split("```")[0]
        elif "```" in response:
            response = response.split("```")[1].split("```")[0]

        data = json.loads(response.strip())
        return {"success": True, "sequence": data.get("steps", [])}
    except Exception as e:
        logger.error(f"[{jobId}] Intent sequence generation failed: {e}")
        return {"success": False, "error": str(e), "sequence": []}


@activity.defn(name="executeHybridWorkflowActivity")
async def executeHybridWorkflowActivity(payload: dict) -> dict:
    """Phase 3: Math-First DOM-Walker execution & Fallback"""
    jobId = payload.get("job_id", "unknown")
    steps = payload.get("sequence", [])
    cookies = payload.get("cookies", [])
    url = payload.get("url", "")

    await NervousSystem.publish_update(jobId, "RUNNING", "Booting headless browser for Hybrid DOM-Walk...", "execution")

    final_steps = []

    try:
        page = await BrowserPool.getPage(jobId, cookies=cookies)
        matcher = ElementMatcher()
        llmClient = SafeLLMClient()

        for i, step in enumerate(steps):
            action = step.get("action", "").lower()
            target = step.get("target", "")
            val = step.get("value", "")

            stepLog = f"[{action.upper()}] {target}"
            await NervousSystem.publish_update(jobId, "RUNNING", f"Executing: {stepLog}", "execution")
            logger.info(f"[{jobId}] Hybrid Step {i+1}: {stepLog}")

            # TELEMETRY: Node Start
            await NervousSystem.publish(
                f"quanta.telemetry.{jobId}",
                json.dumps({"type": "log", "message": f"[Executor] Starting Node: {action} -> {target}"})
            )

            # Navigational / Non-Element Actions
            if action == "navigate":
                target_url = val or target or url
                await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(1.0)
                step["selector"] = target_url
                step["success"] = True
                final_steps.append(step)
                continue

            if action == "wait":
                ms = int(val) if str(val).isdigit() else 2000
                await asyncio.sleep(ms / 1000)
                step["selector"] = "timer"
                step["success"] = True
                final_steps.append(step)
                continue

            if action == "scroll_down":
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await asyncio.sleep(0.5)
                step["selector"] = "window"
                step["success"] = True
                final_steps.append(step)
                continue

            if action == "press_key":
                await page.keyboard.press(val or "Enter")
                await asyncio.sleep(1.0)
                step["selector"] = "keyboard"
                step["success"] = True
                final_steps.append(step)
                continue

            if action == "format":
                # Pure local formatting, skip DOM
                step["selector"] = "local"
                step["success"] = True
                final_steps.append(step)
                continue

            # Interactable Elements (Click, Type, Scrape, etc)
            harvester = DOMHarvester(page)
            snapshot = await harvester.harvest()

            intent = Intent(
                stepNumber=i+1,
                action=action,
                targetDescription=target,
                value=val,
                qualifier=None,
                rawSentence="",
                confidence=1.0
            )

            matchResult = await matcher.match(intent, snapshot)
            selected_element = None

            if matchResult.found and not matchResult.escalateToLlm:
                # Math-First Match!
                selected_element = matchResult.element
                confidence = matchResult.confidence
                await NervousSystem.publish_update(jobId, "RUNNING", f"✅ Found heuristically ({(confidence*100):.0f}% match)", "execution")
            else:
                await NervousSystem.publish_update(
                    jobId, "RUNNING",
                    "⚠️ Low heuristic confidence — resolving candidates…",
                    "execution",
                )
                candidates = await matcher.getTopCandidates(intent, snapshot, n=5)

                if not candidates:
                    raise Exception(f"DOM matches extremely low. Cannot locate {target}")

                if ENABLE_ELEMENT_LLM:
                    system_prompt = (
                        "You are a specialized automation fallback node. Choose the BEST matching HTML element "
                        "for the requested action and target. Return EXACTLY AND ONLY ONE integer ID representing "
                        "the candidate. (e.g. '0' or '1')"
                    )
                    cand_str = "Candidates:\n"
                    for idx, (score, cand) in enumerate(candidates):
                        cand_str += (
                            f"[{idx}] <{cand.tag} id='{cand.id}' class='{' '.join(cand.classes)}' "
                            f"role='{cand.role}' aria='{cand.ariaLabel}'>{cand.text}</{cand.tag}>\n"
                        )

                    user_prompt = (
                        f"Action: {action}\nTarget: {target}\n\n{cand_str}\n\nWhich candidate integer matches best?"
                    )

                    llm_response = await llmClient.call(system_prompt, user_prompt, temperature=0.0)
                    llm_response = llm_response.strip()

                    idx_to_pick = 0
                    for char in llm_response:
                        if char.isdigit():
                            idx_to_pick = int(char)
                            break

                    if idx_to_pick >= len(candidates):
                        idx_to_pick = 0

                    selected_element = candidates[idx_to_pick][1]
                    await NervousSystem.publish_update(
                        jobId, "RUNNING",
                        f"🧠 AI locked element <{selected_element.tag}>",
                        "execution",
                    )
                else:
                    selected_element = candidates[0][1]
                    await NervousSystem.publish_update(
                        jobId, "RUNNING",
                        f"📍 Deterministic pick <{selected_element.tag}> "
                        f"(score={candidates[0][0]:.0%}, no AI)",
                        "execution",
                    )

            if not selected_element:
                raise Exception(f"Failed to bind element for target: {target}")

            # Execution logic using verified element
            # We strictly use the data-quanta-id injected during the harvest
            selector = f"[data-quanta-id='{selected_element.qId}']"

            step["selector"] = selector

            try:
                # Dismiss overlay if going for a click
                if action == "click":
                    await dismiss_overlays(page)

                el_handle = await page.wait_for_selector(selector, state="visible", timeout=10000)
                if not el_handle:
                    # If we somehow missed visibility but it's attached
                    el_handle = await page.wait_for_selector(selector, state="attached", timeout=5000)

                if not el_handle:
                    raise Exception(f"Playwright unable to attach to: {selector}")

                if action == "click":
                    await el_handle.click()
                    await asyncio.sleep(1.0)
                elif action == "type":
                    await el_handle.fill(val)
                    await asyncio.sleep(0.5)
                if action == "scrape":
                    text = await el_handle.inner_text()
                    step["scrapedValue"] = text
                    # TELEMETRY: Extraction Payload
                    await NervousSystem.publish(
                        f"quanta.telemetry.{jobId}",
                        json.dumps({"type": "log", "message": f"[Extractor] Payload: {text}"})
                    )

                step["success"] = True
                final_steps.append(step)

            except Exception as ex:
                err_msg = f"Failed to execute {action} on {target} ({selector}). Err: {ex}"
                logger.error(f"[{jobId}] {err_msg}")

                # TELEMETRY: Failure with Stack Trace
                stack_trace = traceback.format_exc()
                await NervousSystem.publish(
                    f"quanta.telemetry.{jobId}",
                    json.dumps({"type": "log", "message": f"[Executor] Node Failed: {err_msg}\n{stack_trace}"})
                )

                raise Exception(err_msg)

        # If we reached here, 100% of the nodes executed perfectly
        return {"success": True, "sequence": final_steps}

    except Exception as e:
        logger.error(f"[{jobId}] Hybrid execution aborted: {e}")
        return {"success": False, "error_trace": str(e), "sequence": final_steps}
