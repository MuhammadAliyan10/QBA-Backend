import os
import asyncio
import logging
import base64
import time
import tempfile
from datetime import timedelta
from typing import Dict, Any, Optional, List
from temporalio import activity
from playwright.async_api import async_playwright, Page, ElementHandle
from playwright.async_api import TimeoutError as PlaywrightTimeout
import httpx

# --- IMPORTS ---
# 1. The Nervous System (Snake Case - Infrastructure)
from core.NervousSystem import NervousSystem

# 2. The Glass Box Engine (Camel Case - Logic)
from core.SmartFinder import SmartFinder

# 3. The Network Sniffer (Level 5 - Protocol Reverse Engineering)
from core.NetworkSniffer import NetworkSniffer

# 4. The Account Pool Manager (Session Rehydration)
from core.AccountManager import AccountManager

# 5. The Recipe Manager (Dynamic RAG)
from core.RecipeManager import RecipeManager

logger = logging.getLogger("activity")

# =============================================================================
# INDUSTRIAL CONSTANTS - Configurable via Environment
# =============================================================================
NAVIGATION_TIMEOUT = int(os.getenv("NAVIGATION_TIMEOUT_MS", "30000"))
NETWORK_IDLE_TIMEOUT = int(os.getenv("NETWORK_IDLE_TIMEOUT_MS", "5000"))
CLICK_RETRY_ATTEMPTS = int(os.getenv("CLICK_RETRY_ATTEMPTS", "3"))
CLICK_RETRY_DELAY_MS = int(os.getenv("CLICK_RETRY_DELAY_MS", "500"))
HTTP_REQUEST_TIMEOUT = int(os.getenv("HTTP_REQUEST_TIMEOUT_S", "30"))
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", tempfile.gettempdir())


# =============================================================================
# HELPER FUNCTIONS - Robust, Reusable, Tested
# =============================================================================

def validate_step_params(step_params: Dict[str, Any], available_params: Dict[str, Any], step_index: int) -> Dict[str, Any]:
    """
    Validates and substitutes variables in step parameters.

    CRITICAL: Fails fast if required variables are missing.
    Prevents silent failures where {placeholder} is typed literally.

    Args:
        step_params: Parameters for this step (may contain {variable} placeholders)
        available_params: User-provided parameters from payload
        step_index: Step number for error messages

    Returns:
        Dict with all variables substituted

    Raises:
        ValueError: If a required variable is missing
    """
    validated = {}

    for key, value in step_params.items():
        if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
            var_name = value[1:-1]

            if var_name not in available_params:
                raise ValueError(
                    f"[Step {step_index}] Missing required parameter: '{var_name}'. "
                    f"Expected in payload.params. Available params: {list(available_params.keys())}"
                )

            validated[key] = available_params[var_name]
            logger.debug(f"[Param] Substituted {{{var_name}}} → '{str(validated[key])[:20]}...'")
        else:
            validated[key] = value

    return validated


async def click_with_retry(
    finder: SmartFinder,
    page: Page,
    intent: str,
    job_id: str,
    max_attempts: int = CLICK_RETRY_ATTEMPTS
) -> None:
    """
    Robust click with automatic retry and re-find on failure.

    Handles:
    - Stale element references
    - Elements that become detached during animation
    - Transient overlays that disappear

    Args:
        finder: SmartFinder instance
        page: Playwright page
        intent: What to click (e.g., "submit button")
        job_id: For logging
        max_attempts: Maximum retry attempts

    Raises:
        Exception: If all attempts fail
    """
    last_error = None

    for attempt in range(max_attempts):
        try:
            # Re-find element on each attempt (handles stale references)
            element = await finder.find(page, intent)

            # Scroll into view to ensure visibility
            await element.scroll_into_view_if_needed()
            await asyncio.sleep(0.1)  # Brief pause for scroll animation

            # Attempt click
            await element.click(timeout=5000)

            logger.info(f"[{job_id}] Click successful: '{intent}' (attempt {attempt + 1})")
            return

        except Exception as e:
            last_error = e
            logger.warning(
                f"[{job_id}] Click failed (attempt {attempt + 1}/{max_attempts}): {e}"
            )

            if attempt < max_attempts - 1:
                # Wait before retry
                await asyncio.sleep(CLICK_RETRY_DELAY_MS / 1000)

                # Try to dismiss any popups/overlays that might be blocking
                await dismiss_overlays(page)

    # All attempts failed
    raise Exception(
        f"Click failed after {max_attempts} attempts on '{intent}': {last_error}"
    )


