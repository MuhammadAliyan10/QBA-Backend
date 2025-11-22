from playwright.async_api import Page, ElementHandle
from algorithms.levenshtein import LevenshteinScorer, DOMElement
from core.dom_pruner import DOMPruner
from core.llm_client import LLMClient
from core.nervous_system import NervousSystem

class SmartFinder:
    def __init__(self, job_id: str, node_id: str):
        self.job_id = job_id
        self.node_id = node_id
        self.scorer = LevenshteinScorer()
        self.pruner = DOMPruner()
        self.brain = LLMClient()

    async def find(self, page: Page, intent: str) -> ElementHandle:
        # Wait for page to be fully loaded
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except:
            pass
        
        # 1. SNIPER (Fast, Free, Local)
        await NervousSystem.publish_update(self.job_id, self.node_id, "RUNNING", "Engaging Sniper...")
        
        # Scrape interactive elements - wait a bit for dynamic content
        await page.wait_for_timeout(500)  # Give page 500ms to settle
        elements_handle = await page.query_selector_all("button, a, input, [role='button']")
        dom_elements = []
        handle_map = {} # Map index to handle

        for i, handle in enumerate(elements_handle):
            try:
                txt = await handle.inner_text()
                attrs = await handle.evaluate("el => el.getAttributeNames().reduce((obj, name) => ({...obj, [name]: el.getAttribute(name)}), {})")
                tag = await handle.evaluate("el => el.tagName.toLowerCase()")
                
                el_obj = DOMElement(tag_name=tag, text=txt, attributes=attrs)
                dom_elements.append(el_obj)
                handle_map[i] = handle
            except:
                continue

        await NervousSystem.publish_update(self.job_id, self.node_id, "RUNNING", f"Sniper scanned {len(dom_elements)} interactive elements.")
        
        if len(dom_elements) == 0:
            await NervousSystem.publish_update(self.job_id, self.node_id, "WARNING", "Sniper found 0 elements. Engaging Brain immediately.")
        else:
            best = self.scorer.find_best_candidate(dom_elements, intent)

            if best:
                await NervousSystem.publish_update(self.job_id, self.node_id, "RUNNING", f"Sniper best score: {best.score:.2f} for '{best.element.text[:30]}'")

            if best and best.score > 0.75:
                await NervousSystem.publish_update(self.job_id, self.node_id, "SUCCESS", f"Sniper hit! {best.match_reason}")
                idx = dom_elements.index(best.element)
                return handle_map[idx]

        # 2. COMPRESSOR & BRAIN (Slow, Costly, AI)
        await NervousSystem.publish_update(self.job_id, self.node_id, "WARNING", "Sniper missed. Engaging Brain...")
        
        full_html = await page.content()
        cleaned_html, tokens = self.pruner.prune(full_html)
        
        await NervousSystem.publish_update(self.job_id, self.node_id, "RUNNING", f"Pruned HTML to {tokens} tokens. Asking LLM...")
        
        result = self.brain.find_element(cleaned_html, intent)
        selector = result.get("selector")
        confidence = result.get("confidence", 0.0)

        if selector and confidence > 0.5:
            await NervousSystem.publish_update(self.job_id, self.node_id, "SUCCESS", f"Brain found selector: {selector}")
            try:
                element = await page.wait_for_selector(selector, timeout=5000)  # Increased to 5s
                return element
            except:
                pass
            
            if False: # Dummy to keep structure
                pass
            else:
                 await NervousSystem.publish_update(self.job_id, self.node_id, "FAILED", f"Brain selector validation failed: {selector}")
                 raise Exception(f"Brain returned invalid selector: {selector}")
        
        raise Exception(f"SmartFinder failed to find element for intent: {intent}")
