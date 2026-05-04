import asyncio
import rich
from typing import Dict, Any
from playwright.async_api import async_playwright

async def launch_auth_browser(url: str) -> Dict[str, Any]:
    """
    Launches a headful browser, monitors for authenticated session state,
    and auto-closes the browser once a session is detected.
    """
    rich.print(f"[bold cyan]Launching secure BYOS container for:[/bold cyan] {url}")
    rich.print("[bold yellow]INSTRUCTIONS:[/bold yellow]")
    rich.print("1. Authenticate into the target website.")
    rich.print("2. [bold green]The system will auto-detect your login[/bold green] and close the browser.")
    rich.print("Monitoring for session capture...\n")

    session_state = {}
    
    async with async_playwright() as p:
        # Launch headful native Chrome
        browser = await p.chromium.launch(headless=False, channel="chrome")
        context = await browser.new_context()
        page = await context.new_page()

        try:
            await page.goto(url)
            
            # Poll for session state
            max_wait = 300 # 5 minutes max
            waited = 0
            
            while waited < max_wait:
                if page.is_closed():
                    break
                    
                state = await context.storage_state()
                cookies = state.get("cookies", [])
                
                # Heuristic: If we have auth-looking cookies, we assume login success.
                auth_indicators = ["session", "auth", "login", "user", "token", "sid", "jwt", "logged_in"]
                is_authenticated = False
                
                if len(cookies) > 0:
                    for cookie in cookies:
                        name = cookie["name"].lower()
                        if any(ind in name for ind in auth_indicators):
                            # Ensure it's a substantive token, not just a CSRF stub
                            if len(cookie["value"]) > 16:
                                is_authenticated = True
                                break
                
                if is_authenticated:
                    rich.print("[bold green]✔ Session detected! Auto-capturing and closing...[/bold green]")
                    session_state = state
                    break
                
                await asyncio.sleep(1)
                waited += 1
                
        except Exception as e:
            # If the user closes the browser manually, capture whatever we have
            if not session_state:
                try:
                    session_state = await context.storage_state()
                except:
                    pass
        finally:
            if browser.is_connected():
                await browser.close()

    if not session_state:
        raise Exception("Session capture failed. Browser closed or timed out before authentication.")

    return session_state
