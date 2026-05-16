import logging
import asyncio
import os
import json
from playwright.async_api import Page
from core.llm.safe_client import SafeLLMClient
from core.user_facing_logger import UserFriendlyLogger

logger = logging.getLogger("activity")

async def solve_w3_exercise(
    page: Page, 
    job_id: str, 
    user_logger: UserFriendlyLogger,
    nervous_system
):
    """
    Dedicated solver for W3Schools HTML Exercises.
    Designed for FYP2 Visual Presentation.
    """
    try:
        target_url = "https://www.w3schools.com/quiztest/quiztest.asp?qtest=HTML"
        await user_logger.info("NAVIGATE", message=f"Opening Universal Agent Target: {target_url}")
        await page.goto(target_url, wait_until="domcontentloaded")
        await asyncio.sleep(2) # Initial hydration
        
        objective = (
            "Complete the interactive assessment on this page. "
            "Navigate to the next question when answered. "
            "Stop when the assessment is entirely finished."
        )
        
        loop_count = 0
        max_loops = 50
        llm = SafeLLMClient()
        
        while loop_count < max_loops:
            loop_count += 1
            await user_logger.info("THINK", message=f"Analyzing universal DOM state (Iteration {loop_count})...")
            
            # DOM Tagging (Set-of-Marks)
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
                                text = text.trim().replace(/\\s+/g, ' ').substring(0, 150);
                                if (!text && el.tagName === 'INPUT') text = el.type;
                                
                                // CRITICAL NEW ADDITION: Expose the checked state to the LLM
                                if (el.tagName === 'INPUT' && (el.type === 'radio' || el.type === 'checkbox') && el.checked) {
                                    text += " [ALREADY SELECTED]";
                                }
                                
                                marksText += `[ID: ${id}] Text: ${text}\\n`;
                            }
                        }
                    });
                    
                    const pageText = document.body.innerText.replace(/\\s+/g, ' ').substring(0, 2000);
                    return { marksText, pageText };
                }
            """)
            
            prompt = (
                f"Objective: {objective}\n\n"
                f"Visible Page Text (Context):\n{dom_state['pageText']}\n\n"
                f"Interactive Elements:\n{dom_state['marksText']}\n\n"
                "CRITICAL SPEED RULE: You must batch your actions. If this is a quiz, find the correct answer AND the 'Next' button, and output BOTH clicks in the actions array in sequence.\n\n"
                "Return ONLY a strict, valid JSON object.\n"
                "{\n"
                '  "thought_process": "Brief reasoning",\n'
                '  "actions": [\n'
                '    {"type": "click", "target_id": "105"},\n'
                '    {"type": "click", "target_id": "108"}\n'
                '  ],\n'
                '  "status": "in_progress" | "complete"\n'
                "}"
            )
            
            llm_response = await llm.call(system_prompt="You are a JSON-only autonomous web agent.", user_prompt=prompt)
            
            try:
                # Strict JSON parsing
                clean_json = llm_response.replace('```json', '').replace('```', '').strip()
                action_data = json.loads(clean_json)
            except Exception as e:
                logger.error(f"[{job_id}] JSON Parse Error: {str(e)} | Raw: {llm_response}")
                await asyncio.sleep(1)
                continue
                
            await user_logger.info("PLAN", message=f"Reasoning: {action_data.get('thought_process', '')}")
            
            status = action_data.get("status", "in_progress")
            if status == "complete":
                await user_logger.info("COMPLETE", message="Agent determined the assessment is complete.")
                break
                
            actions = action_data.get("actions", [])
            for act in actions:
                act_type = act.get("type")
                t_id = act.get("target_id")
                
                if act_type == "click" and t_id:
                    await user_logger.info("EXECUTE", message=f"Clicking element ID {t_id}")
                    try:
                        locator = page.locator(f"[data-quanta-id='{t_id}']").first
                        await locator.scroll_into_view_if_needed()
                        await locator.click(timeout=5000)
                        await asyncio.sleep(0.5)  # Micro-pause between sequential actions
                    except Exception as e:
                        logger.warning(f"Failed to click ID {t_id}: {e}")
                        
                elif act_type == "type" and t_id:
                    value = act.get("value", "")
                    await user_logger.info("EXECUTE", message=f"Typing '{value}' into ID {t_id}")
                    try:
                        locator = page.locator(f"[data-quanta-id='{t_id}']").first
                        await locator.scroll_into_view_if_needed()
                        await locator.fill(value, timeout=5000)
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.warning(f"Failed to type ID {t_id}: {e}")

            # The Loop Latch: Wait 2 seconds for the Next page to fully hydrate before re-tagging the DOM
            await asyncio.sleep(2)
            
        return {"status": "success", "loops": loop_count}
        
    except Exception as e:
        logger.error(f"[{job_id}] Universal Agent Error: {str(e)}")
        await user_logger.error("FAILURE", message=f"Agent failed: {str(e)}")
        raise e
