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
                    ignore_default_args=["--enable-automation"],
                    viewport={"width": 1280, "height": 720}
                )

                async def on_page(page):
                    rich.print(f"[cyan]DEBUG: New tab spawned: {page.url}[/cyan]")
                    page.on("console", lambda msg: rich.print(f"[dim]Tab Console: {msg.text}[/dim]"))
                    page.on("pageerror", lambda err: rich.print(f"[red]Tab Error: {err}[/red]"))
                    page.on("close", lambda p: rich.print(f"[yellow]DEBUG: Tab closed: {p.url}[/yellow]"))

                context.on("page", on_page)

                # Stealth Injection: Bypass enterprise anti-bot telemetry
                await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    window.navigator.chrome = { runtime: {} };
                    const originalQuery = window.navigator.permissions.query;
                    window.navigator.permissions.query = (parameters) => (
                        parameters.name === 'notifications' ?
                            Promise.resolve({ state: Notification.permission }) :
                            originalQuery(parameters)
                    );
                    
                    // Scrub Playwright CDP variables
                    for (let prop in window) {
                        if (prop.includes('cdc_')) {
                            delete window[prop];
                        }
                    }
                """)
                page = context.pages[0] if context.pages else await context.new_page()

                try:
                    # Use domcontentloaded to avoid waiting for heavy analytics scripts during SSO
                    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    # SSO redirect chains frequently abort initial navigations. 
                    # Do not crash. The browser is open, let the user handle it.
                    rich.print(f"[dim]Navigation warning (expected during SSO): {e}[/dim]")
                
                # Poll for session state
                max_wait = 600 
                waited = 0
                
                while waited < max_wait:
                    try:
                        if len(context.pages) == 0:
                            if not target_marker:
                                is_authenticated = True
                            break
                    except Exception:
                        pass

                    if not target_marker:
                        try:
                            # For unknown domains, continuously capture state.
                            session_state = await context.storage_state()
                            is_authenticated = True
                        except Exception:
                            # Ignore navigation/DOM errors (including Target closed during cross-origin redirects)
                            pass
                    else:
                        try:
                            cookies = await context.cookies()
                            for cookie in cookies:
                                if cookie["name"].lower() == target_marker:
                                    val = cookie.get("value", "")
                                    if len(val) > 20:
                                        is_authenticated = True
                                        rich.print(f"[bold green]✔ Strict Auth Marker Verified: {target_marker}[/bold green]")
                                        break
                                        
                            if is_authenticated:
                                session_state = await context.storage_state()
                                break
                        except Exception:
                            pass

                    await asyncio.sleep(1)
                    waited += 1
                    
            except Exception as e:
                if "Target page, context or browser has been closed" not in str(e):
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
