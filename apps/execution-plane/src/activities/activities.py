import os
import asyncio
import logging
import base64
import time
import json
import tempfile
from datetime import timedelta
from typing import Dict, Any, Optional, List
from temporalio import activity
from temporalio.exceptions import ApplicationError
from contextlib import asynccontextmanager
from playwright.async_api import async_playwright, Page, ElementHandle
from playwright.async_api import TimeoutError as PlaywrightTimeout
import httpx
import traceback

# Feature Flags
from config import is_s3_upload_enabled, is_session_persistence_enabled

# Session Persistence (Encrypted Browser State)
from core.browser.session import SessionManager, get_session_manager

# TASK 2 FIX: Import Universal Storage for actual S3/MinIO uploads
from core.storage import get_storage, is_storage_available, StorageUploadError

# --- IMPORTS ---
# 1. The Nervous System (Snake Case - Infrastructure)
from core.nervous_system import NervousSystem
from core.utils.params import substitute_variables, validate_and_substitute

# 2. The Glass Box Engine (Camel Case - Logic)
from core.selector.smart_finder import SmartFinder

# 3. The Network Sniffer (Level 5 - Protocol Reverse Engineering)
from core.network_sniffer import NetworkSniffer

# 4. The Account Pool Manager (Session Rehydration)
from core.account_manager import AccountManager, SessionHydrationTimeout

# 5. The Recipe Manager (Dynamic RAG)
from core.recipe.recipe_manager import RecipeManager

