# backend/apps/execution-plane/w3_auth.py
import asyncio
from playwright.async_api import async_playwright

async def generate_vault():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        print("🚀 LAUNCHING VAULT GENERATOR...")
        await page.goto("https://profile.w3schools.com/log-in", wait_until="domcontentloaded")
        
        # Wait for the login form to physically appear before polling.
        # This prevents false-positive detection from pre-login redirects.
        try:
            await page.wait_for_selector("input[type='email'], input[name='email'], input[type='password']", timeout=15000)
        except Exception:
            pass  # Proceed to polling even if selector is slightly different
        
        print("⏳ Waiting for login... (browser will close automatically after you sign in)")
        
        # Snapshot the initial URL to detect navigation away from it
        login_url_fragment = "log-in"
        
        while True:
            current_url = page.url
            
            # Condition 1: URL no longer contains the login path
            left_login_page = login_url_fragment not in current_url
            
            # Condition 2: Check for authenticated W3Schools cookies
            cookies = await context.cookies()
            has_session_cookie = any(
                ("session" in c.get("name", "").lower() or "token" in c.get("name", "").lower() or "user" in c.get("name", "").lower())
                and "w3schools" in c.get("domain", "")
                for c in cookies
            )
            
            if left_login_page and has_session_cookie:
                break
            
            await asyncio.sleep(1)

        # Capture the Stealth Vault
        await context.storage_state(path="w3_vault.json")
        print("✅ SESSION CAPTURED: w3_vault.json created.")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(generate_vault())
