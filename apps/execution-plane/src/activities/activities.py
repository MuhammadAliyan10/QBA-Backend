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

# Feature Flags
from config import is_s3_upload_enabled, is_session_persistence_enabled

# Session Persistence (Encrypted Browser State)
from core.browser.session import SessionManager, get_session_manager

# TASK 2 FIX: Import Universal Storage for actual S3/MinIO uploads
from core.storage import get_storage, is_storage_available, StorageUploadError

# --- IMPORTS ---
# 1. The Nervous System (Snake Case - Infrastructure)
from core.NervousSystem import NervousSystem

# 2. The Glass Box Engine (Camel Case - Logic)
from core.selector.smartFinder import SmartFinder

# 3. The Network Sniffer (Level 5 - Protocol Reverse Engineering)
from core.NetworkSniffer import NetworkSniffer

# 4. The Account Pool Manager (Session Rehydration)
from core.AccountManager import AccountManager

# 5. The Recipe Manager (Dynamic RAG)
from core.recipe.recipeManager import RecipeManager

# 6. User-Facing Logger (The Voice of the Glass Box)
from core.UserFacingLogger import UserFriendlyLogger

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
    params = payload.get("params", {})

    # The 'config' dictionary contains our Glass Box settings
    config = payload.get("config", {})

    # 2. Initialize User Logger
    user_logger = UserFriendlyLogger(job_id)

    # 3. Notify Nervous System: START
    await user_logger.info("PROCESSING_RAG") # "Thinking about the next step..."

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
            # SESSION PERSISTENCE: Try to restore encrypted session
            session_manager = None
            session_data = None
            target_domain = SessionManager.extract_domain(url) if 'url' in params else None
            user_id = payload.get("user_id", job_id)  # Fall back to job_id if no user

            if is_session_persistence_enabled() and target_domain:
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
                    session_data = None

            # Create context with or without session state
            if session_data:
                context = await browser.new_context(
                    storage_state=session_data,
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36..."
                )
                logger.info(f"[Session] Using restored session for {target_domain}")
            else:
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36..."
                )

            # Fast Path: Inject cookies if available
            cookie_valid = False
            if leased_account and leased_account['cookies']:
                try:
                    await context.add_cookies(leased_account['cookies'])
                    await user_logger.info("FOUND_ELEMENT", element="Saved Session")
                    cookie_valid = True
                except Exception as e:
                    logger.warning(f"Cookie injection failed: {e}")
                    cookie_valid = False

            page = await context.new_page()

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
                            find_result.new_signature["selector"],
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
                    await element.click(timeout=5000)

                    await user_logger.info("CLICKED_ELEMENT", element=intent)

                elif action == "HOVER":
                    intent = step_params["intent"]
                    element = await finder.find(page, intent)
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

                    element = await finder.find(page, intent)
                    await element.set_input_files(file_path)
                    await user_logger.info("FOUND_ELEMENT", element=f"Uploaded {os.path.basename(file_path)}")

                elif action == "SCROLL":
                    # Scroll to element OR by pixels
                    if "intent" in step_params:
                        intent = step_params["intent"]
                        element = await finder.find(page, intent)
                        await element.scroll_into_view_if_needed()
                    elif "delta_y" in step_params:
                        delta_y = int(step_params["delta_y"])
                        await page.mouse.wheel(0, delta_y)
                    await user_logger.progress("Scrolled page")

                elif action == "DRAG_AND_DROP":
                    source_intent = step_params["source"]
                    target_intent = step_params["target"]

                    source = await finder.find(page, source_intent)
                    target = await finder.find(page, target_intent)

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

                    element = await finder.find(page, intent)

                    if attr:
                        value = await element.get_attribute(attr)
                    else:
                        value = await element.text_content()

                    # Store in job results (could be passed to next steps via context)
                    logger.info(f"[{job_id}] Extracted '{intent}': {value}")
                    await user_logger.info("FOUND_ELEMENT", element=f"Extracted data from {intent}")

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

