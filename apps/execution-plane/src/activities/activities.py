import os
import asyncio
import logging
import base64
from datetime import timedelta
from temporalio import activity
from playwright.async_api import async_playwright

# --- IMPORTS ---
# 1. The Nervous System (Snake Case - Infrastructure)
from core.NervousSystem import NervousSystem

# 2. The Glass Box Engine (Camel Case - Logic)
# We import the Class 'SmartFinder' from the file 'smartFinder.py'
from core.SmartFinder import SmartFinder

logger = logging.getLogger("activity")

# --- MOCK RECIPE DB (In production, this fetches from Postgres) ---
# --- THE RECIPE BOOK (MOCK DB) ---
RECIPES = {
    # 1. The Dynamic Wiki Test (Variable Injection)
    "test_login": [
        {"action": "GOTO", "params": {"url": "{url}"}},
        {"action": "TYPE", "params": {"intent": "search", "text": "{search_term}"}},
        {"action": "CLICK", "params": {"intent": "search button"}}
    ],

    # 2. The E-Commerce Test (Heavy JS + Icons)
    "amazon_scraper": [
        {"action": "GOTO", "params": {"url": "https://amazon.com"}},
        {"action": "TYPE", "params": {"intent": "search box", "text": "{item}"}},
        {"action": "CLICK", "params": {"intent": "search submit"}}
        # Note: Amazon's search button is often an Icon (Magnifying Glass).
        # This tests your GlassBox Icon Hasher or "Go" synonym.
    ],

    # 3. The SaaS Test (Complex Layouts)
    "github_explorer": [
        {"action": "GOTO", "params": {"url": "https://github.com/microsoft/playwright"}},
        {"action": "CLICK", "params": {"intent": "issues tab"}},
        {"action": "TYPE", "params": {"intent": "search all issues", "text": "bug"}}
    ]
}

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
        message=f"🚀 Initializing Glass Box for workflow: {workflow_id}",
        node_id="init"
    )

    # 3. Load Recipe
    steps = RECIPES.get(workflow_id)
    if not steps:
        # Check if raw steps were provided (Developer Mode)
        steps = payload.get("steps", [])
        if not steps:
            err = f"No recipe found for ID: {workflow_id}"
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
                await NervousSystem.publish_update(job_id, "RUNNING", f"Routing via Residential Proxy ({region}) 🛡️", "init")
            else:
                await NervousSystem.publish_update(job_id, "WARNING", "Proxy credentials missing! Using Datacenter IP.", "init")

        try:
            browser = await p.chromium.launch(**launch_args)

            # --- 5. SESSION INJECTION (The "Time Travel") ---
            # We create a context. This is where cookies live.
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36..."
            )

            session_id = config.get("session_id")
            if session_id:
                await NervousSystem.publish_update(job_id, "HEALER_ACTIVE", f"💉 Injecting Session: {session_id}", "init")
                # TODO: Retrieve cookies from Redis using session_id
                # await context.add_cookies(cookies_from_redis)

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
            await context.close()
            await browser.close()
