from dataclasses import dataclass
from temporalio import activity
from playwright.async_api import async_playwright
from algorithms.heuristic import HeuristicScorer, DOMElement
from core.nervous_system import NervousSystem


@dataclass
class BrowserStepInput:
    job_id: str
    node_id: str
    action: str
    params: dict


@activity.defn
async def browser_automation_activity(input: BrowserStepInput) -> dict:
    # ? 1. Notify UI: "Starting Step"
    await NervousSystem.publish_update(
        input.job_id, input.node_id, "RUNNING", f"Executing {input.action}..."
    )

    async with async_playwright() as p:
        # *Launch Headless (Invisible)
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            #! --- ACTION: NAVIGATION ---
            if input.action == "GOTO":
                url = input.params.get("url")
                await page.goto(url)
                title = await page.title()
                await NervousSystem.publish_update(
                    input.job_id, input.node_id, "SUCCESS", f"Loaded {title}"
                )
                return {"title", title}

            elif input.action == "CLICK":
                intent = input.params.get("intent", "submit")

                #! A. Scrape Interactive Elements (The "DOM Pruner" Logic Lite)
                element_handle = await page.query_selector_all(
                    "button", "a", "input[type='button']", "input[type='submit']"
                )
                dom_elements = []

                for handle in element_handle:
                    txt = await handle.inner_text()
                    attrs = await handle.evaluate(
                        "el => el.getAttributeNames().reduce((obj, name) => ({...obj, [name]: el.getAttribute(name)}), {})"
                    )
                    dom_elements.append(
                        DOMElement(tag_name="button", text=txt, attributes=attrs)
                    )

                scorer = HeuristicScorer()
                best = scorer.find_best_candidate(dom_elements, intent)

                if best and best.score > 0.75:
                    # C. Click the winner
                    await NervousSystem.publish_update(
                        input.job_id,
                        input.node_id,
                        "RUNNING",
                        f"Sniper found target: {best.match_reason}",
                    )
                    # In eal Playwright, we need to find the handle again to click it
                    # For MVP, we assume selector stability or use text locator

                    await page.get_by_text(best.element.text).first().click()
                    await NervousSystem.publish_update(
                        input.job_id,
                        input.node_id,
                        "SUCCESS",
                        f"Clicked element: {best.element.text}",
                    )
                    return {"status": "clicked", "confidence": best.score}
                else:
                    # D. Fallback to AI (Not implemented in this sprint)
                    await NervousSystem.publish_update(
                        input.job_id,
                        input.node_id,
                        "FAILED",
                        "Heuristic failed. Needs AI.",
                    )
                    raise Exception("Element not found via Math")

        except Exception as e:
            await NervousSystem.publish_update(
                input.job_id, input.node_id, "FAILED", str(e)
            )
            raise e
        finally:
            await browser.close()
