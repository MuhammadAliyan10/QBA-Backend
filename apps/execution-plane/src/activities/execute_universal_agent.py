import logging
import asyncio
import os
import json
from temporalio import activity
from playwright.async_api import Page
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout
from core.llm.safe_client import SafeLLMClient
from core.user_facing_logger import UserFriendlyLogger

logger = logging.getLogger("activity")

@activity.defn
async def execute_universal_agent(
    page: Page, 
    job_id: str, 
    user_logger: UserFriendlyLogger,
    nervous_system,
    target_url: str = None,
    navigation_objective: str = None,
    extraction_schema: dict = None
):
    """
    Two-Phase Cognitive Orchestration for Universal Agent.
    Phase 1: Navigation State Machine
    Phase 2: Schema-Driven Extraction
    """
    try:
        if target_url:
            from core.url_utils import resolve_final_url
            target_url = await resolve_final_url(target_url)
            await user_logger.info("NAVIGATE", message=f"Opening Universal Agent Target: {target_url}")
            await page.goto(target_url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeout:
                pass
        
        loop_count = 0
        stall_count = 0
        max_loops = 50
        llm = SafeLLMClient()
        ui_ready = False
        
        # ---------------------------------------------------------------------
        # PHASE 1: NAVIGATION STATE MACHINE
        # ---------------------------------------------------------------------
        if navigation_objective:
            while loop_count < max_loops:
                loop_count += 1
                await user_logger.info("THINK", message=f"Phase 1 Navigation - Analyzing DOM (Iteration {loop_count})...")
                
                # Rate Limit Protection for Free-Tier LLMs (Nvidia NIM)
                if loop_count > 1:
                    await asyncio.sleep(10)
                    
                dom_state = await page.evaluate("""
                    () => {
                        let idCounter = 1;
                        let marksText = "";
                        document.querySelectorAll('[data-quanta-id]').forEach(el => el.removeAttribute('data-quanta-id'));
                        
                        const interactiveSelectors = "button, a, input, select, textarea, [role='button'], label";
                        const elements = document.querySelectorAll(interactiveSelectors);
                        
                        elements.forEach(el => {
                            const style = window.getComputedStyle(el);
                            if (style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0') {
                                const rect = el.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0) {
                                    const id = idCounter++;
                                    el.setAttribute("data-quanta-id", id.toString());
                                    
                                    let text = el.innerText || el.value || el.getAttribute("aria-label") || el.placeholder || "";
                                    text = text.trim().replace(/\\s+/g, ' ').substring(0, 80); // reduced from 150 to save tokens
                                    if (!text && el.tagName === 'INPUT') text = el.type;
                                    
                                    if (el.tagName === 'INPUT' && (el.type === 'radio' || el.type === 'checkbox') && el.checked) {
                                        text += " [ON]";
                                    }
                                    
                                    marksText += `[${id}] ${text}\\n`; // token optimized format
                                }
                            }
                        });
                        
                        const pageText = document.body.innerText.replace(/\\s+/g, ' ').substring(0, 1000); // reduced from 2000
                        return { marksText, pageText };
                    }
                """)
                
                prompt = (
                    f"Objective: {navigation_objective}\n"
                    f"URL: {page.url}\n\n"
                    f"Context:\n{dom_state['pageText']}\n\n"
                    f"Elements:\n{dom_state['marksText']}\n\n"
                    "RULES:\n"
                    "1. Only act on visible Elements IDs. No hallucinations.\n"
                    "2. If objective complete/success state reached, return status: 'ui_ready', actions: [].\n"
                    "3. If already on target URL, do NOT click nav links again. Act on the page.\n"
                    "4. Typing: {'type': 'type', 'target_id': 'X', 'value': 'text'}. Clicking: {'type': 'click', 'target_id': 'X'}.\n"
                    "Output ONLY raw JSON:\n"
                    "{\n"
                    '  "thought_process": "brief",\n'
                    '  "actions": [{"type": "click", "target_id": "105"}],\n'
                    '  "status": "in_progress" | "ui_ready"\n'
                    "}"
                )
                
                llm_response = await llm.call(system_prompt="You are a strict JSON web agent.", user_prompt=prompt)
                
                try:
                    clean_json = llm._clean_json(llm_response)
                    logger.info(f"[{job_id}] LLM OUTPUT: {clean_json}")
                    action_data = json.loads(clean_json)
                except Exception as e:
                    logger.error(f"[{job_id}] Phase 1 JSON Parse Error: {str(e)} | Raw: {llm_response}")
                    await page.wait_for_load_state("domcontentloaded")
                    continue
                    
                await user_logger.info("PLAN", message=f"Reasoning: {action_data.get('thought_process', '')}")
                
                status = action_data.get("status", "in_progress")
                if status == "ui_ready":
                    await user_logger.info("COMPLETE", message="Phase 1 Complete: Target UI state reached.")
                    ui_ready = True
                    break
                    
                actions = action_data.get("actions", [])
                
                if not actions and status != "ui_ready":
                    stall_count += 1
                    logger.warning(f"[{job_id}] LLM returned no actions (Stall count: {stall_count}/3)")
                    if stall_count >= 3:
                        from temporalio.exceptions import ApplicationError
                        raise ApplicationError(
                            "Universal Agent stalled: LLM returned no actions 3 times in a row. The page might be blocked by a WAF or the objective is unachievable.",
                            type="UniversalAgentStallError",
                            non_retryable=True
                        )
                else:
                    stall_count = 0
                    
                for act in actions:
                    act_type = act.get("type")
                    t_id = act.get("target_id")
                    
                    if act_type == "click" and t_id:
                        await user_logger.info("EXECUTE", message=f"Clicking element ID {t_id}")
                        try:
                            locator = page.locator(f"[data-quanta-id='{t_id}']").first
                            await locator.scroll_into_view_if_needed(timeout=2000)
                            await locator.click(timeout=5000)
                        except Exception as e:
                            logger.warning(f"Failed to click ID {t_id}: {e}")
                            
                    elif act_type == "type" and t_id:
                        value = act.get("value", "")
                        await user_logger.info("EXECUTE", message=f"Typing '{value}' into ID {t_id}")
                        try:
                            locator = page.locator(f"[data-quanta-id='{t_id}']").first
                            await locator.scroll_into_view_if_needed(timeout=2000)
                            await locator.fill(value, timeout=5000)
                        except Exception as e:
                            logger.warning(f"Failed to type ID {t_id}: {e}")

                import asyncio
                await asyncio.sleep(1)
                
                await page.wait_for_load_state("domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except PlaywrightTimeout:
                    pass
            
            if not ui_ready:
                from temporalio.exceptions import ApplicationError
                raise ApplicationError(
                    f"Navigation Phase failed to reach UI ready state within {max_loops} loops.",
                    type="NavigationTimeoutError",
                    non_retryable=True
                )
        else:
            ui_ready = True # No navigation objective, proceed directly to extraction
        
        # ---------------------------------------------------------------------
        # PHASE 2: SCHEMA-DRIVEN EXTRACTION
        # ---------------------------------------------------------------------
        if extraction_schema and ui_ready:
            await user_logger.info("THINK", message="Phase 2 - Semantic Schema-Driven Extraction...")
            
            # Re-evaluate the DOM one final time to capture the finished UI state
            final_dom_state = await page.evaluate("""
                () => {
                    const pageText = document.body.innerText.replace(/\\s+/g, ' ').substring(0, 5000);
                    return { pageText };
                }
            """)
            
            prompt = (
                f"Objective: Extract the requested data from the page.\n\n"
                f"Visible Page Text:\n{final_dom_state['pageText']}\n\n"
                f"Target Schema:\n{json.dumps(extraction_schema, indent=2)}\n\n"
                "CRITICAL RULE: You must map the visible page text into the exact Target Schema provided. Do not invent data. If a field is missing on the page, return null for that field.\n\n"
                "Return ONLY a strictly valid JSON object that perfectly matches the Target Schema."
            )
            
            # Isolated context to save tokens and prevent hallucination based on previous clicks
            llm_extractor = SafeLLMClient()
            llm_response = await llm_extractor.call(system_prompt="You are a strict JSON data mapping agent.", user_prompt=prompt)
            
            try:
                clean_json = llm_response.replace('```json', '').replace('```', '').strip()
                extracted_data = json.loads(clean_json)
                return {"status": "success", "loops": loop_count, "data": extracted_data}
            except Exception as e:
                logger.error(f"[{job_id}] Phase 2 Schema Parse Error: {str(e)} | Raw: {llm_response}")
                return {"status": "failed", "error": "Schema validation failed"}
                
        return {"status": "success", "loops": loop_count}
        
    except Exception as e:
        logger.error(f"[{job_id}] Universal Agent Error: {str(e)}")
        await user_logger.error("FAILURE", message=f"Agent failed: {str(e)}")
        raise e
