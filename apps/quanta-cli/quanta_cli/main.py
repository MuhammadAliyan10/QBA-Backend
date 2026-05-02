import typer
import asyncio
import rich
import sys

from .browser import launch_auth_browser
from .api import upload_session, get_api_key

app = typer.Typer(
    name="quanta",
    help="Quanta Developer CLI - Secure BYOS Generator",
    no_args_is_help=True
)

@app.callback()
def callback():
    """
    Quanta CLI Toolkit
    """
    pass

@app.command()
def auth(url: str):
    """
    Launch a secure local browser to log into a target portal and upload the 
    authenticated session state directly to the Quanta Vault.
    """
    try:
        api_key = get_api_key()
        if not api_key:
             rich.print("[bold red]Error:[/bold red] QUANTA_API_KEY not found in environment or ~/.quanta/config.json")
             raise typer.Exit(code=1)

        # 1. Local Generation (Browser)
        session_state = asyncio.run(launch_auth_browser(url))

        # 2. Vault Upload (API)
        rich.print("\n[bold cyan]Uploading session state to Quanta Vault...[/bold cyan]")
        credential_id = asyncio.run(upload_session(url, session_state))

        # 3. Output
        rich.print(f"\n[bold green]Success! Session successfully vaulted.[/bold green]")
        rich.print(f"Credential ID: [bold white]{credential_id}[/bold white]")
        rich.print(f"You can now pass this ID to the Quanta Execution Plane API.")

    except Exception as e:
        rich.print(f"\n[bold red]Operation Failed:[/bold red] {e}")
        raise typer.Exit(code=1)

if __name__ == "__main__":
    app()