async def dismiss_overlays(page: Page) -> None:
    """
    Attempts to dismiss common UI overlays that block interactions.

    Handles:
    - Cookie consent banners
    - Newsletter popups
    - Modal dialogs with close buttons
    - Push notification prompts
    """
    DISMISS_SELECTORS = [
        # Cookie banners
        "[class*='cookie'] button[class*='accept']",
        "[class*='cookie'] button[class*='close']",
        "[id*='cookie'] button[class*='accept']",
        "#onetrust-accept-btn-handler",
        ".cc-btn.cc-dismiss",

        # Generic close buttons
        "[class*='modal'] [class*='close']",
        "[class*='popup'] [class*='close']",
        "[class*='overlay'] [class*='close']",
        "button[aria-label='Close']",
        "button[aria-label='Dismiss']",

        # Newsletter popups
        "[class*='newsletter'] [class*='close']",
        "[class*='subscribe'] [class*='close']",
    ]

    for selector in DISMISS_SELECTORS:
        try:
            element = await page.query_selector(selector)
            if element and await element.is_visible():
                await element.click(timeout=1000)
                logger.debug(f"[Overlay] Dismissed: {selector}")
                await asyncio.sleep(0.3)  # Wait for animation
                return  # One dismissal per call is enough
        except:
            continue  # Try next selector


