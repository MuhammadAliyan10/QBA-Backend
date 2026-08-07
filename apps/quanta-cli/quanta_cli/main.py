import typer
import asyncio
import rich
import sys
import json as _json
from typing import Optional

from .browser import launch_auth_browser
from .api import upload_vault_session, get_api_key, save_api_key, execute_mission, stream_mission_logs, list_jobs, get_job

__version__ = "0.2.0"

app = typer.Typer(
    name="quanta",
    help="Quanta Developer CLI - Secure BYOS Generator",
    no_args_is_help=True
)

config_app = typer.Typer(help="Manage local Quanta CLI configuration.")
app.add_typer(config_app, name="config")

jobs_app = typer.Typer(help="Inspect and manage execution jobs.")
app.add_typer(jobs_app, name="jobs")


def _version_callback(value: bool) -> None:
    if value:
        rich.print(f"[bold cyan]quanta[/bold cyan] version [bold white]{__version__}[/bold white]")
        raise typer.Exit()


@app.callback()
def callback(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-V",
        help="Show CLI version and exit.",
        callback=_version_callback,
        is_eager=True,
    )
):
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


@jobs_app.command("list")
def jobs_list(
    status: Optional[str] = typer.Option(None, "--status", "-s", help="Filter by status: RUNNING, COMPLETED, FAILED, QUEUED."),
    limit: int = typer.Option(50, "--limit", "-n", help="Maximum number of jobs to show."),
):
    """
    List recent execution jobs for your account.
    """
    api_key = get_api_key()
    if not api_key:
        rich.print("[bold red]Error:[/bold red] QUANTA_API_KEY not found. Run [bold cyan]quanta config set-key <KEY>[/bold cyan] first.")
        raise typer.Exit(code=1)

    try:
        jobs = asyncio.run(list_jobs(limit=limit, status=status))
    except Exception as e:
        rich.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)

    if not jobs:
        rich.print("[dim]No jobs found.[/dim]")
        return

    from rich.table import Table
    from rich.console import Console

    console = Console()
    table = Table(title=f"Jobs (showing {len(jobs)})", show_header=True, header_style="bold magenta")
    table.add_column("ID", style="cyan", no_wrap=True, max_width=36)
    table.add_column("Status", style="bold", max_width=12)
    table.add_column("Duration (s)", justify="right", max_width=12)
    table.add_column("Credits", justify="right", max_width=8)
    table.add_column("Error", style="dim red", max_width=40)

    STATUS_COLORS = {
        "COMPLETED": "[bold green]COMPLETED[/bold green]",
        "FAILED": "[bold red]FAILED[/bold red]",
        "RUNNING": "[bold yellow]RUNNING[/bold yellow]",
        "QUEUED": "[dim]QUEUED[/dim]",
        "CANCELLED": "[dim]CANCELLED[/dim]",
    }

    for j in jobs:
        status_str = j.get("status", "UNKNOWN")
        colored_status = STATUS_COLORS.get(status_str, status_str)
        duration = j.get("duration", 0)
        duration_str = f"{duration:.1f}" if duration else "-"
        error = (j.get("error") or "")[:40]
        table.add_row(
            j.get("id", "-"),
            colored_status,
            duration_str,
            str(j.get("totalCost", "-")),
            error,
        )

    console.print(table)


@jobs_app.command("get")
def jobs_get(
    job_id: str = typer.Argument(..., help="The Job ID to inspect."),
    show_data: bool = typer.Option(False, "--data", "-d", help="Print extracted data as JSON."),
):
    """
    Fetch details and extracted data for a specific job.
    """
    api_key = get_api_key()
    if not api_key:
        rich.print("[bold red]Error:[/bold red] QUANTA_API_KEY not found. Run [bold cyan]quanta config set-key <KEY>[/bold cyan] first.")
        raise typer.Exit(code=1)

    try:
        job = asyncio.run(get_job(job_id))
    except Exception as e:
        rich.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)

    status = job.get("status", "UNKNOWN")
    STATUS_COLORS = {
        "COMPLETED": "bold green",
        "FAILED": "bold red",
        "RUNNING": "bold yellow",
        "QUEUED": "dim",
        "CANCELLED": "dim",
    }
    color = STATUS_COLORS.get(status, "white")

    rich.print(f"\n[bold]Job:[/bold] [cyan]{job_id}[/cyan]")
    rich.print(f"[bold]Status:[/bold] [{color}]{status}[/{color}]")
    if job.get("workflowId"):
        rich.print(f"[bold]Workflow:[/bold] {job['workflowId']}")
    if job.get("duration"):
        rich.print(f"[bold]Duration:[/bold] {job['duration']:.1f}s")
    if job.get("creditsUsed") is not None:
        rich.print(f"[bold]Credits Used:[/bold] {job['creditsUsed']}")
    if job.get("errorMessage"):
        rich.print(f"[bold red]Error:[/bold red] {job['errorMessage']}")
    if job.get("resultUrl"):
        rich.print(f"[bold]Result URL:[/bold] [link={job['resultUrl']}]{job['resultUrl']}[/link]")

    extracted = job.get("extracted_data")
    if extracted:
        rich.print(f"\n[bold magenta]Extracted Data:[/bold magenta]")
        if show_data or True:  # Always show data for get command
            rich.print(f"[white]{_json.dumps(extracted, indent=2)}[/white]")


