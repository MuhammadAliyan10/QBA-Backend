import logging
import asyncio
import os
import json
import re
import hashlib
import markdownify
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
    extraction_schema: dict = None,
    materialized_files: list[str] = None
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

            # --- SESSION EXPIRY DETECTION ---
            # If vaulted cookies expired, the site redirects to login.
            # Detect this early to fail fast instead of hallucinating on a login page.
            current_url_lower = page.url.lower()
            page_title_lower = (await page.title()).lower()
            auth_indicators = ["login", "signin", "sign-in", "sign_in", "auth", "sso", "oauth", "accounts/login"]
            title_indicators = ["sign in", "log in", "login", "authenticate"]

            url_has_auth = any(indicator in current_url_lower for indicator in auth_indicators)
            title_has_auth = any(indicator in page_title_lower for indicator in title_indicators)

            if url_has_auth or title_has_auth:
                from temporalio.exceptions import ApplicationError
                raise ApplicationError(
                    f"Session expired: redirected to auth page ({page.url}). Re-vault your session with 'quanta auth'.",
                    type="SessionExpired",
                    non_retryable=True
                )
        
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
                
                file_ctx = ""
                if materialized_files:
                    file_ctx = f"Available Files to Upload:\n" + "\n".join([f"- {f}" for f in materialized_files]) + "\n\n"

                prompt = (
                    f"Objective: {navigation_objective}\n"
                    f"URL: {page.url}\n\n"
                    f"Context:\n{dom_state['pageText']}\n\n"
                    f"Elements:\n{dom_state['marksText'][:10000]}\n\n"
                    f"{file_ctx}"
                    "RULES:\n"
                    "1. ONLY use the strictly numeric [ID]s listed in the Elements section. NEVER hallucinate string IDs like 'search' or 'tsf'.\n"
                    "2. If objective complete/success state reached, return status: \"ui_ready\", actions: [].\n"
                    "3. If already on target URL, do NOT click nav links again. Act on the page.\n"
                    "4. Typing: {\"type\": \"type\", \"target_id\": \"12\", \"value\": \"text\"}. Clicking: {\"type\": \"click\", \"target_id\": \"5\"}.\n"
                    "5. File Upload: {\"type\": \"upload\", \"target_id\": \"9\", \"value\": \"/path/to/file\"}\n"
                    "Output ONLY raw JSON using DOUBLE QUOTES:\n"
                    "{\n"
                    '  "thought_process": "brief",\n'
                    '  "actions": [{"type": "click", "target_id": "105"}],\n'
                    '  "status": "in_progress"\n'
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
                            
                    elif act_type == "upload" and t_id:
                        value = act.get("value", "")
                        await user_logger.info("EXECUTE", message=f"Uploading '{value}' to ID {t_id}")
                        try:
                            locator = page.locator(f"[data-quanta-id='{t_id}']").first
                            await locator.scroll_into_view_if_needed(timeout=2000)
                            await locator.set_input_files(value, timeout=5000)
                        except Exception as e:
                            logger.warning(f"Failed to upload to ID {t_id}: {e}")

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
            
            # SPA Hydration Wait — React/Vue/Angular apps render asynchronously.
            # Without this, innerHTML captures the empty shell before product grids load.
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
                await asyncio.sleep(3)  # Hard buffer for SPA DOM hydration
            except PlaywrightTimeout:
                logger.warning(f"[{job_id}] SPA hydration wait timed out. Proceeding with current DOM.")
            
            # --- PAGINATION LOOP ---
            # Enterprise sites paginate data. We extract, then attempt to advance
            # to the next page, re-extract, and merge until no new data appears.
            MAX_PAGES = 10
            all_extracted_rows = []
            seen_hashes = set()
            llm_extractor = SafeLLMClient()

            for page_num in range(1, MAX_PAGES + 1):
                if page_num > 1:
                    await user_logger.progress(f"Extracting page {page_num}...")

                # CLIENT-SIDE DOM PRUNING ENGINE
                await page.evaluate("""
                    () => {
                        document.querySelectorAll(
                            'script, style, svg, nav, footer, header, aside, path, ' +
                            'noscript, iframe, link[rel=stylesheet], meta, ' +
                            '[role=banner], [role=navigation], [role=contentinfo], ' +
                            '[aria-hidden=true], .cookie-banner, .modal-backdrop'
                        ).forEach(el => el.remove());
                    }
                """)
                
                final_dom_state = await page.evaluate("""
                    () => {
                        return { html: document.body.innerHTML };
                    }
                """)
                
                markdown_content = markdownify.markdownify(final_dom_state['html'], heading_style="ATX")

                # DOM CHUNKING: Truncate to 12K chars to prevent LLM context overflow
                MAX_MARKDOWN_CHARS = 12000
                if len(markdown_content) > MAX_MARKDOWN_CHARS:
                    logger.warning(f"[{job_id}] Markdown truncated from {len(markdown_content)} to {MAX_MARKDOWN_CHARS} chars")
                    markdown_content = markdown_content[:MAX_MARKDOWN_CHARS]

                logger.info(f"[{job_id}] Markdown content length (post-prune): {len(markdown_content)} chars")
                
                prompt = (
                    f"Objective: Extract the requested data from the page.\n\n"
                    f"Visible Page Markdown Structure:\n{markdown_content}\n\n"
                    f"Target Schema:\n{json.dumps(extraction_schema, indent=2)}\n\n"
                    "CRITICAL RULE: You must map the visible page text into the exact Target Schema provided. Do not invent data. If a field is missing on the page, return null for that field.\n\n"
                    "Return ONLY a strictly valid JSON object that perfectly matches the Target Schema."
                )
                
                llm_response = await llm_extractor.call(system_prompt="You are a strict JSON data mapping agent.", user_prompt=prompt)
                
                try:
                    json_match = re.search(r'\{[\s\S]*\}|\[[\s\S]*\]', llm_response)
                    if not json_match:
                        raise ValueError("No valid JSON found in LLM response")
                    clean_json = json_match.group()
                    page_data = json.loads(clean_json)
                except Exception as e:
                    logger.error(f"[{job_id}] Phase 2 Schema Parse Error (page {page_num}): {str(e)} | Raw: {llm_response}")
                    if page_num == 1:
                        return {"status": "failed", "error": "Schema validation failed"}
                    break  # Stop pagination on parse failure, return what we have

                # Flatten and deduplicate by content hash
                rows = []
                if isinstance(page_data, list):
                    rows = page_data
                elif isinstance(page_data, dict):
                    for key, value in page_data.items():
                        if isinstance(value, list):
                            rows = value
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

                # If no new rows on this page, pagination is exhausted
                if new_rows == 0:
                    logger.info(f"[{job_id}] Pagination exhausted at page {page_num} (no new data)")
                    break

                # Only attempt pagination if we're not on the last allowed page
                if page_num < MAX_PAGES:
                    # Attempt to find and click a "Next" button
                    next_clicked = False
                    try:
                        next_button = await page.evaluate("""
                            () => {
                                const selectors = [
                                    'a[aria-label*="next" i]', 'button[aria-label*="next" i]',
                                    'a:has-text("Next")', 'button:has-text("Next")',
                                    'a:has-text("»")', 'a:has-text("›")',
                                    '[class*="next"]', '[class*="pagination"] a:last-child'
                                ];
                                for (const sel of selectors) {
                                    try {
                                        const el = document.querySelector(sel);
                                        if (el && el.offsetParent !== null) {
                                            return sel;
                                        }
                                    } catch(e) {}
                                }
                                return null;
                            }
                        """)

                        if next_button:
                            await page.click(next_button, timeout=5000)
                            try:
                                await page.wait_for_load_state("networkidle", timeout=10000)
                            except PlaywrightTimeout:
                                pass
                            await asyncio.sleep(2)  # SPA hydration buffer
                            next_clicked = True
                    except Exception as nav_err:
                        logger.info(f"[{job_id}] Pagination navigation failed: {nav_err}")

                    if not next_clicked:
                        # Try infinite scroll fallback
                        prev_height = await page.evaluate("document.body.scrollHeight")
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(3)
                        new_height = await page.evaluate("document.body.scrollHeight")
                        if new_height == prev_height:
                            logger.info(f"[{job_id}] No more content to scroll at page {page_num}")
                            break

            # Return aggregated deduplicated data
            final_data = all_extracted_rows if len(all_extracted_rows) > 1 else (all_extracted_rows[0] if all_extracted_rows else {})
            return {"status": "success", "loops": loop_count, "data": final_data, "pages_scraped": min(page_num, MAX_PAGES), "total_rows": len(all_extracted_rows)}
                
        return {"status": "success", "loops": loop_count}
        
    except Exception as e:
        logger.error(f"[{job_id}] Universal Agent Error: {str(e)}")
        await user_logger.error("FAILURE", message=f"Agent failed: {str(e)}")
        raise e
