import subprocess
from temporalio import activity
from playwright.async_api import async_playwright
from algorithms.levenshtein import LevenshteinScorer, DOMElement
from core.dom_pruner import DOMPruner
from core.nervous_system import NervousSystem
from core.smart_finder import SmartFinder
from api.gen.python.v1.workflow_pb2 import BrowserStepInput

def ensure_browsers_installed():
    """Checks if Playwright browsers are installed. If not, installs them."""
    try:
        subprocess.run(["playwright", "install", "chromium"], check=True)
    except Exception as e:
        print(f"⚠️ Auto-install failed: {e}")


@activity.defn
async def browser_automation_activity(input: BrowserStepInput) -> dict:
    # 1. Notify UI: "Starting Step"
    await NervousSystem.publish_update(
        input.job_id, input.node_id, "RUNNING", f"Executing {input.action}..."
    )

    async with async_playwright() as p:
        try:
            # Launch Headless (Invisible)
            browser = await p.chromium.launch(headless=True)
        except Exception:
            # Fallback: Try to install and relaunch
            await NervousSystem.publish_update(
                input.job_id, input.node_id, "WARNING", "Installing Browsers..."
            )
            subprocess.run(["playwright", "install", "chromium"], check=True)
            browser = await p.chromium.launch(headless=True)

        page = await browser.new_page()

        try:
            # --- ACTION: NAVIGATION ---
            if input.action == "GOTO":
                url = input.params.get("url")
                await page.goto(url)
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except:
                    pass
                title = await page.title()
                await NervousSystem.publish_update(
                    input.job_id, input.node_id, "SUCCESS", f"Loaded {title}"
                )
                return {"title": title}

            # --- ACTION: SMART CLICK (THE SNIPER + BRAIN) ---
            elif input.action == "CLICK":
                intent = input.params.get("intent", "submit")
                finder = SmartFinder(input.job_id, input.node_id)
                element = await finder.find(page, intent)
                
                await element.click()
                await NervousSystem.publish_update(
                    input.job_id, input.node_id, "SUCCESS", "Clicked successfully"
                )
                return {"status": "clicked"}

            # --- ACTION: TYPE ---
            elif input.action == "TYPE":
                intent = input.params.get("intent", "input field")
                text = input.params.get("text", "")
                
                finder = SmartFinder(input.job_id, input.node_id)
                element = await finder.find(page, intent)
                
                await element.fill(text)
                await NervousSystem.publish_update(
                    input.job_id, input.node_id, "SUCCESS", f"Typed '{text}'"
                )
                return {"status": "typed", "text": text}
            
            # --- ACTION: SCROLL ---
            elif input.action == "SCROLL":
                direction = input.params.get("direction", "down")
                amount = input.params.get("amount", "500")
                
                if direction == "down":
                    await page.evaluate(f"window.scrollBy(0, {amount})")
                else:
                    await page.evaluate(f"window.scrollBy(0, -{amount})")
                    
                await NervousSystem.publish_update(
                    input.job_id, input.node_id, "SUCCESS", f"Scrolled {direction} by {amount}"
                )
                return {"status": "scrolled"}

        except Exception as e:
            await NervousSystem.publish_update(
                input.job_id, input.node_id, "FAILED", str(e)
            )
            raise e
        finally:
            await browser.close()
