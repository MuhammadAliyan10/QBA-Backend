import asyncio
import rich
import sys
from typing import Dict, Any, Optional
from playwright.async_api import async_playwright
import tempfile
from urllib.parse import urlparse

async def launch_auth_browser(url: str, alias: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Launches a headful browser with a strict ephemeral context, monitors for 
    domain-specific authenticated state using optimized cookie-only polling,
    and aborts if no valid session is detected upon closure.
    """
    rich.print(f"[bold cyan]Launching secure isolated container for:[/bold cyan] {url}")
    if alias:
        rich.print(f"[dim]Vault Alias: {alias}[/dim]")
    rich.print("[bold yellow]INSTRUCTIONS:[/bold yellow]")
    rich.print("1. Authenticate into the target website.")
    rich.print("2. [bold green]The system will auto-detect your login[/bold green] and close the browser.")
    rich.print("Monitoring for session capture...\n")

    # Domain -> Specific Auth Cookie mapping
    AUTH_MARKERS = {
        "facebook.com": "c_user",
        "linkedin.com": "li_at",
        "github.com": "user_session"
    }

    # Strict URL Parsing: Extract base hostname
    parsed_url = urlparse(url)
    hostname = parsed_url.hostname or ""
    if hostname.startswith("www."):
        hostname = hostname[4:]
    
    # Identify target marker
    target_marker = AUTH_MARKERS.get(hostname)
    
    if target_marker:
        rich.print(f"[dim]Detection Strategy: Waiting for strict marker '{target_marker}' on {hostname}...[/dim]")
    else:
        rich.print(f"[bold yellow]⚠ Unknown domain ({hostname}). Auto-detection disabled. Please close browser manually after login.[/bold yellow]")

    session_state = {}
    is_authenticated = False
    
    async with async_playwright() as p:
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=temp_dir,
                    headless=False,
                    channel="chrome",
                    no_viewport=True
                )
                page = context.pages[0] if context.pages else await context.new_page()

                await page.goto(url)
                
                # Poll for session state
                max_wait = 600 
                waited = 0
                
                while waited < max_wait:
                    # Optimized Exit Check: Ensure loop breaks if all tabs are closed
                    if len(context.pages) == 0:
                        break
                        
                    # Optimized Polling: Retrieve ONLY cookies to prevent CDP thrashing/flashing
                    cookies = await context.cookies()
                    
                    if target_marker:
                        for cookie in cookies:
                            if cookie["name"].lower() == target_marker:
                                val = cookie.get("value", "")
                                if len(val) > 20:
                                    is_authenticated = True
                                    rich.print(f"[bold green]✔ Strict Auth Marker Verified: {target_marker}[/bold green]")
                                    break
                    
                    if is_authenticated:
                        # Capture full storage state EXACTLY once upon successful detection
                        session_state = await context.storage_state()
                        break
                    
                    await asyncio.sleep(1)
                    waited += 1
                    
            except Exception as e:
                rich.print(f"[bold red]Browser Error:[/bold red] {e}")
            finally:
                # Cleanup and final capture attempt if manually closed
                try:
                    if not is_authenticated and len(context.pages) > 0:
                        session_state = await context.storage_state()
                    await context.close()
                except:
                    pass

    # --- ABORT GUARDRAIL ---
    if not is_authenticated:
        rich.print("[bold red]✖ Auth aborted. No valid session detected.[/bold red]")
        rich.print("[dim]The browser was closed before the required authentication marker was established.[/dim]")
        return None

    rich.print("[bold green]✔ Session successfully captured.[/bold green]")
    return session_state
