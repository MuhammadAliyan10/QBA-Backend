import asyncio
import json
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError

DEFAULT_TIMEOUT_MS: int = 3000
FALLBACK_TIMEOUT_MS: int = 15000
MAX_DOM_SNIPPET_LENGTH: int = 5000

class LlmProvider:
    async def generateSelector(self, intent: str, domContext: str) -> str:
        print(f"[LLM] Received intent: '{intent}'")
        print(f"[LLM] Extracted {len(domContext)} chars of context.")
        return "#real-button"

class CacheProvider:
    def __init__(self):
        self.store = {"click the target": "#bad-selector"}

    async def getSelector(self, intent: str) -> str:
        val = self.store.get(intent)
        print(f"[Cache] getSelector('{intent}') -> {val}")
        return val

    async def setSelector(self, intent: str, selector: str) -> None:
        print(f"[Cache] setSelector('{intent}', '{selector}')")
        self.store[intent] = selector

class DomReducer:
    @staticmethod
    def pruneHtml(rawHtml: str) -> str:
        soup = BeautifulSoup(rawHtml, "html.parser")
        
        for tag in soup(["script", "style", "noscript", "iframe", "svg", "meta", "link", "canvas"]):
            tag.decompose()
            
        for tag in soup.find_all(style=True):
            styleValue = str(tag.get("style")).lower()
            if "display: none" in styleValue or "visibility: hidden" in styleValue or "opacity: 0" in styleValue:
                tag.decompose()
                
        for tag in soup.find_all("div"):
            if not tag.find_all(recursive=False) and not tag.get_text(strip=True):
                tag.decompose()
                
        return soup.prettify()

    @staticmethod
    async def getStructuralMap(page: Page) -> str:
        try:
            return await page.locator("body").aria_snapshot()
        except AttributeError:
            return await page.evaluate("() => document.body.innerText")

class SemanticAutoHealer:
    def __init__(self, llmClient: LlmProvider, cacheClient: CacheProvider):
        self.llmClient = llmClient
        self.cacheClient = cacheClient

    async def executeAction(
        self,
        page: Page,
        intent: str,
        actionType: str,
        actionValue: str = None
    ) -> None:
        cachedSelector = await self.cacheClient.getSelector(intent)
        
        if cachedSelector:
            try:
                print(f"[AutoHealer] Attempting action with cached selector: {cachedSelector}")
                await self._performAction(page, cachedSelector, actionType, actionValue, 1000)
                print("[AutoHealer] Success!")
                return
            except PlaywrightTimeoutError:
                print(f"[AutoHealer] Timeout on cached selector. Triggering fallback...")
                
        structuralMap = await DomReducer.getStructuralMap(page)
        rawHtml = await page.content()
        prunedDom = DomReducer.pruneHtml(rawHtml)
        
        domContext = f"Accessibility Tree (AOM):\n{structuralMap}\n\nPruned DOM Snippet:\n{prunedDom[:MAX_DOM_SNIPPET_LENGTH]}"
        
        newSelector = await self.llmClient.generateSelector(intent, domContext)
        
        if not newSelector:
            raise ValueError(f"LLM failed to generate a fallback selector for intent: {intent}")
            
        print(f"[AutoHealer] LLM returned new selector: {newSelector}")
        await self._performAction(page, newSelector, actionType, actionValue, FALLBACK_TIMEOUT_MS)
        await self.cacheClient.setSelector(intent, newSelector)
        print("[AutoHealer] Fallback successful!")

    async def _performAction(
        self, 
        page: Page, 
        selector: str, 
        actionType: str, 
        actionValue: str, 
        timeoutMs: int
    ) -> None:
        targetLocator = page.locator(selector).first
        
        if actionType == "click":
            await targetLocator.click(timeout=timeoutMs)
        elif actionType == "fill":
            await targetLocator.fill(actionValue, timeout=timeoutMs)

async def main():
    html_content = '''
    <!DOCTYPE html>
    <html>
    <head><style>.hidden { display: none; }</style></head>
    <body>
        <div class="hidden">Secret Data</div>
        <div></div> <!-- Empty div -->
        <button id="real-button" onclick="console.log('Clicked!')">Click Me</button>
        <svg><path d="M10 10 H 90 V 90 H 10 L 10 10"/></svg>
    </body>
    </html>
    '''
    
    llm = LlmProvider()
    cache = CacheProvider()
    healer = SemanticAutoHealer(llm, cache)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(html_content)
        
        intent = "click the target"
        print("--- RUN 1: Bad Cache Entry ---")
        await healer.executeAction(page, intent, "click")
        
        print("\n--- RUN 2: Good Cache Entry ---")
        await healer.executeAction(page, intent, "click")
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
