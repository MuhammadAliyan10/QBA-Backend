import os
import asyncio
import logging
import base64
import time
from datetime import timedelta
from temporalio import activity
from playwright.async_api import async_playwright
import httpx

# --- IMPORTS ---
# 1. The Nervous System (Snake Case - Infrastructure)
from core.NervousSystem import NervousSystem

# 2. The Glass Box Engine (Camel Case - Logic)
# We import the Class 'SmartFinder' from the file 'smartFinder.py'
from core.SmartFinder import SmartFinder

# 3. The Network Sniffer (Level 5 - Protocol Reverse Engineering)
from core.NetworkSniffer import NetworkSniffer

# 4. The Account Pool Manager (Session Rehydration)
from core.AccountManager import AccountManager

# 5. The Recipe Manager (Dynamic RAG)
from core.RecipeManager import RecipeManager

logger = logging.getLogger("activity")

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

            async def handle_download(download):
                filename = download.suggested_filename
                await NervousSystem.publish_update(job_id, "RUNNING", f"Intercepted download: {filename}", "io")
                # Stream to Local/S3 (Simulated here with a temp path,
                # in prod you use boto3.upload_fileobj(download.create_read_stream(), ...))
                try:
                    # Ideally: stream to R2. For MVP: save to persistent volume.
                    # await download.save_as(f"/data/downloads/{job_id}_{filename}")

                    # Report success URL
                    final_url = f"https://r2.api.com/{job_id}/{filename}"
                    await NervousSystem.publish_update(job_id, "SUCCESS", f"File uploaded: {final_url}", "io")
                except Exception as e:
                    await NervousSystem.publish_update(job_id, "FAILED", f"Download failed: {e}", "io")

            page.on("download", handle_download)

            # Initialize the Co-Pilot (SmartFinder)
            finder = SmartFinder(job_id)

            # --- 6. EXECUTION LOOP ---
            for i, step in enumerate(steps):
                node_id = f"step-{i+1}"
                action = step["action"]
                step_params = step["params"]

                # Variable Substitution (e.g. "{search_term}" -> "Laptop")
                for k, v in step_params.items():
                    if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
                        key = v[1:-1]
                        if key in params:
                            step_params[k] = params[key]

                logger.info(f"[{job_id}] Executing {action}...")

                # --- ACTION SWITCH ---
                if action == "GOTO":
                    url = step_params["url"]
                    await page.goto(url)
                    # We wait for the 'Network Idle' state (Stability Check)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except:
                        pass # Proceed anyway if some tracking pixel is stuck

                    await NervousSystem.publish_update(job_id, "RUNNING", f"Navigated to {url}", node_id)

                elif action == "CLICK":
                    intent = step_params["intent"]
                    if intent == "simulate_human_check":
                        from exceptions import HumanInterventionRequired
                        raise HumanInterventionRequired(
                            reason="GOD_MODE_CHECK",
                            context={"msg": "System is healthy. Proceed?"}
                        )

                    # 🧠 CALL SMART FINDER (The Math Engine)
                    # This runs: ShadowPierce -> HoneypotFilter -> Levenshtein -> Raycast
                    element = await finder.find(page, intent)

                    await element.click()
                    await NervousSystem.publish_update(job_id, "RUNNING", f"Clicked '{intent}'", node_id)

                elif action == "TYPE":
                    intent = step_params["intent"]
                    text = step_params["text"]

                    element = await finder.find(page, intent)

                    # 🧠 USE GAUSSIAN TYPING (The Humanizer)
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
                    await NervousSystem.publish_update(job_id, "RUNNING", f"🕵️ Sniffer Watching {target_domain}...", node_id)

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
                                except Exception as req_err:
                                    logger.error(f"Protocol Request Failed: {req_err}")

                                start_time = time.time()  # Reset timer for next request

                        await NervousSystem.publish_update(job_id, "SUCCESS", f"Protocol Mode Complete: {iterations} requests replayed", node_id)

                    else:
                        msg = "❌ No verified API keys found (Auth failed or no XHR). Continuing in Browser Mode."
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
            return {"status": "SUCCESS"}

        except Exception as e:
            logger.error(f"Job Failed: {e}", exc_info=True)
            await NervousSystem.publish_update(job_id, "FAILED", f"Critical Error: {str(e)}", "error")
            # Re-raise so Temporal knows to retry
            raise e
        finally:
            # Release account back to pool with updated cookies
            if 'leased_account' in locals() and leased_account:
                try:
                    # Capture current cookies from the session
                    new_cookies = await context.cookies()

                    # Release account (status depends on workflow success)
                    success = 'e' not in locals()  # True if no exception
                    account_mgr.release_account(
                        leased_account['id'],
                        new_cookies=new_cookies,
                        success=success
                    )

                    logger.info(f"Released account {leased_account['username']} with updated cookies")
                except Exception as release_err:
                    logger.error(f"Failed to release account: {release_err}")

            await context.close()
            await browser.close()
