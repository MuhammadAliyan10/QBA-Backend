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
            max_wait = 600 # 10 minutes max for manual login
            waited = 0
            
            while waited < max_wait:
                # 1. Check if browser/page is closed
                if page.is_closed():
                    break
                    
                # 2. Capture current state
                current_state = await context.storage_state()
                
                # 3. Heuristic: Authentication Detection
                current_url = page.url.lower()
                cookies = current_state.get("cookies", [])
                
                # Indicator 1: Substantive cookie detection
                auth_indicators = [
                    "session", "auth", "login", "user", "token", "sid", "jwt", 
                    "logged_in", "xs", "c_user", "li_at", "atlassian", "okta"
                ]
                
                is_authenticated = False
                for cookie in cookies:
                    name = cookie["name"].lower()
                    val = cookie.get("value", "")
                    
                    if any(ind in name for ind in auth_indicators):
                        if len(val) > 20: 
                            is_authenticated = True
                            break
                    if len(val) > 100:
                        is_authenticated = True
                        break
                
                # Indicator 2: URL Navigation (Home/Dashboard patterns)
                # If we moved away from 'login' or 'signin' to a root or common landing path
                login_patterns = ["login", "signin", "auth", "signup"]
                landing_patterns = ["/home", "/dashboard", "/feed", "/account", "/overview", "/welcome"]
                
                # Check if we were on a login page and now we are on a landing page
                if not any(lp in current_url for lp in login_patterns):
                    if any(hp in current_url for hp in landing_patterns) or current_url.endswith(".com/") or current_url.endswith(".dev/"):
                        if len(cookies) > 5: # Basic check for some state
                            is_authenticated = True

                # 4. Update the 'best available' state
                if cookies:
                    session_state = current_state

                # 5. Exit if authenticated
                if is_authenticated:
                    rich.print("[bold green]✔ Authentication detected! Auto-capturing and closing...[/bold green]")
                    break
                
                await asyncio.sleep(1)
                waited += 1
                
        except Exception as e:
            # Handle unexpected errors (e.g. browser crash)
            pass
        finally:
            # Attempt one last capture if we don't have a solid state yet
            # and the browser is still alive
            if not is_authenticated:
                try:
                    if not page.is_closed():
                        session_state = await context.storage_state()
                except:
                    pass
            
            # Close browser gracefully
            try:
                await browser.close()
            except:
                pass

    if not session_state or not session_state.get("cookies"):
        raise Exception("Session capture failed. Browser closed or timed out before authentication.")

    # Success message
    if is_authenticated:
        rich.print("[bold green]✔ Session successfully captured and vaulted.[/bold green]")
    else:
        rich.print("[bold yellow]⚠ Captured session state upon manual closure (heuristic not triggered).[/bold yellow]")

    return session_state
