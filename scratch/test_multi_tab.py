import asyncio
from playwright.async_api import async_playwright, BrowserContext, Page

class ContextManager:
    def __init__(self, context: BrowserContext, initialPage: Page):
        self.context = context
        self.activePage = initialPage
        self.context.on("page", self._onNewPage)
        print("[ContextManager] Initialized with primary tab.")

    async def _onNewPage(self, newPage: Page) -> None:
        print("[ContextManager] Popup detected! Capturing new tab...")
        await newPage.wait_for_load_state("domcontentloaded")
        self.activePage = newPage
        print("[ContextManager] Active execution context switched to new tab.")

    def getActivePage(self) -> Page:
        return self.activePage

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page1 = await context.new_page()
        
        manager = ContextManager(context, page1)
        
        html_content = '''
        <!DOCTYPE html>
        <html>
        <body>
            <h1>Primary Tab</h1>
            <button id="open-tab" onclick="window.open('about:blank', '_blank')">Open Target in New Tab</button>
            <script>
                localStorage.setItem("session_token", "secure-token-123");
            </script>
        </body>
        </html>
        '''
        
        await page1.route("http://dummy.local/", lambda route: route.fulfill(body=html_content, content_type="text/html"))
        await page1.goto("http://dummy.local/")
        
        print("--- SCENARIO: Multi-Tab Context Handoff ---")
        print("[Engine] Intent: 'Click the button to open target'")
        
        token1 = await page1.evaluate("localStorage.getItem('session_token')")
        print(f"[Engine] Tab 1 LocalStorage Token: {token1}")
        
        async with context.expect_page() as popup_info:
            await page1.locator("#open-tab").click()
            
        new_page = await popup_info.value
        await asyncio.sleep(0.5) 
        
        active_page = manager.getActivePage()
        
        await active_page.evaluate("document.body.innerHTML = '<h1>Target Tab</h1>';")
        
        token2 = await active_page.evaluate("localStorage.getItem('session_token')")
        print(f"[Engine] Tab 2 Active: {active_page == new_page}")
        print(f"[Engine] Tab 2 LocalStorage Token: {token2}")
        print("[Engine] Context synchronization and handoff complete!")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