@app.command()
def auth(
    url: str,
    alias: Optional[str] = typer.Option(None, "--alias", help="An optional name for this session (e.g., 'Work Account')."),
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
    prompt: str = typer.Option(..., "--prompt", help="The natural language mission prompt."),
    file_path: Optional[str] = typer.Option(None, "--file", help="Path to a local file (e.g. PDF) to attach to the mission. Max 1.5MB."),
    extraction_schema: Optional[str] = typer.Option(None, "--extraction-schema", help="JSON string for extraction schema.")
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
            job_id = await execute_mission(url, prompt, vault_id, file_path, extraction_schema)
            
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
                        inline_data = event.get("extracted_data")
                        if inline_data:
                            console.print(f"\n[bold green][OK] Mission Completed Successfully.[/bold green]")
                            console.print(f"\n[bold magenta]Extracted Data:[/bold magenta]")
                            if isinstance(inline_data, str):
                                try:
                                    inline_data = _json.loads(inline_data)
                                except Exception:
                                    pass
                            console.print(f"[white]{_json.dumps(inline_data, indent=2)}[/white]")
                            return

                        console.print(f"\n[bold green][OK] Mission Completed Successfully.[/bold green]")
                        # Fetch final job details to print extracted data (Fallback)
                        try:
                            import os, urllib.request
                            job_data = await get_job(job_id)
                            extracted = job_data.get("extracted_data")
                            if extracted:
                                if isinstance(extracted, dict) and "artifact_url" in extracted:
                                    artifact_url = extracted["artifact_url"]
                                    dl_dir = os.path.join(os.getcwd(), "downloads")
                                    os.makedirs(dl_dir, exist_ok=True)
                                    out_path = os.path.join(dl_dir, f"quanta_artifact_{job_id[:8]}.csv")
                                    
                                    console.print(f"[bold cyan]Downloading artifact from secure storage...[/bold cyan]")
                                    urllib.request.urlretrieve(artifact_url, out_path)
                                    console.print(f"[bold green]Artifact Downloaded:[/bold green] {out_path}")
                                else:
                                    console.print(f"\n[bold magenta]Extracted Data:[/bold magenta]")
                                    console.print(f"[white]{_json.dumps(extracted, indent=2)}[/white]")
                        except Exception:
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
    prompt: str = typer.Option(..., "--prompt", help="The natural language mission prompt."),
    file_path: Optional[str] = typer.Option(None, "--file", help="Path to a local file (e.g. PDF) to attach to the mission. Max 1.5MB."),
    extraction_schema: Optional[str] = typer.Option(None, "--extraction-schema", help="JSON string for extraction schema.")
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
            job_id = await execute_mission(url, prompt, vault_id, file_path, extraction_schema)
            
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
                        inline_data = event.get("extracted_data")
                        if inline_data:
                            console.print(f"\n[bold green][OK] Mission Completed Successfully.[/bold green]")
                            console.print(f"\n[bold magenta]Extracted Data:[/bold magenta]")
                            import json
                            if isinstance(inline_data, str):
                                try:
                                    inline_data = json.loads(inline_data)
                                except Exception:
                                    pass
                            console.print(f"[white]{json.dumps(inline_data, indent=2)}[/white]")
                            return

                        console.print(f"\n[bold green][OK] Mission Completed Successfully.[/bold green]")
                        # Fetch final job details to print extracted data (Fallback)
                        try:
                            import httpx, os, urllib.request
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
                                        if isinstance(extracted, dict) and "artifact_url" in extracted:
                                            artifact_url = extracted["artifact_url"]
                                            dl_dir = os.path.join(os.getcwd(), "downloads")
                                            os.makedirs(dl_dir, exist_ok=True)
                                            out_path = os.path.join(dl_dir, f"quanta_artifact_{job_id[:8]}.csv")
                                            
                                            console.print(f"[bold cyan]Downloading artifact from secure storage...[/bold cyan]")
                                            urllib.request.urlretrieve(artifact_url, out_path)
                                            console.print(f"[bold green]Artifact Downloaded:[/bold green] {out_path}")
                                        else:
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