# 6. User-Facing Logger (The Voice of the Glass Box)
from core.user_facing_logger import UserFriendlyLogger

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
    Supports both {{variable}} and {variable} syntax within strings.
    """
    return validate_and_substitute(step_params, available_params)

@asynccontextmanager
async def safe_browser_context(playwright_instance, launch_args, storage_state=None):
    """
    Guarantees browser and context closure even on catastrophic crashes.
    Prevents zombie Chromium processes and memory leaks.
    """
    browser = await playwright_instance.chromium.launch(**launch_args)
    context = await browser.new_context(storage_state=storage_state)
    page = await context.new_page()
    try:
        # We yield browser, context, and page to allow explicit closure in exception handlers
        yield browser, context, page
    finally:
        await context.close()
        await browser.close()


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
            result = await finder.find(intent, timeout=10000)
            if not result.found:
                raise Exception(f"Element not found: {intent}")

            element = result.element

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

def get_recipe_manager():
    """Get or create RecipeManager singleton. Returns None if model download fails."""
    global _recipe_manager_instance
    if _recipe_manager_instance is None:
        try:
            _recipe_manager_instance = RecipeManager()
        except Exception as e:
            logger.warning(f"[RecipeManager] Init failed (model download?): {e}. Skipping RAG recipe lookup.")
            return None
    return _recipe_manager_instance

def get_proxy_config(region="us"):
    """
    Constructs the Proxy dictionary for Playwright.
    Supports BrightData / Smartproxy / IPRoyal formats.

    TASK 5 FIX: Returns None if PROXY_SERVER is not configured.
    This allows running without a proxy provider to save costs.

    Returns:
        dict: Playwright proxy config, or None if proxy is not configured
    """
    server = os.getenv("PROXY_SERVER")  # e.g., "http://brd.superproxy.io:22225"

    # TASK 5 FIX: If no proxy server configured, return None (don't crash)
    if not server:
        logger.info("[Proxy] PROXY_SERVER not set - running without proxy")
        return None

    username = os.getenv("PROXY_USER", "")
    password = os.getenv("PROXY_PASSWORD", "")

    # If server is set but credentials are missing, log warning but still try
    # Some proxies (like SOCKS5) don't require auth
    if not username:
        logger.warning("[Proxy] PROXY_USER not set - attempting connection without auth")
        return {
            "server": server
        }

    # Full proxy config with region support
    return {
        "server": server,
        "username": f"{username}-country-{region}",  # Most providers use this format
        "password": password
    }


def is_proxy_available() -> bool:
    """
    Check if proxy is configured.

    Returns:
        bool: True if PROXY_SERVER environment variable is set
    """
    return bool(os.getenv("PROXY_SERVER"))


@activity.defn
async def browser_automation_activity(payload: dict) -> dict:
    """
    The Main Execution Loop.
    Runs inside a Temporal Worker.
    """
    # 1. Unpack Payload (From Go)
    job_id = payload.get("job_id")
    workflow_id = payload.get("workflow_id")
    target_url = payload.get("target_url")
    objective = payload.get("objective")
    params = payload.get("params", {})

    # DIAGNOSTIC TELEMETRY
    payload_keys = list(payload.keys())
    await NervousSystem.publish(
        f"quanta.telemetry.{job_id}",
        json.dumps({"type": "log", "message": f"[Executor] Raw Payload Keys: {payload_keys} | WorkflowID: {workflow_id}"})
    )

    # The 'config' dictionary contains our Glass Box settings
    config = payload.get("config", {})

    # 2. Initialize User Logger
    user_logger = UserFriendlyLogger(job_id)

    # 3. Notify Nervous System: START
    await user_logger.info("PROCESSING_RAG")  # "Thinking about the next step..."

    # 3. Load Recipe — Priority order:
    #    a) Editor recipe graph (nodes/edges from frontend via Go controller)
    #    b) RAG/Qdrant vector search (semantic match)
    #    c) Raw steps (developer mode / direct API)
    steps = None
    recipe_data = payload.get("recipe")

    if recipe_data and isinstance(recipe_data, dict) and "nodes" in recipe_data:
        # SOURCE A: Recipe graph from the frontend editor
        from core.recipe.recipe_converter import convert_graph_to_steps
        nodes = recipe_data.get("nodes", [])
        edges = recipe_data.get("edges", [])
        steps = convert_graph_to_steps(nodes, edges)
        logger.info(f"[System] Converted editor graph: {len(nodes)} nodes → {len(steps)} steps")
        await NervousSystem.publish_update(
            job_id, "RUNNING",
            f"Loaded workflow from editor ({len(steps)} steps)",
            "init"
        )

    if not steps:
        # SOURCE B: RAG/Qdrant vector search
        recipe_mgr = get_recipe_manager()
        recipe = recipe_mgr.find_recipe(workflow_id) if recipe_mgr else None

        if recipe:
            steps = recipe['steps']
            logger.info(f"[System] Found recipe via vector search: '{recipe['name']}' (score: {recipe['score']:.3f})")
            await NervousSystem.publish_update(
                job_id, "RUNNING",
                f"[RAG] Loaded workflow: '{recipe['name']}' (semantic match)",
                "init"
            )

    if not steps:
        # SOURCE C: Raw steps (developer mode)
        steps = payload.get("steps", [])

    if not steps:
        # SOURCE D: AI Autonomous Planning (Ad-Hoc)
        logger.info(f"[{job_id}] No recipe found. Invoking Preflight Planner...")
        await NervousSystem.publish_update(job_id, "RUNNING", "Generating autonomous plan...", "init")

        from core.rag.preflight import PreflightPipeline
        pipeline = PreflightPipeline()
        preflight_result = await pipeline.run(target_url, objective, skip_justification=True, job_id=job_id)

        if preflight_result.success and preflight_result.recipe:
            recipe_data = preflight_result.recipe
            from core.recipe.recipe_converter import convert_graph_to_steps
            nodes = recipe_data.get("nodes", [])
            edges = recipe_data.get("edges", [])
            steps = convert_graph_to_steps(nodes, edges)
            logger.info(f"[{job_id}] Autonomously generated {len(steps)} steps")
            await NervousSystem.publish_update(job_id, "RUNNING", f"Plan generated ({len(steps)} steps)", "init")
        else:
            err = f"[Error] Autonomous planning failed: {preflight_result.errors if hasattr(preflight_result, 'errors') else 'Unknown error'}"
            await NervousSystem.publish_update(job_id, "FAILED", err, "init")
            return {"status": "FAILED", "error": err}

    if not steps:
        err = f"[Error] No recipe found or generated for workflow: '{workflow_id}'"
        await NervousSystem.publish_update(job_id, "FAILED", err, "init")
        return {"status": "FAILED", "error": err}

    async with async_playwright() as p:
        # --- 4. BROWSER LAUNCH STRATEGY ---
        launch_args = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-setuid-sandbox"]
        }

        # TASK 5 FIX: Optional Proxy Logic (The "Warden")
        # Only attempt to configure proxy if:
        # 1. User requested premium proxy in config
        # 2. AND proxy server is actually configured in environment
        if config.get("use_premium_proxy"):
            if is_proxy_available():
                region = config.get("region", "us")
                proxy_conf = get_proxy_config(region)

                if proxy_conf:
                    launch_args["proxy"] = proxy_conf
                    await NervousSystem.publish_update(
                        job_id, "RUNNING",
                        f"[Network] Routing via residential proxy ({region})",
                        "init"
                    )
                else:
                    # This shouldn't happen if is_proxy_available() is True
                    await NervousSystem.publish_update(
                        job_id, "WARNING",
                        "Proxy configuration error. Using direct connection.",
                        "init"
                    )
            else:
                # TASK 5 FIX: Gracefully skip proxy when not configured
                await NervousSystem.publish_update(
                    job_id, "INFO",
                    "[Network] Proxy not configured. Using direct connection (cost-saving mode).",
                    "init"
                )
                logger.info(f"[{job_id}] Premium proxy requested but PROXY_SERVER not set. Continuing without proxy.")

        # STATE FLAG: Track workflow success for account release logic
        # This replaces the fragile 'e' not in locals() hack
        workflow_succeeded = False

        # --- 5. SESSION & ACCOUNT PREPARATION ---
        user_id = payload.get("user_id", job_id)
        target_domain = config.get("domain") or (steps[0]["params"].get("url") if steps and steps[0]["action"] == "GOTO" else None)
        if target_domain and not target_domain.startswith("http"):
             # Handle cases where domain is just a string
             pass
        elif target_domain:
             target_domain = SessionManager.extract_domain(target_domain)

        # BYOS (Bring Your Own Session) Support: 
        # Prioritize sessionState explicitly passed from the Control Plane (Vaulted sessions)
        session_data = payload.get("sessionState")
        
        if session_data:
             await NervousSystem.publish_update(
                 job_id, "RUNNING",
                 f"[Session] Using vaulted session (BYOS)",
                 "init"
             )
        elif is_session_persistence_enabled() and target_domain:
            try:
                session_manager = await get_session_manager()
                if session_manager:
                    session_data = await session_manager.get_session(user_id, target_domain)
                    if session_data:
                        await NervousSystem.publish_update(
                            job_id, "RUNNING",
                            f"[Session] Restored encrypted session for {target_domain}",
                            "init"
                        )
            except Exception as e:
                logger.warning(f"[Session] Failed to restore session: {e}")

        # --- 6. EXECUTION ENGINE ---
        async with safe_browser_context(p, launch_args, storage_state=session_data) as (browser, context, page):
            try:

                # Initialize Account Manager for just-in-time leasing if needed
                account_mgr = AccountManager()
                leased_account = None

                require_login = config.get("require_login", False)
                if require_login and target_domain:
                    leased_account = await account_mgr.lease_account(target_domain)
                    if leased_account:
                        await NervousSystem.publish_update(
                            job_id, "RUNNING",
                            f"[Security] Leased account: {leased_account['username']} (cookies: {'Yes' if leased_account['cookies'] else 'No'})",
                            "init"
                        )
                        if leased_account['cookies']:
                             await context.add_cookies(leased_account['cookies'])

                # --- 7. Initialize Global Network Sniffer ---
                from core.network_sniffer import NetworkSniffer
                global_sniffer = NetworkSniffer(target_domain=target_domain)
                await global_sniffer.start_sniffing(page)

                # --- GHOST SESSION VERIFICATION & AUTH LOCK ---
                if is_session_persistence_enabled() and target_domain and session_manager:
                    # 1. Verify if session is still valid (Lightweight DOM/Network check)
                    is_valid = await session_manager.verify_session(page, target_domain)
    
                    if not is_valid:
                        await NervousSystem.publish_update(
                            job_id, "RUNNING",
                            f"[Security] Ghost session detected for {target_domain}. Re-authenticating...",
                            "auth"
                        )
    
                        if leased_account:
                            # 2. Acquire Distributed Lock (Block Thundering Herds)
                            is_leader, lock_uuid = await account_mgr.acquire_auth_lock(
                                leased_account['id'], target_domain
                            )
    
                            if is_leader:
                                # LEADER: Execute headless login sequence
                                # Note: The main loop will handle login steps if they are present
                                logger.info(f"[Auth] Leader status active for {job_id}")
                                payload["_auth_lock"] = {"uuid": lock_uuid, "account_id": leased_account['id']}
                            else:
                                # FOLLOWER: Polling finished, fresh cookies should be in DB
                                logger.info(f"[Auth] Follower resumed. Re-fetching fresh session.")
                                # Re-lease to get the fresh cookies (already happens in lease_account)
                                fresh_account = account_mgr.lease_account(target_domain)
                                if fresh_account and fresh_account['cookies']:
                                    await context.add_cookies(fresh_account['cookies'])
                                    await page.reload()
                                    await user_logger.info("SESSION_RECOVERED")
    
                # --- DOWNLOAD HANDLER (Industrial-Grade) ---
                # TASK 2 FIX: Real blob storage implementation
                storage = get_storage()  # Get singleton storage client
    
                async def handle_download(download):
                    """Handles file downloads with actual storage upload."""
                    filename = download.suggested_filename
                    safe_filename = "".join(c for c in filename if c.isalnum() or c in '._-')
                    local_path = os.path.join(DOWNLOAD_DIR, f"{job_id}_{safe_filename}")
    
                    await user_logger.info("DOWNLOAD_START", filename=filename)
    
                    try:
                        # Save to local filesystem first
                        await download.save_as(local_path)
                        file_size = os.path.getsize(local_path)
    
                        logger.info(f"[{job_id}] Downloaded {filename} ({file_size} bytes)")
    
                        # TASK 2 FIX: Actually upload to S3/MinIO
                        if is_s3_upload_enabled() and storage:
                            try:
                                # Read file content
                                with open(local_path, 'rb') as f:
                                    file_data = f.read()
    
                                # Determine MIME type
                                content_type = "application/octet-stream"
                                lower_filename = filename.lower()
                                if lower_filename.endswith('.pdf'):
                                    content_type = "application/pdf"
                                elif lower_filename.endswith('.csv'):
                                    content_type = "text/csv"
                                elif lower_filename.endswith('.json'):
                                    content_type = "application/json"
                                elif lower_filename.endswith(('.png', '.jpg', '.jpeg', '.webp')):
                                    content_type = f"image/{lower_filename.split('.')[-1]}"
    
                                # Upload to storage and get presigned URL
                                storage_key = f"{job_id}/downloads/{safe_filename}"
                                final_url = await storage.upload(file_data, storage_key, content_type)
    
                                logger.info(f"[Storage] Uploaded {filename} to {storage_key}")
                                logger.info(f"[Storage] Presigned URL: {final_url[:80]}...")
    
                                # Clean up local file after successful upload
                                os.remove(local_path)
    
                            except StorageUploadError as e:
                                logger.error(f"[Storage] Upload failed for {filename}: {e}")
                                # Fall back to local path
                                final_url = f"file://{local_path}"
                            except Exception as e:
                                logger.error(f"[Storage] Unexpected error uploading {filename}: {e}")
                                final_url = f"file://{local_path}"
                        else:
                            if not storage:
                                logger.warning(f"[Storage] Storage not configured. Saved to local disk: {local_path}")
                            else:
                                logger.info(f"[Storage] S3 Upload Disabled. Saved to local disk: {local_path}")
                            final_url = f"file://{local_path}"
    
                        await user_logger.info("DOWNLOAD_COMPLETE", filename=filename)
    
                        # Store the URL in job context for later retrieval
                        logger.info(f"[{job_id}] Final download URL: {final_url}")
    
                    except Exception as e:
                        logger.error(f"Download failed for {filename}: {e}")
                        await user_logger.error("GENERIC_ERROR", error_details=str(e))
                        raise  # Don't silently continue
    
                page.on("download", handle_download)
    
                # Initialize the Co-Pilot (SmartFinder)
                finder = SmartFinder(page)
    

                # --- PRE-LOOP: Mandatory initial navigation ---
                if target_url:
                    logger.info(f"[{job_id}] Pre-loop navigation to {target_url}")
                    try:
                        await page.goto(target_url, timeout=NAVIGATION_TIMEOUT, wait_until="networkidle")
                        # Settle JS redirects
                        await asyncio.sleep(2)
                    except Exception as nav_err:
                        logger.warning(f"[{job_id}] Pre-loop navigation error (non-fatal): {nav_err}")
                # --- 6. EXECUTION LOOP ---
                for i, step in enumerate(steps):
                    # Use real node ID from graph converter for frontend event correlation
                    node_id = step.get("node_id", f"step-{i+1}")
                    action = step["action"]
                    raw_params = step.get("params", {})
    
                    # CRITICAL: Validate and substitute variables (fails fast on missing params)
                    step_params = validate_step_params(raw_params, params, i + 1)
    
                    logger.info(f"[{job_id}] Executing {action} (step {i+1}/{len(steps)})...")
    
                    # TELEMETRY: Node Start
                    await NervousSystem.publish(
                        f"quanta.telemetry.{job_id}",
                        json.dumps({"type": "log", "message": f"[Executor] Starting Node: {action} (step {i+1}/{len(steps)})"})
                    )
    
                    # --- ACTION SWITCH ---
                    if action == "GOTO":
                        url = step_params.get("url", "")
    
                        if not url:
                            logger.warning(f"[{job_id}] Skipping GOTO step {i+1}: URL is empty")
                            await NervousSystem.publish_update(job_id, "RUNNING", "Skipped empty navigation", node_id)
                            continue
    
                        # Use configurable timeout. wait_until="domcontentloaded" is
                        # faster than "load" and enough to unblock Playwright.
                        await page.goto(url, timeout=NAVIGATION_TIMEOUT, wait_until="domcontentloaded")
    
                        # Wait for network to settle (catches lazy-loaded assets)
                        await safe_wait_for_network_idle(page)
    
                        # ⚡ PHASE 2: WAF Evasion & Captcha Detection
                        waf_detected = await page.evaluate('''() => {
                            const isCloudflare = document.querySelector('#cf-spinner') || document.title.includes('Just a moment...') || window.__cf_chl_opt;
                            const isDatadome = document.querySelector('iframe[src*="datadome.co"]');
                            const isCaptcha = document.querySelector('iframe[src*="recaptcha"]') || document.querySelector('iframe[src*="hcaptcha"]');
    
                            if (isCloudflare) return 'Cloudflare';
                            if (isDatadome) return 'Datadome';
                            if (isCaptcha) return 'Captcha';
                            return null;
                        }''')
    
                        if waf_detected:
                            logger.warning(f"[{job_id}] WAF/Captcha Detected: {waf_detected}")
    
                            # FIX: Automated Solver Routing Loop
                            solver_success = False
                            for attempt in range(3):
                                logger.info(f"[{job_id}] Routing page to Automated Solver API (Attempt {attempt+1}/3)...")
                                # Mock solver hook (FlareSolverr/CapSolver implementation goes here)
                                await asyncio.sleep(5)
    
                                # Re-verify WAF presence
                                still_detected = await page.evaluate('''() => {
                                    const isCloudflare = document.querySelector('#cf-spinner') || document.title.includes('Just a moment...') || window.__cf_chl_opt;
                                    const isDatadome = document.querySelector('iframe[src*="datadome.co"]');
                                    const isCaptcha = document.querySelector('iframe[src*="recaptcha"]') || document.querySelector('iframe[src*="hcaptcha"]');
                                    return isCloudflare || isDatadome || isCaptcha;
                                }''')
    
                                if not still_detected:
                                    solver_success = True
                                    logger.info(f"[{job_id}] Automated Solver successfully bypassed the interstitial.")
                                    break
    
                            if not solver_success:
                                # Use Temporal ApplicationError to explicitly mark this non-retryable
                                # Prevents infinite loops DDOSing Cloudflare endpoints
                                raise ApplicationError(
                                    f"WAF_DETECTED: {waf_detected} block. Automated solver failed 3 times.",
                                    type="HumanInterventionRequired",
                                    non_retryable=True,
                                    details={"url": url}
                                )
    
                        # ⚡ SPA FIX: After networkidle, JavaScript frameworks (React/Vue/Svelte)
                        # still need time to execute and paint the final DOM.
                        # A brief settle window prevents the next step from reading an empty shell.
                        await asyncio.sleep(1.5)
    
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
                        # SELF-HEALING: Capture result to check for healing
                        # Note: click_with_retry needs update to return result, or we use finder directly here
                        # For now, we'll use finder directly to enable healing logic
    
                        # 1. Find Element (with healing)
                        find_result = await finder.find(
                            intent,
                            metadata=step_params.get("metadata"),
                            container_selector=step_params.get("container")
                        )
    
                        if not find_result.found:
                            raise Exception(f"Element not found: {intent}")
    
                        # 2. Heal if needed (Write-Back)
                        if find_result.needs_healing and find_result.new_signature:
                            logger.info(f"[{job_id}] 🩹 Healing recipe for '{intent}'")
                            # Update RAG
                            await finder.vector_db.store(
                                intent,
                                find_result.new_signature.get("selector", "unknown"),
                                find_result.new_signature.get("attributes")
                            )
                            # Update In-Memory Recipe (if running from recipe)
                            if 'recipe_mgr' in locals() and recipe:
                                # TODO: Implement recipe_mgr.update_step(workflow_id, i, find_result.new_signature)
                                pass
    
                            await user_logger.info("GENERIC_ERROR", error_details=f"Self-healed selector for {intent}")
    
                        # 3. Interact
                        element = find_result.element
                        await element.scroll_into_view_if_needed()
    
                        # Phase 2.5 FIX: Async Hydration Retry Loop
                        # React/Next.js elements might be visible in DOM but lacking event listeners for ~500ms
                        hydration_success = False
                        for click_attempt in range(3):
                            try:
                                await element.click(timeout=5000)
    
                                # Give JS framework time to process synthetic event
                                await asyncio.sleep(0.5)
    
                                # If we made it here without element throwing "Node Detached", we assume success
                                hydration_success = True
                                break
                            except Exception as e:
                                logger.warning(f"[{job_id}] Click hydration failure (Attempt {click_attempt+1}/3). Retrying...")
                                await asyncio.sleep(1.0)
    
                        if not hydration_success:
                            logger.error(f"[{job_id}] Element failed hydration after 3 attempts.")
    
                        # POST-CLICK STABILITY: Wait for potential navigation or dynamic load
                        await safe_wait_for_network_idle(page)
                        await asyncio.sleep(1.0) # Settle window for React/SPA frameworks
    
                        await user_logger.info("CLICKED_ELEMENT", element=intent)
    
                    elif action == "HOVER":
                        intent = step_params["intent"]
                        result = await finder.find(intent, timeout=10000)
                        if not result.found:
                            raise Exception(f"Element not found: {intent}")
                        element = result.element
                        await element.hover()
                        await user_logger.info("FOUND_ELEMENT", element=f"Hovered {intent}")
    
                    elif action == "PRESS_KEY":
                        key = step_params["key"]
                        await page.keyboard.press(key)
                        await user_logger.progress(f"Pressed key: {key}")
    
                    elif action == "UPLOAD_FILE":
                        intent = step_params["intent"]
                        file_path = step_params["file_path"]
                        # Ensure absolute path
                        if not os.path.isabs(file_path):
                            file_path = os.path.join(DOWNLOAD_DIR, file_path)
    
                        result = await finder.find(intent, timeout=10000)
                        if not result.found:
                            raise Exception(f"Element not found: {intent}")
                        element = result.element
                        await element.set_input_files(file_path)
                        await user_logger.info("FOUND_ELEMENT", element=f"Uploaded {os.path.basename(file_path)}")
    
                    elif action == "SCROLL":
                        # Scroll to element OR by pixels
                        if "intent" in step_params:
                            intent = step_params["intent"]
                            result = await finder.find(intent, timeout=10000)
                            if not result.found:
                                raise Exception(f"Element not found: {intent}")
                            element = result.element
                            await element.scroll_into_view_if_needed()
                        elif "delta_y" in step_params:
                            delta_y = int(step_params["delta_y"])
                            await page.mouse.wheel(0, delta_y)
                        await user_logger.progress("Scrolled page")
    
                    elif action == "DRAG_AND_DROP":
                        source_intent = step_params["source"]
                        target_intent = step_params["target"]
    
                        source_res = await finder.find(source_intent)
                        target_res = await finder.find(target_intent)
    
                        if not source_res.found: raise Exception(f"Source not found: {source_intent}")
                        if not target_res.found: raise Exception(f"Target not found: {target_intent}")
    
                        source = source_res.element
                        target = target_res.element
    
                        await source.drag_to(target)
                        await user_logger.progress(f"Dragged {source_intent} to {target_intent}")
    
                    elif action == "WAIT_FOR":
                        # Wait for selector, network, or timeout
                        if "selector" in step_params:
                            state = step_params.get("state", "visible")
                            timeout = int(step_params.get("timeout_ms", 10000))
                            await page.wait_for_selector(step_params["selector"], state=state, timeout=timeout)
                        elif "event" in step_params:
                            event = step_params["event"]
                            if event == "network_idle":
                                await safe_wait_for_network_idle(page)
                        elif "timeout_ms" in step_params:
                            await asyncio.sleep(int(step_params["timeout_ms"]) / 1000)
    
                        await user_logger.info("WAITING_NETWORK")
    
                    elif action == "EXTRACT":
                        intent = step_params["intent"]
                        attr = step_params.get("attribute") # None = text content
                        
                        raw_value = None
                        is_network_extraction = False

                        # PHASE 1: DOM Finding (Primary Source of Truth)
                        dom_result = await finder.find(intent, timeout=5000, scan_mode="all")
                        
                        # PHASE 2: Network Sniffing (Secondary Fallback/Optimization)
                        best_sniffer_payload = None
                        best_sniffer_score = -1
                        
                        if global_sniffer and hasattr(global_sniffer, 'captured_responses') and global_sniffer.captured_responses:
                            intent_keywords = [k.lower() for k in intent.split() if len(k) > 2]
                            total_keys = len(intent_keywords)
                            
                            for resp in global_sniffer.captured_responses:
                                data_str = str(resp["data"]).lower()
                                matches = sum(1 for k in intent_keywords if k in data_str)
                                
                                # Semantic Match Ratio (0.0 to 1.0)
                                match_ratio = matches / total_keys if total_keys > 0 else 0
                                
                                # Noise Penalty: Subtract points if the payload is massive but matches are few
                                noise_penalty = (len(data_str) / 50000.0) if match_ratio < 0.5 else 0
                                
                                final_score = (match_ratio * 10) - noise_penalty
                                
                                if final_score > best_sniffer_score:
                                    best_sniffer_score = final_score
                                    best_sniffer_payload = resp["data"]

                        # PHASE 3: Arbitration Logic
                        # Override DOM ONLY if Sniffer has extreme confidence (>90% match)
                        sniffer_threshold = 9.0 
                        
                        if best_sniffer_payload is not None and (not dom_result.found or best_sniffer_score >= sniffer_threshold):
                            raw_value = best_sniffer_payload
                            is_network_extraction = True
                            global_sniffer.captured_responses = [] # Prevent stale reads
                            logger.info(f"[{job_id}] Sniffer Override: '{intent}' (Score: {best_sniffer_score:.2f})")
                        elif dom_result.found:
                            element = dom_result.element
                            if attr:
                                raw_value = await element.get_attribute(attr)
                            else:
                                # Inspect tag for table extraction
                                tag_name = await element.evaluate("el => el.tagName.toLowerCase()")
                                if tag_name == "table":
                                    raw_value = await element.evaluate("""el => {
                                        const headers = Array.from(el.querySelectorAll('th')).map((th, i) => th.innerText.trim() || `col_${i+1}`);
                                        const rows = Array.from(el.querySelectorAll('tbody tr, tr')).filter(tr => !tr.querySelector('th'));

                                        let keys = headers;
                                        if (keys.length === 0 && rows.length > 0) {
                                            const maxCols = Math.max(...rows.map(tr => (tr.cells ? tr.cells.length : 0)));
                                            keys = Array.from({length: maxCols}, (_, i) => `col_${i+1}`);
                                        }

                                        const result = [];
                                        for (const row of rows) {
                                            if (!row.cells) continue;
                                            const cells = Array.from(row.cells).map(td => td.innerText.trim());
                                            if (cells.length > 0 && cells.some(c => c !== '')) {
                                                const rowDict = {};
                                                for (let i = 0; i < keys.length; i++) {
                                                    rowDict[keys[i]] = cells[i] !== undefined ? cells[i] : null;
                                                }
                                                result.push(rowDict);
                                            }
                                        }
                                        return result;
                                    }""")
                                else:
                                    raw_value = await element.inner_text()
                        else:
                            raise Exception(f"Extraction failed: '{intent}' not found in DOM or Network.")
    
                        # TYPE INFERENCE
                        typed_type = "string"
                        typed_content = raw_value
    
                        if isinstance(raw_value, list):
                            typed_type = "table"
                        elif isinstance(raw_value, str):
                            val_str = raw_value.strip()
                            lower_str = val_str.lower()
                            if lower_str == "true":
                                typed_type = "boolean"
                                typed_content = True
                            elif lower_str == "false":
                                typed_type = "boolean"
                                typed_content = False
                            else:
                                import re
                                # Basic heuristic for full-string numbers (allow formatted curr/commas)
                                if re.match(r'^[-+]?[^\d.-]*[\d.,]+[^\d.-]*$', val_str):
                                    clean_str = re.sub(r'[^\d.-]', '', val_str)
                                    if clean_str and clean_str != "-" and clean_str != ".":
                                        try:
                                            if '.' in clean_str:
                                                typed_content = float(clean_str)
                                                typed_type = "number"
                                            else:
                                                typed_content = int(clean_str)
                                                typed_type = "number"
                                        except ValueError:
                                            pass
    
                        # PHASE 3.5: Pydantic Extraction Guardrails (Kill Switch)
                        from .validator import ExtractionValidator
                        from pydantic import ValidationError
                        try:
                            validator_instance = ExtractionValidator(intent=intent, value=typed_content)
                            typed_content = validator_instance.value
                        except (ValueError, ValidationError) as e:
                            logger.warning(f"[{job_id}] Hallucination intercepted by Validator: {e}")
                            typed_content = None
                            typed_type = "null"

                        payload_dict = {
                            "type": typed_type,
                            "content": typed_content,
                            "confidence": 1.0
                        }
                        data_json = json.dumps(payload_dict)
    
                        # TELEMETRY: Extraction Payload
                        await NervousSystem.publish(
                            f"quanta.telemetry.{job_id}",
                            json.dumps({"type": "log", "message": f"[Extractor] Payload: {data_json}"})
                        )
    
                        logger.info(f"[{job_id}] Extracted '{intent}': ({typed_type}) {str(typed_content)[:100]}")
                        await user_logger.info("FOUND_ELEMENT", element=f"Extracted data from {intent}")
    
                        publish_str = str(typed_content) if typed_type != "table" else f"Table ({len(typed_content)} rows)"
                        await NervousSystem.publish_update(
                            job_id, "RUNNING", f"Extracted: {publish_str[:30]}...", node_id, data=data_json
                        )
    
                    elif action == "TYPE":
                        intent = step_params["intent"]
                        text = step_params["text"]
    
                        result = await finder.find(intent, timeout=10000)
                        if not result.found:
                            raise Exception(f"Element not found: {intent}")
                        element = result.element
    
                        # USE GAUSSIAN TYPING (The Humanizer)
                        await finder.glass.human_type(page, element, text)
    
                        await NervousSystem.publish_update(job_id, "RUNNING", f"Typed input safely", node_id)
    
                    elif action == "LOG":
                        # Support both 'content' (from old nodes) and 'message' (from new nodes)
                        content = step_params.get("message") or step_params.get("content", "")
    
                        data_json = json.dumps({"content": content})
    
                        logger.info(f"[{job_id}] LOG: {content}")
                        await NervousSystem.publish_update(
                            job_id, "SUCCESS", f"Log: {content[:30]}...", node_id, data=data_json
                        )
    
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
                        except PlaywrightTimeout:
                            logger.debug("Network idle timeout during sniff phase (expected for streaming sites)")
                        except Exception as e:
                            logger.warning(f"Network wait failed during sniff: {e}")
    
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
    
    
                    elif action == "DATA_TRANSFORM":
                        import csv
                        import io
    
                        input_data = step_params.get("inputData")
                        output_format = step_params.get("format", "json")
    
                        logger.info(f"[{job_id}] Transforming data to {output_format}")
    
                        try:
                            # Parse input data if it's a JSON string
                            data_to_format = input_data
                            if isinstance(input_data, str):
                                try:
                                    data_to_format = json.loads(input_data)
                                except:
                                    # Keep as raw string if not JSON
                                    pass
    
                            transformed_value = ""
    
                            if output_format == "json":
                                transformed_value = json.dumps(data_to_format, indent=2)
                            elif output_format == "csv":
                                # Simple CSV conversion for lists of dicts
                                if isinstance(data_to_format, list) and len(data_to_format) > 0:
                                    output = io.StringIO()
                                    if isinstance(data_to_format[0], dict):
                                        writer = csv.DictWriter(
                                            output,
                                            fieldnames=data_to_format[0].keys(),
                                            quoting=csv.QUOTE_ALL
                                        )
                                        if step_params.get("includeHeader", True):
                                            writer.writeheader()
                                        writer.writerows(data_to_format)
                                    else:
                                        writer = csv.writer(output, quoting=csv.QUOTE_ALL)
                                        writer.writerows([[x] for x in data_to_format])
                                    transformed_value = output.getvalue()
                                else:
                                    transformed_value = str(data_to_format)
                            elif output_format == "html_table":
                                if isinstance(data_to_format, list) and len(data_to_format) > 0 and isinstance(data_to_format[0], dict):
                                    headers = data_to_format[0].keys()
                                    html = "<table><thead><tr>"
                                    html += "".join([f"<th>{h}</th>" for h in headers])
                                    html += "</tr></thead><tbody>"
                                    for row in data_to_format:
                                        html += "<tr>" + "".join([f"<td>{row.get(h, '')}</td>" for h in headers]) + "</tr>"
                                    html += "</tbody></table>"
                                    transformed_value = html
                                else:
                                    transformed_value = f"<p>{str(data_to_format)}</p>"
                            else:
                                transformed_value = str(data_to_format)
    
                            # Update status with data for frontend preview
                            res_json = json.dumps({"content": transformed_value})
                            await NervousSystem.publish_update(
                                job_id, "RUNNING", f"Formatted data as {output_format}", node_id, data=res_json
                            )
    
                        except Exception as e:
                            logger.error(f"[{job_id}] Transformation failed: {e}")
                            await NervousSystem.publish_update(
                                job_id, "WARNING", f"Formatting failed: {str(e)}", node_id
                            )
    
                    # --- VISUAL PROOF (Screenshot) ---
                    # Take a tiny jpeg
                    screenshot = await page.screenshot(
                        type='jpeg',
                        quality=20,
                        scale="css",
                        animations="disabled",
                        caret="hide"
                    )
    
                    # TASK 2 FIX: Upload screenshot to storage if enabled
                    screenshot_url = None
                    if is_s3_upload_enabled() and storage:
                        try:
                            screenshot_url = await storage.upload_screenshot(
                                screenshot, job_id, i + 1
                            )
                            logger.debug(f"[Storage] Screenshot uploaded: {screenshot_url[:60]}...")
                        except Exception as e:
                            logger.warning(f"[Storage] Screenshot upload failed: {e}")
                            # Continue with embedded screenshot fallback
    
                    # Send the visual proof to the dashboard
                    # If storage upload succeeded, we could send URL instead of bytes
                    # For now, still embed for real-time preview
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
    
            except ApplicationError as app_err:
                logger.error(f"Job Failed (ApplicationError): {app_err}", exc_info=True)
                workflow_succeeded = False
                raise app_err  # Re-raise Temporal errors exactly as they are so Temporal halts
    
            except Exception as e:
                logger.error(f"Job Failed: {e}", exc_info=True)
                workflow_succeeded = False  # Explicitly mark failure
    
                # INDUSTRIAL: Capture screenshot for debugging
                failure_screenshot = b""
                if 'page' in locals() and page:
                    try:
                        if not page.is_closed():
                            failure_screenshot = await page.screenshot(type='jpeg', quality=60)
                    except:
                        pass
    
                # TELEMETRY: Failure with Stack Trace
                stack_trace = traceback.format_exc()
                await NervousSystem.publish(
                    f"quanta.telemetry.{job_id}",
                    json.dumps({"type": "log", "message": f"[Executor] Job Failed: {str(e)}\n{stack_trace}"})
                )
    
                await NervousSystem.publish_update(
                    job_id, "FAILED",
                    f"Critical Error: {str(e)}",
                    "error",
                    screenshot=failure_screenshot if failure_screenshot else None
                )
    
                # Explicitly close Playwright resources to prevent Zombie Chromium
                if 'context' in locals() and context:
                    await context.close()
                if 'browser' in locals() and browser:
                    await browser.close()
    
                # Re-raise so Temporal knows to retry
                raise e
    
            finally:
                # =================================================================
                # CRITICAL: Robust cleanup with existence checks
                # =================================================================
    
                # 1. Save session on success (BEFORE releasing account)
                if workflow_succeeded and is_session_persistence_enabled():
                    if 'session_manager' in locals() and session_manager and 'context' in locals() and context:
                        if 'target_domain' in locals() and target_domain and 'user_id' in locals():
                            try:
                                await session_manager.save_session(user_id, target_domain, context)
                                logger.info(f"[Session] Saved encrypted session for {target_domain}")
                            except Exception as session_err:
                                logger.warning(f"[Session] Failed to save session: {session_err}")
    
                # 2. Release account back to pool
                if 'leased_account' in locals() and leased_account and 'account_mgr' in locals():
                    try:
                        # Check if context exists before accessing cookies
                        new_cookies = None
                        if 'context' in locals() and context:
                            try:
                                # Context is safely closed by context manager AFTER this block
                                new_cookies = await context.cookies()
                            except Exception as cookie_err:
                                logger.warning(f"Could not capture cookies: {cookie_err}")
                        await account_mgr.release_account(
                            leased_account['id'],
                            new_cookies=new_cookies,
                            success=workflow_succeeded
                        )
                        logger.info(f"Released account {leased_account['username']}")
    
                    except Exception as release_err:
                        logger.error(f"Failed to release account: {release_err}")
    
                # --- RELEASE AUTH LOCK ---
                if "_auth_lock" in payload and account_mgr:
                    lock_info = payload["_auth_lock"]
                    await account_mgr.release_auth_lock(
                        lock_info["account_id"],
                        target_domain,
                    )
    
