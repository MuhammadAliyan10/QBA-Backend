import logging
import json
import urllib.request
import asyncio
from typing import Dict, Any, List

from temporalio import activity
from core.llm.safe_client import SafeLLMClient
from core.nervous_system import NervousSystem
from activities.discovery_activities import BrowserPool
from core.browser.dom_harvester import DOMHarvester

logger = logging.getLogger("healingActivities")

@activity.defn(name="validateRequestActivity")
async def validateRequestActivity(payload: dict) -> dict:
    """Pre-Validation (Zero AI Cost Rule)"""
    url = payload.get("url", "")
    prompt = payload.get("prompt", "")

    if not url or not prompt:
        return {"valid": False, "error": "Error: URL and prompt are required."}

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        return {"valid": False, "error": f"Error: Target URL '{url}' is unreachable or malformed."}

    if len(prompt.split()) < 2:
        return {"valid": False, "error": "Error: Prompt lacks specific targets or actions."}

    return {"valid": True, "error": None}


@activity.defn(name="generateWorkflowMapActivity")
async def generateWorkflowMapActivity(payload: dict) -> dict:
    """Generation Phase: Invoke LLM once to generate the initial workflow map."""
    jobId = payload.get("job_id", "unknown")
    prompt = payload.get("prompt", "")
    url = payload.get("url", "")

    await NervousSystem.publish_update(jobId, "RUNNING", "Generating strict execution map via LLM...", "planning")

    llmClient = SafeLLMClient()
    system_prompt = """You are an elite automation engineer. Build a strict JSON workflow map for headless browser execution.
Return ONLY a valid JSON object matching this schema:
{
  "steps": [
    {
      "action": "navigate" | "click" | "type" | "press_key" | "wait" | "scrape",
      "target": "Human-readable target",
      "value": "Text to type or URL",
      "selector": "Best guess CSS selector (e.g. input[type='search'], button:has-text('Submit'))"
    }
  ] # Ensure steps are granular and sequential
}
1. First step MUST be "navigate" to the provided URL.
2. Follow navigation and submit actions with a "wait" action (value: "2000").
3. Make best guess CSS selectors for standard elements.
"""
    user_prompt = f"Target URL: {url}\nPrompt: {prompt}"

    try:
        response = await llmClient.call(system_prompt, user_prompt, temperature=0.1)
        if "```json" in response:
            response = response.split("```json")[1].split("```")[0]
        elif "```" in response:
            response = response.split("```")[1].split("```")[0]

        data = json.loads(response.strip())
        return {"success": True, "map": data.get("steps", [])}
    except Exception as e:
        logger.error(f"[{jobId}] Workflow map generation failed: {e}")
        return {"success": False, "error": str(e), "map": []}


@activity.defn(name="executeWorkflowStrictlyActivity")
async def executeWorkflowStrictlyActivity(payload: dict) -> dict:
    """The Assertion & Testing Phase"""
    jobId = payload.get("job_id", "unknown")
    steps = payload.get("steps", [])
    cookies = payload.get("cookies", [])

    await NervousSystem.publish_update(jobId, "RUNNING", "Executing workflow strictly...", "execution")

    try:
        # Instantiate headless browser instance
        page = await BrowserPool.getPage(jobId, cookies=cookies)

        for i, step in enumerate(steps):
            action = step.get("action", "").lower()
            target = step.get("target", "")
            val = step.get("value", "")
            selector = step.get("selector", "")

            stepLog = f"[{action.upper()}] {target}"
            await NervousSystem.publish_update(jobId, "RUNNING", f"Executing: {stepLog}", "execution")
            logger.info(f"[{jobId}] Step {i+1}: {stepLog} (selector: {selector})")

            try:
                if action == "navigate":
                    await page.goto(val or target, wait_until="domcontentloaded", timeout=30000)
                elif action == "wait":
                    ms = int(val) if str(val).isdigit() else 2000
                    await asyncio.sleep(ms / 1000)
                elif action == "click":
                    el = await page.wait_for_selector(selector, state="visible", timeout=5000)
                    if not el: raise Exception(f"Element not found for selector: {selector}")
                    await el.click()
                elif action == "type":
                    el = await page.wait_for_selector(selector, state="visible", timeout=5000)
                    if not el: raise Exception(f"Element not found for selector: {selector}")
                    await el.fill(val)
                elif action == "press_key":
                    # typically enter
                    await page.keyboard.press(val or "Enter")
                elif action == "scrape":
                    el = await page.wait_for_selector(selector, state="attached", timeout=5000)
                    if not el: raise Exception(f"Element not found for selector: {selector}")
                    scraped = await el.inner_text()
                    step["scrapedValue"] = scraped
                else:
                    raise Exception(f"Unknown action: {action}")

                step["success"] = True

            except Exception as step_err:
                # Capture Error and DOM State
                harvester = DOMHarvester(page)
                dom_snapshot = ""
                try:
                    snapshot = await harvester.harvest()
                    # Just take a simplified view of the DOM for the LLM
                    dom_snapshot = json.dumps([{"tag": e.tag, "text": e.text, "id": e.id, "class": " ".join(e.classes)} for e in snapshot.elements[:50]])
                except:
                    dom_snapshot = "Failed to capture DOM"

                error_trace = f"Step {i+1} failed ({action} {target}): {str(step_err)}"
                return {"success": False, "error_trace": error_trace, "dom_state": dom_snapshot, "failed_step_index": i}

        return {"success": True, "steps": steps}

    except Exception as e:
        return {"success": False, "error_trace": f"Execution setup failed: {str(e)}", "dom_state": "", "failed_step_index": -1}


@activity.defn(name="healWorkflowActivity")
async def healWorkflowActivity(payload: dict) -> dict:
    """The Self-Healing Loop"""
    jobId = payload.get("job_id", "unknown")
    failed_workflow_json = payload.get("failed_map", [])
    error_trace = payload.get("error_trace", "")
    dom_state = payload.get("dom_state", "")

    await NervousSystem.publish_update(jobId, "RUNNING", f"Self-healing triggered. Analyzing error...", "repair")
    logger.info(f"[{jobId}] Healing workflow. Error: {error_trace}")

    llmClient = SafeLLMClient()
    system_prompt = """You are an elite automation engineer fixing a JSON workflow map.
The current workflow execution failed. Replace the broken selector or fix the sequence based on the error trace and provided DOM snapshot.
Return ONLY a valid JSON object matching this schema:
{
  "steps": [
    { "action": "...", "target": "...", "value": "...", "selector": "..." }
  ]
}
"""
    user_prompt = f"Failed Map:\n{json.dumps(failed_workflow_json, indent=2)}\n\nError Trace:\n{error_trace}\n\nDOM State (partial):\n{dom_state}\n\nFix the JSON workflow map."

    try:
        response = await llmClient.call(system_prompt, user_prompt, temperature=0.1)
        if "```json" in response:
            response = response.split("```json")[1].split("```")[0]
        elif "```" in response:
            response = response.split("```")[1].split("```")[0]

        data = json.loads(response.strip())
        return {"success": True, "map": data.get("steps", [])}
    except Exception as e:
        logger.error(f"[{jobId}] LLM Healing failed: {e}")
        return {"success": False, "error": str(e), "map": failed_workflow_json}
