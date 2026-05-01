import asyncio
from typing import Optional, Dict
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, ProxySettings
from playwright_stealth import Stealth

class StealthConfig:
    def __init__(
        self, 
        proxy_url: Optional[str] = None, 
        proxy_username: Optional[str] = None, 
        proxy_password: Optional[str] = None,
        timezone: str = "America/New_York",
        locale: str = "en-US",
        latitude: float = 40.7128,
        longitude: float = -74.0060,
        user_agent: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ):
        self.proxy_url = proxy_url
        self.proxy_username = proxy_username
        self.proxy_password = proxy_password
        self.timezone = timezone
        self.locale = locale
        self.latitude = latitude
        self.longitude = longitude
        self.user_agent = user_agent

class StealthBrowserManager:
    def __init__(self, browser: Browser):
        self.browser = browser

    async def create_stealth_context(self, config: StealthConfig) -> BrowserContext:
        proxy_settings = None
        if config.proxy_url:
            proxy_settings = {
                "server": config.proxy_url,
                "username": config.proxy_username,
                "password": config.proxy_password
            }

        print(f"[StealthEngine] Initializing context with timezone: {config.timezone}, locale: {config.locale}")
        
        context = await self.browser.new_context(
            proxy=proxy_settings,
            timezone_id=config.timezone,
            locale=config.locale,
            geolocation={"latitude": config.latitude, "longitude": config.longitude},
            permissions=["geolocation"],
            user_agent=config.user_agent,
            viewport={"width": 1920, "height": 1080},
            device_scale_factor=1,
            has_touch=False,
            is_mobile=False,
            color_scheme="dark"
        )

        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            window.chrome = {
                runtime: {}
            };
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3]
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en']
            });
            
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Google Inc. (Apple)';
                if (parameter === 37446) return 'ANGLE (Apple, Apple M1 Pro, OpenGL 4.1)';
                return getParameter.apply(this, [parameter]);
            };
        """)

        print("[StealthEngine] Context configured with proxy, geo-sync, and evasion scripts.")
        return context

    async def setup_page(self, context: BrowserContext) -> Page:
        page = await context.new_page()
        stealth = Stealth()
        await stealth.apply_stealth_async(page)
        print("[StealthEngine] playwright-stealth applied to new page.")
        return page

async def main():
    config = StealthConfig(
        timezone="Europe/Berlin",
        locale="de-DE",
        latitude=52.5200,
        longitude=13.4050,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        manager = StealthBrowserManager(browser)
        
        context = await manager.create_stealth_context(config)
        page = await manager.setup_page(context)
        
        print("\n--- EVASION AUDIT ---")
        
        is_webdriver = await page.evaluate("navigator.webdriver")
        print(f"[Audit] navigator.webdriver: {is_webdriver}")
        
        user_agent = await page.evaluate("navigator.userAgent")
        print(f"[Audit] User-Agent: {user_agent}")
        
        languages = await page.evaluate("navigator.languages")
        print(f"[Audit] Languages: {languages}")
        
        timezone = await page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
        print(f"[Audit] Timezone: {timezone}")
        
        plugins_len = await page.evaluate("navigator.plugins.length")
        print(f"[Audit] Plugins Length: {plugins_len}")
        
        webgl_vendor = await page.evaluate("""
            (() => {
                const canvas = document.createElement('canvas');
                const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
                if (!gl) return null;
                const debugInfo = gl.getExtension('WEBGL_debug_renderer_info');
                return debugInfo ? gl.getParameter(debugInfo.UNMASKED_VENDOR_WEBGL) : null;
            })();
        """)
        print(f"[Audit] WebGL Vendor: {webgl_vendor}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