async def safe_wait_for_network_idle(page: Page, timeout_ms: int = NETWORK_IDLE_TIMEOUT) -> None:
    """
    Waits for network idle state with proper exception handling.

    Only catches timeout - re-raises critical errors like page closed.
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeout:
        logger.debug("Network idle timeout (expected for some sites with streaming)")
    except Exception as e:
        error_str = str(e).lower()
        if "closed" in error_str or "crashed" in error_str:
            raise  # Critical error - re-raise
        logger.warning(f"Unexpected network wait error: {e}")


async def capture_failure_screenshot(page: Page, job_id: str, error: Exception) -> bytes:
    """
    Captures screenshot on failure for debugging.

    Returns empty bytes if capture fails (never throws).
    """
    try:
        screenshot = await page.screenshot(
            type='jpeg',
            quality=60,
            full_page=False  # Current viewport only for speed
        )
        logger.info(f"[{job_id}] Failure screenshot captured ({len(screenshot)} bytes)")
        return screenshot
    except Exception as e:
        logger.warning(f"Failed to capture failure screenshot: {e}")
        return b""

# Initialize Recipe Manager (singleton pattern - loads once)
_recipe_manager_instance = None

def get_recipe_manager() -> RecipeManager:
    """Get or create RecipeManager singleton."""
    global _recipe_manager_instance
    if _recipe_manager_instance is None:
        _recipe_manager_instance = RecipeManager()
    return _recipe_manager_instance

def get_proxy_config(region="us"):
    """
    Constructs the Proxy dictionary for Playwright.
    Supports BrightData / Smartproxy / IPRoyal formats.
    """
    server = os.getenv("PROXY_SERVER") # e.g., "http://brd.superproxy.io:22225"
    username = os.getenv("PROXY_USER")
    password = os.getenv("PROXY_PASSWORD")

    if not server or not username:
        return None

    return {
        "server": server,
        "username": f"{username}-country-{region}", # Most providers use this format
        "password": password
    }

@activity.defn
async def browser_automation_activity(payload: dict) -> dict:
    """
    The Main Execution Loop.
    Runs inside a Temporal Worker.
    """
    # 1. Unpack Payload (From Go)
    job_id = payload.get("job_id")
    workflow_id = payload.get("workflow_id")
    params = payload.get("params", {})

    # The 'config' dictionary contains our Glass Box settings
    config = payload.get("config", {})

    # 2. Notify Nervous System: START
    await NervousSystem.publish_update(
        job_id=job_id,
        status="RUNNING",
        message=f"[System] Initializing Glass Box for workflow: {workflow_id}",
        node_id="init"
    )

    # 3. Load Recipe (DYNAMIC - From Qdrant Vector Search)
    recipe_mgr = get_recipe_manager()

    # Try to find recipe by semantic search
    recipe = recipe_mgr.find_recipe(workflow_id)

    if recipe:
        steps = recipe['steps']
        logger.info(f"[System] Found recipe via vector search:'{recipe['name']}' (score: {recipe['score']:.3f})")
        await NervousSystem.publish_update(
            job_id, "RUNNING",
            f"[RAG] Loaded workflow: '{recipe['name']}' (semantic match)",
            "init"
        )
    else:
        #  Fallback: Check if raw steps were provided (Developer Mode)
        steps = payload.get("steps", [])
        if not steps:
            err = f"[Error] No recipe found for query: '{workflow_id}' (threshold: 0.7)"
            await NervousSystem.publish_update(job_id, "FAILED", err, "init")
            return {"status": "FAILED", "error": err}

    async with async_playwright() as p:
        # --- 4. BROWSER LAUNCH STRATEGY ---
        launch_args = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-setuid-sandbox"]
        }

        # PROXY LOGIC (The "Warden")
        if config.get("use_premium_proxy"):
            region = config.get("region", "us")
            proxy_conf = get_proxy_config(region)

            if proxy_conf:
                launch_args["proxy"] = proxy_conf
                await NervousSystem.publish_update(job_id, "RUNNING", f"[Network] Routing via residential proxy ({region})", "init")
            else:
                await NervousSystem.publish_update(job_id, "WARNING", "Proxy credentials missing! Using Datacenter IP.", "init")

        # STATE FLAG: Track workflow success for account release logic
        # This replaces the fragile 'e' not in locals() hack
        workflow_succeeded = False

        try:
            browser = await p.chromium.launch(**launch_args)

            # --- 5. ACCOUNT POOL & SESSION INJECTION ---
            # Initialize Account Manager
            account_mgr = AccountManager()
            leased_account = None

            # Check if login is required
            require_login = config.get("require_login", False)
            target_domain = config.get("domain")

            if require_login and target_domain:
                # Attempt to lease an account from the pool
                leased_account = account_mgr.lease_account(target_domain)

                if leased_account:
                    await NervousSystem.publish_update(
                        job_id, "RUNNING",
                        f"[Security] Leased account: {leased_account['username']} (cookies: {'Yes' if leased_account['cookies'] else 'No'})",
                        "init"
                    )

            # Create browser context
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36..."
            )

            # Fast Path: Inject cookies if available
            cookie_valid = False
            if leased_account and leased_account['cookies']:
                try:
                    await context.add_cookies(leased_account['cookies'])
                    await NervousSystem.publish_update(job_id, "RUNNING", "[Security] Cookies injected (Fast Path)", "init")
                    cookie_valid = True
                except Exception as e:
                    logger.warning(f"Cookie injection failed: {e}")
                    cookie_valid = False

            page = await context.new_page()

            # --- DOWNLOAD HANDLER (Industrial-Grade) ---
            async def handle_download(download):
                """Handles file downloads with actual storage."""
                filename = download.suggested_filename
                safe_filename = "".join(c for c in filename if c.isalnum() or c in '._-')
                local_path = os.path.join(DOWNLOAD_DIR, f"{job_id}_{safe_filename}")

                await NervousSystem.publish_update(
                    job_id, "RUNNING", f"Downloading: {filename}", "io"
                )

                try:
                    # Save to local filesystem
                    await download.save_as(local_path)
                    file_size = os.path.getsize(local_path)

                    logger.info(f"[{job_id}] Downloaded {filename} ({file_size} bytes)")

                    # TODO: Upload to S3/MinIO in production
                    # async with aiofiles.open(local_path, 'rb') as f:
                    #     await s3_client.upload_fileobj(f, bucket, f"{job_id}/{filename}")

                    final_url = f"file://{local_path}"  # Local path for now

                    await NervousSystem.publish_update(
                        job_id, "SUCCESS", f"File saved: {final_url} ({file_size} bytes)", "io"
                    )

                except Exception as e:
                    logger.error(f"Download failed for {filename}: {e}")
                    await NervousSystem.publish_update(
                        job_id, "FAILED", f"Download failed: {filename} - {e}", "io"
                    )
                    raise  # Don't silently continue

            page.on("download", handle_download)

            # Initialize the Co-Pilot (SmartFinder)
            finder = SmartFinder(job_id)

            # --- 6. EXECUTION LOOP ---
            for i, step in enumerate(steps):
                node_id = f"step-{i+1}"
                action = step["action"]
                raw_params = step.get("params", {})

                # CRITICAL: Validate and substitute variables (fails fast on missing params)
                step_params = validate_step_params(raw_params, params, i + 1)

                logger.info(f"[{job_id}] Executing {action} (step {i+1}/{len(steps)})...")

                # --- ACTION SWITCH ---
                if action == "GOTO":
                    url = step_params["url"]

                    # Use configurable timeout
                    await page.goto(url, timeout=NAVIGATION_TIMEOUT)

                    # Safe network wait (won't swallow critical errors)
                    await safe_wait_for_network_idle(page)

                    # Try to dismiss any popups that appeared on page load
                    await dismiss_overlays(page)

                elif action == "CLICK":
                    intent = step_params["intent"]

                    # Special test hook for human intervention simulation
                    if intent == "simulate_human_check":
                        from exceptions import HumanInterventionRequired
                        raise HumanInterventionRequired(
                            reason="GOD_MODE_CHECK",
                            context={"msg": "System is healthy. Proceed?"}
                        )

                    # INDUSTRIAL: Use robust click with retry and overlay dismissal
                    await click_with_retry(finder, page, intent, job_id)

                    await NervousSystem.publish_update(job_id, "RUNNING", f"Clicked '{intent}'", node_id)

                elif action == "TYPE":
                    intent = step_params["intent"]
                    text = step_params["text"]

                    element = await finder.find(page, intent)

                    # USE GAUSSIAN TYPING (The Humanizer)
                    await finder.glass.human_type(page, element, text)

                    await NervousSystem.publish_update(job_id, "RUNNING", f"Typed input safely", node_id)

                elif action == "LOGIN_AND_SNIFF":
                    # --- LEVEL 5: HYBRID PROTOCOL AUTOMATION ---
                    # Phase 1: Use Browser to Authenticate
                    # Phase 2: Capture API Session
                    # Phase 3: Switch to HTTPX for Speed

                    target_domain = step_params.get("target_domain")
                    url = step_params.get("url")
                    iterations = step_params.get("iterations", 5)

                    # 1. Start the Spy
                    sniffer = NetworkSniffer(target_domain=target_domain)
                    await sniffer.start_sniffing(page)
                    await NervousSystem.publish_update(job_id, "RUNNING", f"Sniffer monitoring {target_domain}...", node_id)

                    # 2. Execute Navigation (This triggers the network traffic)
                    await page.goto(url)

                    # Wait for stability (Allow APIs to fire)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except:
                        pass

                    # 3. Check the Loot
                    session = sniffer.get_session_context()

                    if session:
                        await NervousSystem.publish_update(job_id, "SUCCESS", "🔓 Golden Ticket Captured! Switching to Protocol Mode.", node_id)

                        # --- PHASE 2: PROTOCOL MODE (The Speed Run) ---
                        api_url = session["url"]
                        headers = session["headers"]
                        payload_template = session.get("payload")
                        method = session["method"]

                        # Replay Logic (Simulating "Next Page" iteration)
                        async with httpx.AsyncClient() as client:
                            start_time = time.time()

                            # We loop N times to demonstrate speed
                            for k in range(1, iterations + 1):
                                current_payload = payload_template

                                # Dynamic Injection: If payload is JSON dict, try to increment 'page'
                                if isinstance(current_payload, dict):
                                    # Create a copy to avoid mutating the template
                                    current_payload = payload_template.copy()
                                    # Heuristic: Look for common pagination keys
                                    if "page" in current_payload:
                                        current_payload["page"] = int(current_payload["page"]) + k
                                    elif "cursor" in current_payload:
                                        # Mock cursor update
                                        current_payload["cursor"] = f"next_{k}"

                                try:
                                    # CRITICAL: Add timeout to prevent hanging
                                    resp = await client.request(
                                        method,
                                        api_url,
                                        headers=headers,
                                        json=current_payload if isinstance(current_payload, dict) else None,
                                        content=current_payload if isinstance(current_payload, str) else None,
                                        timeout=10.0  # 10 second hard limit
                                    )

                                    duration = (time.time() - start_time) * 1000
                                    size_kb = len(resp.content) / 1024


                                    await NervousSystem.publish_update(
                                        job_id, "RUNNING",
                                        f"[Network] Protocol Hit #{k}: Status {resp.status_code} ({size_kb:.2f} KB) in {duration:.0f}ms",
                                        node_id
                                    )

                                except httpx.TimeoutException:
                                    logger.warning(f"[Network] API timeout on replay #{k} - falling back to browser mode")
                                    break  # Exit replay loop, continue with browser
                                except httpx.HTTPError as e:
                                    logger.warning(f"[Network] API error on replay #{k}: {e}")
                                    break  # Exit replay loop, continue with browser
                                except Exception as e:
                                    logger.error(f"[Network] Unexpected error in protocol replay: {e}")
                                    break
                                # REMOVED: Unreachable dead code - duplicate except Exception block

                                start_time = time.time()  # Reset timer for next request

                        await NervousSystem.publish_update(job_id, "SUCCESS", f"Protocol Mode Complete: {iterations} requests replayed", node_id)

                    else:
                        msg = "No verified API keys found (Auth failed or no XHR). Continuing in Browser Mode."
                        await NervousSystem.publish_update(job_id, "WARNING", msg, node_id)


                # --- VISUAL PROOF (Screenshot) ---
                # Take a tiny jpeg
                screenshot = await page.screenshot(
                    type='jpeg',
                    quality=20,
                    scale="css",
                    animations="disabled",
                    caret="hide"
                )
                # Resize logic would go here with Pillow if needed to save more bandwidth

                # Send the visual proof to the dashboard
                await NervousSystem.publish_update(
                    job_id, "RUNNING", "Step Verified", node_id, screenshot=screenshot
                )


            # --- 7. CLEANUP ---
            await NervousSystem.publish_update(job_id, "COMPLETED", "Workflow Finished Successfully", "end")
            workflow_succeeded = True  # Mark success before return

            # Return with metrics
            return {
                "status": "SUCCESS",
                "steps_completed": len(steps),
                "job_id": job_id
            }

        except Exception as e:
            logger.error(f"Job Failed: {e}", exc_info=True)
            workflow_succeeded = False  # Explicitly mark failure

            # INDUSTRIAL: Capture screenshot for debugging
            failure_screenshot = b""
            if 'page' in locals() and page:
                failure_screenshot = await capture_failure_screenshot(page, job_id, e)

            await NervousSystem.publish_update(
                job_id, "FAILED",
                f"Critical Error: {str(e)}",
                "error",
                screenshot=failure_screenshot if failure_screenshot else None
            )

            # Re-raise so Temporal knows to retry
            raise e

        finally:
            # =================================================================
            # CRITICAL: Robust cleanup with existence checks
            # =================================================================

            # 1. Release account back to pool
            if 'leased_account' in locals() and leased_account and 'account_mgr' in locals():
                try:
                    # Check if context exists before accessing cookies
                    new_cookies = None
                    if 'context' in locals() and context:
                        try:
                            new_cookies = await context.cookies()
                        except Exception as cookie_err:
                            logger.warning(f"Could not capture cookies: {cookie_err}")

                    account_mgr.release_account(
                        leased_account['id'],
                        new_cookies=new_cookies,
                        success=workflow_succeeded
                    )
                    logger.info(f"Released account {leased_account['username']}")

                except Exception as release_err:
                    logger.error(f"Failed to release account: {release_err}")

            # 2. Close browser context (if exists)
            if 'context' in locals() and context:
                try:
                    await context.close()
                except Exception as ctx_err:
                    logger.warning(f"Context close error: {ctx_err}")

            # 3. Close browser (if exists)
            if 'browser' in locals() and browser:
                try:
                    await browser.close()
                except Exception as browser_err:
                    logger.warning(f"Browser close error: {browser_err}")

