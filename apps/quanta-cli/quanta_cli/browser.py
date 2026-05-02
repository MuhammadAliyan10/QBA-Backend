import asyncio
import rich
from typing import Dict, Any
from playwright.async_api import async_playwright

async def launch_auth_browser(url: str) -> Dict[str, Any]:
    """
    Launches a headful browser, waits for the user to close it,
    and extracts the storage state right before it dies.
    """
    rich.print(f"[bold cyan]Launching secure BYOS container for:[/bold cyan] {url}")
    rich.print("[bold yellow]INSTRUCTIONS:[/bold yellow]")
    rich.print("1. Authenticate into the target website.")
    rich.print("2. [bold red]Close the browser window[/bold red] when you are finished.")
    rich.print("Waiting for browser to be closed...\n")

    session_state = {}
    
    async with async_playwright() as p:
        # Launch headful native Chrome to bypass macOS Gatekeeper blocks
        browser = await p.chromium.launch(headless=False, channel="chrome")
        context = await browser.new_context()
        page = await context.new_page()

        # Handle Tear-Down Race Condition
        # We need to extract the state BEFORE the context is fully destroyed.
        # Playwright's page.on("close") is synchronous and doesn't easily allow awaiting storage_state().
        # Instead, we will poll page.is_closed() in a loop, but also hook into the close event 
        # to trigger the extraction immediately if possible.
        
        extraction_done = asyncio.Event()

        async def _extract_state():
            nonlocal session_state
            if not session_state: # Only extract once
                try:
                    # Explicitly extract to memory (zero-trace), no path argument
                    session_state = await context.storage_state()
                except Exception as e:
                    rich.print(f"[dim]State extraction note: {e}[/dim]")
            extraction_done.set()

        # Hook the close event to attempt extraction before it's too late
        page.on("close", lambda p: asyncio.create_task(_extract_state()))

        try:
            await page.goto(url)
            
            # Wait for the user to close the page
            while not page.is_closed():
                await asyncio.sleep(0.5)
                
            # Ensure extraction completed via the event hook
            await asyncio.wait_for(extraction_done.wait(), timeout=5.0)

        except Exception as e:
            # If the user closed the entire browser instead of just the page, 
            # we might get an error here. We should still try to extract if we haven't.
            if not session_state:
                 try:
                     session_state = await context.storage_state()
                 except:
                     pass
        finally:
            if browser.is_connected():
                await browser.close()

    if not session_state:
        raise Exception("Failed to extract session state. The browser may have closed too abruptly.")

    return session_state
