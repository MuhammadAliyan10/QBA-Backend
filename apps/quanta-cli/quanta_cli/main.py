import typer
import asyncio
import rich
import sys
from typing import Optional

from .browser import launch_auth_browser
from .api import upload_vault_session, get_api_key, save_api_key, execute_mission, stream_mission_logs

app = typer.Typer(
    name="quanta",
    help="Quanta Developer CLI - Secure BYOS Generator",
    no_args_is_help=True
)

config_app = typer.Typer(help="Manage local Quanta CLI configuration.")
app.add_typer(config_app, name="config")

@app.callback()
def callback():
    """
    Quanta CLI Toolkit
    """
    pass

@config_app.command("set-key")
def set_key(api_key: str):
    """
    Securely save your Quanta API Key to the local config file.
    """
    try:
        path = save_api_key(api_key)
        rich.print(f"[bold green]Success![/bold green] API Key saved to [white]{path}[/white]")
    except Exception as e:
        rich.print(f"[bold red]Error:[/bold red] Failed to save config: {e}")
        raise typer.Exit(code=1)

@app.command()
def auth(
    url: str,
    alias: Optional[str] = typer.Option(None, "--alias", help="An optional name for this session (e.g., 'Work Account').")
):
    """
    Launch a secure local browser to log into a target portal and upload the 
    authenticated session state directly to the Quanta Vault.
    """
    try:
        api_key = get_api_key()
        if not api_key:
             rich.print("[bold red]Error:[/bold red] QUANTA_API_KEY not found. Run [bold cyan]quanta config set-key <KEY>[/bold cyan] first.")
             raise typer.Exit(code=1)

        # 1. Local Generation (Browser)
        session_state = asyncio.run(launch_auth_browser(url, alias=alias))

        if session_state is None:
            # launch_auth_browser handles the error message
            raise typer.Exit(code=0)

        # 2. Vault Upload (API)
        rich.print("\n[bold cyan]Uploading session state to Quanta Vault...[/bold cyan]")
        vault_id = asyncio.run(upload_vault_session(url, session_state, alias=alias))

        # 3. Output
        rich.print(f"\n[bold green][OK] Session Vaulted Successfully![/bold green]")
        rich.print(f"Vault ID: [bold cyan]{vault_id}[/bold cyan]")
        if alias:
            rich.print(f"Alias: [white]{alias}[/white]")
        rich.print(f"You can now use this ID for agentic execution missions.")

    except Exception as e:
        rich.print(f"\n[bold red]Operation Failed:[/bold red] {e}")
        raise typer.Exit(code=1)

@app.command()
def execute(
    url: str, 
    vault_id: Optional[str] = typer.Option(None, "--vault-id", help="The Credential ID from the Quanta Vault."),
    prompt: str = typer.Option(..., "--prompt", help="The natural language mission prompt.")
):
    """
    Trigger an agentic execution mission using a vaulted session.
    """
    api_key = get_api_key()
    if not api_key:
        rich.print("[bold red]Error:[/bold red] QUANTA_API_KEY not found. Run [bold cyan]quanta config set-key <KEY>[/bold cyan] first.")
        raise typer.Exit(code=1)

    async def run_and_stream():
        def clean_str(s: str) -> str:
            if not s:
                return ""
            s = s.replace("→", "->").replace("✔", "[OK]").replace("✘", "[FAIL]")
            try:
                s.encode(sys.stdout.encoding or 'utf-8')
            except UnicodeEncodeError:
                s = s.encode('ascii', errors='replace').decode('ascii')
            return s

        try:
            rich.print(f"[bold cyan]Triggering execution mission...[/bold cyan]")
            job_id = await execute_mission(url, prompt, vault_id)
            
            rich.print(f"[bold green]Mission Dispatched:[/bold green] Job ID: [bold white]{job_id}[/bold white]")
            rich.print(f"[bold cyan]Attaching to live log stream...[/bold cyan]\n")
            
            from rich.console import Console
            from rich.theme import Theme
            
            console = Console(theme=Theme({"info": "dim cyan", "error": "bold red", "success": "bold green"}))
            
            async for event in stream_mission_logs(job_id):
                # Event format from backend: {type: "LOG"|"NODE_STATUS"|"WORKFLOW_STATUS", message, nodeId, status, ...}
                event_type = event.get("type")
                message = clean_str(event.get("message", ""))
                status = event.get("status", "").upper()
                
                if event_type == "LOG":
                    level = event.get("level", "info")
                    data = event.get("data")
                    if data:
                        data = clean_str(str(data))
                    
                    console.print(f"[{level}]{message}[/{level}]")
                    if data:
                        console.print(f"  [cyan]Data:[/cyan] {data}")
                elif event_type == "WORKFLOW_STATUS":
                    if status in ["SUCCESS", "COMPLETED"]:
                        console.print(f"\n[bold green][OK] Mission Completed Successfully.[/bold green]")
                        # Fetch final job details to print extracted data
                        try:
                            import httpx, os
                            from .api import get_api_key
                            api_url = os.getenv("QUANTA_API_URL", "http://localhost:8080").rstrip("/")
                            api_key = get_api_key()
                            headers = {"Authorization": f"Bearer {api_key}"}
                            
                            with httpx.Client() as client:
                                r = client.get(f"{api_url}/v1/jobs/{job_id}", headers=headers)
                                if r.status_code == 200:
                                    job_data = r.json()
                                    extracted = job_data.get("extracted_data")
                                    if extracted:
                                        console.print(f"\n[bold magenta]Extracted Data:[/bold magenta]")
                                        import json
                                        console.print(f"[white]{json.dumps(extracted, indent=2)}[/white]")
                        except Exception as fetch_err:
                            pass
                        return
                    elif status == "FAILED":
                        console.print(f"\n[bold red][FAIL] Mission Failed:[/bold red] {message}")
                        return
        except Exception as e:
            rich.print(f"\n[bold red]Execution Failed:[/bold red] {clean_str(str(e))}")
            sys.exit(1)

    asyncio.run(run_and_stream())

if __name__ == "__main__":
    app()
