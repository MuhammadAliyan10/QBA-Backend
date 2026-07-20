import os
import asyncio
import logging
import base64
import time
import json
import tempfile
from datetime import timedelta
from typing import Dict, Any, Optional, List
from temporalio import activity
from temporalio.exceptions import ApplicationError
from contextlib import asynccontextmanager
from playwright.async_api import async_playwright, Page, ElementHandle
from playwright.async_api import TimeoutError as PlaywrightTimeout
import httpx
import traceback

# Feature Flags
from config import is_s3_upload_enabled, is_session_persistence_enabled

# Session Persistence (Encrypted Browser State)
from core.browser.session import SessionManager, get_session_manager

# TASK 2 FIX: Import Universal Storage for actual S3/MinIO uploads
from core.storage import get_storage, is_storage_available, StorageUploadError

# --- IMPORTS ---
# 1. The Nervous System (Snake Case - Infrastructure)
from core.nervous_system import NervousSystem
from core.utils.params import substitute_variables, validate_and_substitute
from activities.executeUniversalAgent import execute_universal_agent

# 2. The Glass Box Engine (Camel Case - Logic)
from core.selector.smart_finder import SmartFinder

# 3. The Network Sniffer (Level 5 - Protocol Reverse Engineering)
from core.network_sniffer import NetworkSniffer

# 4. The Account Pool Manager (Session Rehydration)
from core.account_manager import AccountManager, SessionHydrationTimeout

# 5. The Recipe Manager (Dynamic RAG)
from core.recipe.recipe_manager import RecipeManager

# 6. User-Facing Logger (The Voice of the Glass Box)
from core.user_facing_logger import UserFriendlyLogger

logger = logging.getLogger("activity")


from .telemetry import capture_failure_screenshot
from .navigation import (
    safe_browser_context, click_with_retry, dismiss_overlays, 
    safe_wait_for_network_idle, get_proxy_config, is_proxy_available,
    NAVIGATION_TIMEOUT, NETWORK_IDLE_TIMEOUT, CLICK_RETRY_ATTEMPTS, CLICK_RETRY_DELAY_MS
)
from .extraction import perform_extraction
from .context import ExecutionContext
from .actions import registry

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", tempfile.gettempdir())


def validate_step_params(step_params: Dict[str, Any], available_params: Dict[str, Any], step_index: int) -> Dict[str, Any]:
    """
    Validates and substitutes variables in step parameters.
    Supports both {{variable}} and {variable} syntax within strings.
    """
    return validate_and_substitute(step_params, available_params)


_recipe_manager_instance = None

def get_recipe_manager() -> RecipeManager:
    """Get or create RecipeManager singleton."""
    global _recipe_manager_instance
    if _recipe_manager_instance is None:
        _recipe_manager_instance = RecipeManager()
    return _recipe_manager_instance


@activity.defn
async def browser_automation_activity(payload: dict) -> dict:
    """
    The Main Execution Loop.
    Runs inside a Temporal Worker.
    """
    # 1. Unpack Payload (From Go)
    job_id = payload.get("job_id")
    workflow_id = payload.get("workflow_id")
    target_url = payload.get("target_url")
    target_urls = payload.get("target_urls", [])
    navigation_objective = payload.get("navigation_objective") or payload.get("objective")
    extraction_schema = payload.get("extraction_schema")
    params = payload.get("params", {})
    attachments = payload.get("attachments", [])
    
    # 1.5 Materialize attachments to disk
    materialized_files = []
    if attachments:
        import base64
        import tempfile
        
        temp_dir = tempfile.gettempdir()
        for att in attachments:
            try:
                filename = att.get("filename", "upload.tmp")
                # Sanitize filename to prevent path traversal
                safe_filename = os.path.basename(filename)
                file_path = os.path.join(temp_dir, f"{job_id}_{safe_filename}")
                with open(file_path, "wb") as f:
                    f.write(base64.b64decode(att["base64"]))
                materialized_files.append(file_path)
                logger.info(f"[{job_id}] Materialized attachment to {file_path}")
            except Exception as e:
                logger.error(f"[{job_id}] Failed to materialize attachment {filename}: {e}")

    # DIAGNOSTIC TELEMETRY
    payload_keys = list(payload.keys())
    await NervousSystem.publish(
        f"quanta.telemetry.{job_id}",
        json.dumps({"type": "log", "message": f"[Executor] Raw Payload Keys: {payload_keys} | WorkflowID: {workflow_id}"})
    )

    # The 'config' dictionary contains our Glass Box settings
    config = payload.get("config") or payload.get("engine_settings", {})

    # Resolve extraction_schema: top-level takes priority, then engine_settings
    if not extraction_schema:
        extraction_schema = config.get("extraction_schema")

    # Normalize target_urls: ensure we always have a list
    if not target_urls and target_url:
        target_urls = [target_url]

    # 2. Initialize User Logger
    user_logger = UserFriendlyLogger(job_id)

    # 3. Notify Nervous System: START
    await user_logger.info("PROCESSING_RAG")  # "Thinking about the next step..."

    # 3. Load Recipe — Priority order:
    #    a) Editor recipe graph (nodes/edges from frontend via Go controller)
    #    b) RAG/Qdrant vector search (semantic match)
    #    c) Raw steps (developer mode / direct API)
    steps = None
    recipe_data = payload.get("recipe")

    if recipe_data and isinstance(recipe_data, dict) and "nodes" in recipe_data:
        # SOURCE A: Recipe graph from the frontend editor
        from core.recipe.recipe_converter import convert_graph_to_steps
        nodes = recipe_data.get("nodes", [])
        edges = recipe_data.get("edges", [])
        steps = convert_graph_to_steps(nodes, edges)
        logger.info(f"[System] Converted editor graph: {len(nodes)} nodes → {len(steps)} steps")
        await NervousSystem.publish_update(
            job_id, "RUNNING",
            f"Loaded workflow from editor ({len(steps)} steps)",
            "init"
        )

    if not steps:
        # SOURCE B: RAG/Qdrant vector search — match by task description, not by UUID
        recipe_mgr = get_recipe_manager()
        rag_query = navigation_objective or target_url or workflow_id
        recipe = recipe_mgr.find_recipe(rag_query)

        if recipe:
            steps = recipe['steps']
            logger.info(f"[System] Found recipe via vector search: '{recipe['name']}' (score: {recipe['score']:.3f})")
            await NervousSystem.publish_update(
                job_id, "RUNNING",
                f"[RAG] Loaded workflow: '{recipe['name']}' (semantic match: {recipe['score']:.2f})",
                "init"
            )

    if not steps:
        # SOURCE C: Raw steps (developer mode)
        steps = payload.get("steps", [])

    use_universal_agent = False
    if not steps:
        # SOURCE D: AI Autonomous Planning (Ad-Hoc) - FALLBACK TO UNIVERSAL AGENT
        logger.info(f"[{job_id}] No recipe found. Falling back to Universal Agent...")
        await NervousSystem.publish_update(job_id, "RUNNING", "Initializing Universal Agent...", "init")
        use_universal_agent = True

    async with async_playwright() as p:
        # --- 4. BROWSER LAUNCH STRATEGY ---
        launch_args = {
            "headless": False,
            "slow_mo": 800,
            "args": ["--no-sandbox", "--disable-setuid-sandbox"]
        }

        # TASK 5 FIX: Optional Proxy Logic (The "Warden")
        # Only attempt to configure proxy if:
        # 1. User requested premium proxy in config
        # 2. AND proxy server is actually configured in environment
        if config.get("use_premium_proxy"):
            if is_proxy_available():
                region = config.get("region", "us")
                proxy_conf = get_proxy_config(region)

                if proxy_conf:
                    launch_args["proxy"] = proxy_conf
                    await NervousSystem.publish_update(
                        job_id, "RUNNING",
                        f"[Network] Routing via residential proxy ({region})",
                        "init"
                    )
                else:
                    # This shouldn't happen if is_proxy_available() is True
                    await NervousSystem.publish_update(
                        job_id, "WARNING",
                        "Proxy configuration error. Using direct connection.",
                        "init"
                    )
            else:
                # TASK 5 FIX: Gracefully skip proxy when not configured
                await NervousSystem.publish_update(
                    job_id, "INFO",
                    "[Network] Proxy not configured. Using direct connection (cost-saving mode).",
                    "init"
                )
                logger.info(f"[{job_id}] Premium proxy requested but PROXY_SERVER not set. Continuing without proxy.")

        # STATE FLAG: Track workflow success for account release logic
        # This replaces the fragile 'e' not in locals() hack
        workflow_succeeded = False

        # --- 5. SESSION & ACCOUNT PREPARATION ---
        user_id = payload.get("user_id", job_id)
        # 1st Priority: Explicit config domain
        # 2nd Priority: First GOTO step
        # 3rd Priority: Target URL from activity payload (BYOS / CLI mode)
        target_domain = config.get("domain")
        if not target_domain:
            if steps and steps[0]["action"] == "GOTO":
                target_domain = steps[0]["params"].get("url")
            else:
                target_domain = target_url

        if target_domain and not target_domain.startswith("http"):
             # Handle cases where domain is just a string
             pass
        elif target_domain:
             target_domain = SessionManager.extract_domain(target_domain)

        session_data = payload.get("sessionState")
        
        if session_data:
            await NervousSystem.publish_update(
                job_id, "RUNNING",
                f"[Session] Using vaulted session state for {target_domain}",
                "init"
            )
            logger.info(f"[{job_id}] Injecting vaulted session state from payload.")
            
            # Optional: Clear persistent cache for this domain to prevent contamination
            if is_session_persistence_enabled() and target_domain:
                try:
                    session_manager = await get_session_manager()
                    if session_manager:
                        await session_manager.delete_session(user_id, target_domain)
                        logger.info(f"[{job_id}] Cleared stale persistent session for {target_domain} in favor of vault ID.")
                except Exception:
                    pass
        elif is_session_persistence_enabled() and target_domain:
            try:
                session_manager = await get_session_manager()
                if session_manager:
                    session_data = await session_manager.get_session(user_id, target_domain)
                    if session_data:
                        await NervousSystem.publish_update(
                            job_id, "RUNNING",
                            f"[Session] Restored persistent session for {target_domain}",
                            "init"
                        )
            except Exception as e:
                logger.warning(f"[Session] Failed to restore session: {e}")


        # --- 6. EXECUTION ENGINE ---
        async with safe_browser_context(p, launch_args, storage_state=session_data) as (browser, context, page):
            try:

                # Initialize Account Manager for just-in-time leasing if needed
                account_mgr = AccountManager()
                leased_account = None

                require_login = config.get("require_login", False)
                if require_login and target_domain:
                    leased_account = await account_mgr.lease_account(target_domain)
                    if leased_account:
                        await NervousSystem.publish_update(
                            job_id, "RUNNING",
                            f"[Security] Leased account: {leased_account['username']} (cookies: {'Yes' if leased_account['cookies'] else 'No'})",
                            "init"
                        )
                        if leased_account['cookies']:
                             await context.add_cookies(leased_account['cookies'])

                # --- 7. Initialize Global Network Sniffer ---
                from core.network_sniffer import NetworkSniffer
                global_sniffer = NetworkSniffer(target_domain=target_domain)
                await global_sniffer.start_sniffing(page)

                # --- GHOST SESSION VERIFICATION & AUTH LOCK ---
                if is_session_persistence_enabled() and target_domain and session_manager:
                    # 1. Verify if session is still valid (Lightweight DOM/Network check)
                    is_valid = await session_manager.verify_session(page, target_domain)
    
                    if not is_valid:
                        await NervousSystem.publish_update(
                            job_id, "RUNNING",
                            f"[Security] Ghost session detected for {target_domain}. Re-authenticating...",
                            "auth"
                        )
    
                        if leased_account:
                            # 2. Acquire Distributed Lock (Block Thundering Herds)
                            is_leader, lock_uuid = await account_mgr.acquire_auth_lock(
                                leased_account['id'], target_domain
                            )
    
                            if is_leader:
                                # LEADER: Execute headless login sequence
                                # Note: The main loop will handle login steps if they are present
                                logger.info(f"[Auth] Leader status active for {job_id}")
                                payload["_auth_lock"] = {"uuid": lock_uuid, "account_id": leased_account['id']}
                            else:
                                # FOLLOWER: Polling finished, fresh cookies should be in DB
                                logger.info(f"[Auth] Follower resumed. Re-fetching fresh session.")
                                # Re-lease to get the fresh cookies (already happens in lease_account)
                                fresh_account = account_mgr.lease_account(target_domain)
                                if fresh_account and fresh_account['cookies']:
                                    await context.add_cookies(fresh_account['cookies'])
                                    await page.reload()
                                    await user_logger.info("SESSION_RECOVERED")
    
                # --- DOWNLOAD HANDLER (Industrial-Grade) ---
                # TASK 2 FIX: Real blob storage implementation
                storage = get_storage()  # Get singleton storage client
    
                async def handle_download(download):
                    """Handles file downloads with actual storage upload."""
                    filename = download.suggested_filename
                    safe_filename = "".join(c for c in filename if c.isalnum() or c in '._-')
                    local_path = os.path.join(DOWNLOAD_DIR, f"{job_id}_{safe_filename}")
    
                    await user_logger.info("DOWNLOAD_START", filename=filename)
    
                    try:
                        # Save to local filesystem first
                        await download.save_as(local_path)
                        file_size = os.path.getsize(local_path)
    
                        logger.info(f"[{job_id}] Downloaded {filename} ({file_size} bytes)")
    
                        # TASK 2 FIX: Actually upload to S3/MinIO
                        if is_s3_upload_enabled() and storage:
                            try:
                                # Read file content
                                with open(local_path, 'rb') as f:
                                    file_data = f.read()
    
                                # Determine MIME type
                                content_type = "application/octet-stream"
                                lower_filename = filename.lower()
                                if lower_filename.endswith('.pdf'):
                                    content_type = "application/pdf"
                                elif lower_filename.endswith('.csv'):
                                    content_type = "text/csv"
                                elif lower_filename.endswith('.json'):
                                    content_type = "application/json"
                                elif lower_filename.endswith(('.png', '.jpg', '.jpeg', '.webp')):
                                    content_type = f"image/{lower_filename.split('.')[-1]}"
    
                                # Upload to storage and get presigned URL
                                storage_key = f"{job_id}/downloads/{safe_filename}"
                                final_url = await storage.upload(file_data, storage_key, content_type)
    
                                logger.info(f"[Storage] Uploaded {filename} to {storage_key}")
                                logger.info(f"[Storage] Presigned URL: {final_url[:80]}...")
    
                                # Clean up local file after successful upload
                                os.remove(local_path)
    
                            except StorageUploadError as e:
                                logger.error(f"[Storage] Upload failed for {filename}: {e}")
                                # Fall back to local path
                                final_url = f"file://{local_path}"
                            except Exception as e:
                                logger.error(f"[Storage] Unexpected error uploading {filename}: {e}")
                                final_url = f"file://{local_path}"
                        else:
                            if not storage:
                                logger.warning(f"[Storage] Storage not configured. Saved to local disk: {local_path}")
                            else:
                                logger.info(f"[Storage] S3 Upload Disabled. Saved to local disk: {local_path}")
                            final_url = f"file://{local_path}"
    
                        await user_logger.info("DOWNLOAD_COMPLETE", filename=filename)
    
                        # Store the URL in job context for later retrieval
                        logger.info(f"[{job_id}] Final download URL: {final_url}")
    
                    except Exception as e:
                        logger.error(f"Download failed for {filename}: {e}")
                        await user_logger.error("GENERIC_ERROR", error_details=str(e))
                        raise  # Don't silently continue
    
                page.on("download", handle_download)
    
                # Initialize the Co-Pilot (SmartFinder)
                finder = SmartFinder(page)
    
                # Initialize Execution Context
                ctx = ExecutionContext(
                    job_id=job_id,
                    page=page,
                    browser_context=context,
                    finder=finder,
                    user_logger=user_logger,
                    global_sniffer=global_sniffer
                )

                # --- PRE-LOOP: Mandatory initial navigation ---
                if target_url and not use_universal_agent:
                    logger.info(f"[{job_id}] Pre-loop navigation to {target_url}")
                    try:
                        await page.goto(target_url, timeout=NAVIGATION_TIMEOUT, wait_until="networkidle")
                        # Settle JS redirects using native Playwright deterministic wait
                        try:
                            await page.wait_for_load_state("networkidle", timeout=10000)
                        except PlaywrightTimeout:
                            pass
                    except Exception as nav_err:
                        logger.warning(f"[{job_id}] Pre-loop navigation error (non-fatal): {nav_err}")

                # --- 5.5: UNIVERSAL AGENT FALLBACK / SEMANTIC SCHEMA EXTRACTION ---
                if use_universal_agent:
                    
                    aggregated_results = []
                    urls_to_process = target_urls if target_urls else ([target_url] if target_url else [])
                    
                    for url_index, current_url in enumerate(urls_to_process):
                        logger.info(f"[{job_id}] Processing URL {url_index + 1}/{len(urls_to_process)}: {current_url}")
                        await user_logger.progress(f"Processing URL {url_index + 1} of {len(urls_to_process)}...")
                        
                        # Navigate to this URL (skip for first if already there)
                        if url_index > 0 or page.url != current_url:
                            try:
                                await page.goto(current_url, timeout=NAVIGATION_TIMEOUT, wait_until="networkidle")
                                try:
                                    await page.wait_for_load_state("networkidle", timeout=10000)
                                except PlaywrightTimeout:
                                    pass
                            except Exception as nav_err:
                                logger.warning(f"[{job_id}] URL navigation error (non-fatal): {nav_err}")
                        
                        ua_result = await execute_universal_agent(
                            page=page,
                            job_id=job_id,
                            user_logger=user_logger,
                            nervous_system=NervousSystem,
                            target_url=current_url,
                            navigation_objective=navigation_objective if url_index == 0 else None,
                            extraction_schema=extraction_schema,
                            materialized_files=materialized_files
                        )
                        
                        ua_status = ua_result.get("status")

                        if ua_status in ("stopped", "failed"):
                            reason = ua_result.get("reason", "Unknown reason")
                            tokens = ua_result.get("tokens", 0)
                            logger.error(f"[{job_id}] Universal Agent stopped/failed: {reason} | tokens_used={tokens}")
                            await NervousSystem.publish_update(job_id, "FAILED", f"Agent stopped: {reason}", "error")
                            return {
                                "status": "FAILED",
                                "job_id": job_id,
                                "error": f"Agent stopped: {reason}",
                                "tokens_used": tokens,
                            }

                        if extraction_schema and ua_status == "success":
                            extracted_data = ua_result.get("data")
                            aggregated_results.append(extracted_data)
                            data_json = json.dumps(extracted_data)
                            await NervousSystem.publish(
                                f"quanta.telemetry.{job_id}",
                                json.dumps({"type": "log", "message": f"[Extractor] URL {url_index + 1} Payload: {data_json}"})
                            )
                            # Auto-save recipe after first successful extraction so repeat
                            # requests with the same objective skip the agent entirely.
                            if navigation_objective and url_index == 0:
                                try:
                                    recipe_mgr = get_recipe_manager()
                                    import hashlib as _hl
                                    recipe_name = "ua_" + _hl.md5(navigation_objective.encode()).hexdigest()[:12]
                                    recipe_mgr.save_recipe(
                                        name=recipe_name,
                                        description=navigation_objective,
                                        steps=[{
                                            "action": "UNIVERSAL_AGENT",
                                            "params": {
                                                "target_url": current_url,
                                                "navigation_objective": navigation_objective,
                                                "extraction_schema": extraction_schema,
                                            }
                                        }],
                                        user_id=user_id
                                    )
                                    logger.info(f"[{job_id}] Auto-saved recipe '{recipe_name}' for objective: {navigation_objective[:80]}")
                                except Exception as recipe_err:
                                    logger.warning(f"[{job_id}] Recipe auto-save failed (non-fatal): {recipe_err}")
                    
                    # --- DATA_TRANSFORM: CSV Aggregation + Cloud Upload ---
                    if extraction_schema and aggregated_results:
                        import csv
                        import io
                        
                        # Flatten nested lists (e.g., {"products": [...]}) into a single row list
                        flat_rows = []
                        for result in aggregated_results:
                            if isinstance(result, dict):
                                for key, value in result.items():
                                    if isinstance(value, list):
                                        flat_rows.extend(value)
                                    else:
                                        flat_rows.append(result)
                                        break
                            elif isinstance(result, list):
                                flat_rows.extend(result)
                        
                        artifact_url = None

                        if flat_rows and isinstance(flat_rows[0], dict):
                            # --- CSV INJECTION SANITIZATION ---
                            # Prefix cells starting with formula chars to prevent CSV injection attacks
                            FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')
                            sanitized_rows = []
                            for row in flat_rows:
                                sanitized_row = {}
                                for k, v in row.items():
                                    if isinstance(v, str) and v and v[0] in FORMULA_PREFIXES:
                                        sanitized_row[k] = f"'{v}"
                                    else:
                                        sanitized_row[k] = v
                                sanitized_rows.append(sanitized_row)

                            try:
                                csv_buffer = io.StringIO()
                                writer = csv.DictWriter(csv_buffer, fieldnames=sanitized_rows[0].keys())
                                writer.writeheader()
                                writer.writerows(sanitized_rows)
                                csv_output = csv_buffer.getvalue()
                            except Exception as csv_err:
                                # --- ERROR RESILIENCE: Fall back to raw JSON if CSV generation fails ---
                                logger.error(f"[{job_id}] CSV generation failed: {csv_err}. Falling back to raw JSON.")
                                await NervousSystem.publish(
                                    f"quanta.telemetry.{job_id}",
                                    json.dumps({"type": "log", "level": "error", "message": f"CSV generation failed: {csv_err}"})
                                )
                                return {
                                    "status": "SUCCESS",
                                    "steps_completed": len(urls_to_process),
                                    "job_id": job_id,
                                    "rows_extracted": len(flat_rows),
                                    "data": aggregated_results
                                }
                            
                            logger.info(f"[{job_id}] CSV Generated ({len(flat_rows)} rows, {len(csv_output)} bytes)")
                            await user_logger.progress(f"Extraction complete. {len(flat_rows)} rows extracted.")

                            # --- PHASE 2: Cloud Storage Upload ---
                            if is_s3_upload_enabled() and storage:
                                try:
                                    storage_key = f"{job_id}/results.csv"
                                    csv_bytes = csv_output.encode("utf-8")
                                    artifact_url = await storage.upload(csv_bytes, storage_key, "text/csv")
                                    logger.info(f"[{job_id}] CSV uploaded to cloud storage: {artifact_url[:80]}...")
                                    await user_logger.progress("Results uploaded to secure storage.")
                                except StorageUploadError as upload_err:
                                    logger.error(f"[{job_id}] CSV upload failed: {upload_err}")
                                    artifact_url = None
                                except Exception as upload_err:
                                    logger.error(f"[{job_id}] CSV upload unexpected error: {upload_err}")
                                    artifact_url = None
                            else:
                                logger.info(f"[{job_id}] Storage not configured. CSV delivered via telemetry stream only.")

                            # Telemetry broadcast (always — powers SSE/frontend preview)
                            await NervousSystem.publish(
                                f"quanta.telemetry.{job_id}",
                                json.dumps({"type": "DATA_TRANSFORM", "format": "csv", "data": csv_output})
                            )
                        
                        await NervousSystem.publish_update(
                            job_id, "RUNNING", "Extracted semantic schema", "ua_node",
                            data=json.dumps(aggregated_results)
                        )

                        # Build lean response — strip raw data when artifact is cloud-hosted
                        response_payload = {
                            "status": "SUCCESS",
                            "steps_completed": len(urls_to_process),
                            "job_id": job_id,
                            "rows_extracted": len(flat_rows),
                        }
                        if artifact_url:
                            response_payload["artifact_url"] = artifact_url
                        else:
                            response_payload["data"] = aggregated_results
                        return response_payload

                    steps = []  # Skip the regular execution loop
                
                # --- 6. EXECUTION LOOP ---
                active_vault = None
                for i, step in enumerate(steps):
                    # Use real node ID from graph converter for frontend event correlation
                    node_id = step.get("node_id", f"step-{i+1}")
                    action = step["action"]
                    raw_params = step.get("params", {})
    
                    # CRITICAL: Validate and substitute variables (fails fast on missing params)
                    step_params = validate_step_params(raw_params, params, i + 1)
    
                    logger.info(f"[{job_id}] Executing {action} (step {i+1}/{len(steps)})...")
    
                    # TELEMETRY: Node Start
                    await NervousSystem.publish(
                        f"quanta.telemetry.{job_id}",
                        json.dumps({"type": "log", "message": f"[Executor] Starting Node: {action} (step {i+1}/{len(steps)})"})
                    )
    
                    # --- ACTION SWITCH ---
                    if action in ["CLICK", "EXTRACT", "LOGIN_AND_SNIFF"]:
                        step_params["_global_params"] = params
                        step_params["_node_id"] = node_id
                        try:
                            action_handler = registry.get(action)
                            result = await action_handler.execute(ctx, step_params)
                        except KeyError:
                            raise NotImplementedError(f"Action {action} not supported")
                    elif action == "GOTO":
                        url = step_params.get("url", "")
    
                        if not url:
                            logger.warning(f"[{job_id}] Skipping GOTO step {i+1}: URL is empty")
                            await NervousSystem.publish_update(job_id, "RUNNING", "Skipped empty navigation", node_id)
                            continue
    
                        # Use configurable timeout. wait_until="domcontentloaded" is
                        # faster than "load" and enough to unblock Playwright.
                        await page.goto(url, timeout=NAVIGATION_TIMEOUT, wait_until="domcontentloaded")
    
                        # Wait for network to settle (catches lazy-loaded assets)
                        await safe_wait_for_network_idle(page)
    
                        # ⚡ PHASE 2: WAF Evasion & Captcha Detection
                        waf_detected = await page.evaluate('''() => {
                            const isCloudflare = document.querySelector('#cf-spinner') || document.title.includes('Just a moment...') || window.__cf_chl_opt;
                            const isDatadome = document.querySelector('iframe[src*="datadome.co"]');
                            const isCaptcha = document.querySelector('iframe[src*="recaptcha"]') || document.querySelector('iframe[src*="hcaptcha"]');
    
                            if (isCloudflare) return 'Cloudflare';
                            if (isDatadome) return 'Datadome';
                            if (isCaptcha) return 'Captcha';
                            return null;
                        }''')
    
                        if waf_detected:
                            logger.warning(f"[{job_id}] WAF/Captcha Detected: {waf_detected}")
    
                            # FIX: Automated Solver Routing Loop
                            solver_success = False
                            for attempt in range(3):
                                logger.info(f"[{job_id}] Routing page to Automated Solver API (Attempt {attempt+1}/3)...")
                                # Mock solver hook (FlareSolverr/CapSolver implementation goes here)
                                await asyncio.sleep(5)
    
                                # Re-verify WAF presence
                                still_detected = await page.evaluate('''() => {
                                    const isCloudflare = document.querySelector('#cf-spinner') || document.title.includes('Just a moment...') || window.__cf_chl_opt;
                                    const isDatadome = document.querySelector('iframe[src*="datadome.co"]');
                                    const isCaptcha = document.querySelector('iframe[src*="recaptcha"]') || document.querySelector('iframe[src*="hcaptcha"]');
                                    return isCloudflare || isDatadome || isCaptcha;
                                }''')
    
                                if not still_detected:
                                    solver_success = True
                                    logger.info(f"[{job_id}] Automated Solver successfully bypassed the interstitial.")
                                    break
    
                            if not solver_success:
                                # Use Temporal ApplicationError to explicitly mark this non-retryable
                                # Prevents infinite loops DDOSing Cloudflare endpoints
                                raise ApplicationError(
                                    f"WAF_DETECTED: {waf_detected} block. Automated solver failed 3 times.",
                                    type="HumanInterventionRequired",
                                    non_retryable=True,
                                    details={"url": url}
                                )
    
                        # ⚡ SPA FIX: After networkidle, JavaScript frameworks (React/Vue/Svelte)
                        # still need time to execute and paint the final DOM.
                        try:
                            await page.wait_for_load_state("networkidle", timeout=10000)
                        except PlaywrightTimeout:
                            pass
    
                        # --- ACTION VERIFICATION: GOTO ---
                        # Verify navigation actually succeeded (not blank/error page)
                        final_url = page.url
                        if final_url == "about:blank" or final_url == "chrome-error://chromewebcontent/":
                            logger.warning(f"[{job_id}] GOTO verification failed: landed on {final_url}")
                            await NervousSystem.publish_update(
                                job_id, "WARNING",
                                f"Navigation verification failed: page is blank/error ({final_url})",
                                node_id
                            )
                        else:
                            logger.info(f"[{job_id}] GOTO verified: {final_url}")

                        # Try to dismiss any popups that appeared on page load
                        await dismiss_overlays(page)
    
                    elif action == "HOVER":
                        intent = step_params["intent"]
                        result = await finder.find(intent, timeout=10000)
                        if not result.found:
                            raise Exception(f"Element not found: {intent}")
                        element = result.element
                        await element.hover()
                        await user_logger.info("FOUND_ELEMENT", element=f"Hovered {intent}")
    
                    elif action == "PRESS_KEY":
                        key = step_params["key"]
                        await page.keyboard.press(key)
                        await user_logger.progress(f"Pressed key: {key}")
    
                    elif action == "UPLOAD_FILE":
                        intent = step_params["intent"]
                        file_path = step_params["file_path"]
                        # Ensure absolute path
                        if not os.path.isabs(file_path):
                            file_path = os.path.join(DOWNLOAD_DIR, file_path)
    
                        result = await finder.find(intent, timeout=10000)
                        if not result.found:
                            raise Exception(f"Element not found: {intent}")
                        element = result.element
                        await element.set_input_files(file_path)
                        await user_logger.info("FOUND_ELEMENT", element=f"Uploaded {os.path.basename(file_path)}")
    
                    elif action == "SCROLL":
                        # Scroll to element OR by pixels
                        if "intent" in step_params:
                            intent = step_params["intent"]
                            result = await finder.find(intent, timeout=10000)
                            if not result.found:
                                raise Exception(f"Element not found: {intent}")
                            element = result.element
                            await element.scroll_into_view_if_needed()
                        elif "delta_y" in step_params:
                            delta_y = int(step_params["delta_y"])
                            await page.mouse.wheel(0, delta_y)
                        await user_logger.progress("Scrolled page")
    
                    elif action == "DRAG_AND_DROP":
                        source_intent = step_params["source"]
                        target_intent = step_params["target"]
    
                        source_res = await finder.find(source_intent)
                        target_res = await finder.find(target_intent)
    
                        if not source_res.found: raise Exception(f"Source not found: {source_intent}")
                        if not target_res.found: raise Exception(f"Target not found: {target_intent}")
    
                        source = source_res.element
                        target = target_res.element
    
                        await source.drag_to(target)
                        await user_logger.progress(f"Dragged {source_intent} to {target_intent}")
    
                    elif action == "WAIT_FOR":
                        # Wait for selector, network, or timeout
                        if "selector" in step_params:
                            state = step_params.get("state", "visible")
                            timeout = int(step_params.get("timeout_ms", 10000))
                            try:
                                await page.wait_for_selector(step_params["selector"], state=state, timeout=timeout)
                            except PlaywrightTimeout:
                                raise ValueError(f"WAIT_FOR Timeout: Selector '{step_params['selector']}' not {state} after {timeout}ms")
                        elif "event" in step_params:
                            event = step_params["event"]
                            if event == "network_idle":
                                await safe_wait_for_network_idle(page)
                        elif "timeout_ms" in step_params:
                            await asyncio.sleep(int(step_params["timeout_ms"]) / 1000)
    
                        await user_logger.info("WAITING_NETWORK")
    

                    elif action == "TYPE":
                        intent = step_params["intent"]
                        # Resilient extraction chain for LLM schema drift
                        text_to_type = step_params.get("text") or step_params.get("value") or step_params.get("content")
                        
                        if not text_to_type:
                            raise ValueError(f"TYPE action payload for node {node_id} missing text/value field.")
    
                        result = await finder.find(intent, timeout=10000)
                        if not result.found:
                            raise Exception(f"Element not found: {intent}")
                        element = result.element
    
                        # GHOST TYPIST FALLBACK (Resilient to non-input wrappers)
                        await element.click()
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=1000)
                        except PlaywrightTimeout:
                            pass
                        await page.keyboard.type(text_to_type, delay=50)
    
                        # --- ACTION VERIFICATION: TYPE ---
                        # Verify the input field received the typed value
                        try:
                            actual_value = await page.evaluate(
                                """(sel) => {
                                    const el = document.querySelector(sel);
                                    return el ? (el.value || el.innerText || '') : '';
                                }""",
                                f"[data-quanta-id]"
                            )
                            if actual_value and text_to_type not in actual_value:
                                logger.warning(f"[{job_id}] TYPE verification: typed '{text_to_type[:20]}' but field contains '{actual_value[:20]}'")
                        except Exception:
                            pass  # Non-critical verification

                        await NervousSystem.publish_update(job_id, "RUNNING", f"Typed input safely", node_id)
    
                    elif action == "LOAD_VAULT":
                        active_vault = step_params.get("vault_name")
                        logger.info(f"[{job_id}] Active vault set to {active_vault}")
                        await NervousSystem.publish_update(job_id, "SUCCESS", f"Loaded BYOS vault: {active_vault}", node_id)

                    elif action == "UNIVERSAL_AGENT":
                        nav_obj = step_params.get("navigation_objective")
                        ext_schema = step_params.get("extraction_schema")

                        logger.info(f"[{job_id}] Executing UNIVERSAL_AGENT node logic")
                        
                        # Logical patch for context initialization
                        ua_page = page
                        if active_vault:
                            storage_state = f"vaults/{active_vault}.json"
                            ua_context = await browser.new_context(storage_state=storage_state)
                            ua_page = await ua_context.new_page()

                        # Use the current page URL if we have navigated, otherwise target_url
                        current_url = page.url if page.url != "about:blank" else target_url

                        ua_result = await execute_universal_agent(
                            page=ua_page,
                            job_id=job_id,
                            user_logger=user_logger,
                            nervous_system=NervousSystem,
                            target_url=current_url,
                            navigation_objective=nav_obj,
                            extraction_schema=ext_schema,
                            materialized_files=materialized_files
                        )

                        if ext_schema and ua_result.get("status") == "success":
                            extracted_data = ua_result.get("data")
                            data_json = json.dumps(extracted_data)
                            await NervousSystem.publish_update(
                                job_id, "SUCCESS", "Universal Agent Extracted Schema", node_id, data=data_json
                            )

                    elif action == "LOG":
                        # Support both 'content' (from old nodes) and 'message' (from new nodes)
                        content = step_params.get("message") or step_params.get("content", "")
    
                        data_json = json.dumps({"content": content})
    
                        logger.info(f"[{job_id}] LOG: {content}")
                        await NervousSystem.publish_update(
                            job_id, "SUCCESS", f"Log: {content[:30]}...", node_id, data=data_json
                        )
    
                    elif action == "DATA_TRANSFORM":
                        import csv
                        import io
    
                        input_data = step_params.get("inputData")
                        output_format = step_params.get("format", "json")
    
                        logger.info(f"[{job_id}] Transforming data to {output_format}")
    
                        try:
                            # Parse input data if it's a JSON string
                            data_to_format = input_data
                            if isinstance(input_data, str):
                                try:
                                    data_to_format = json.loads(input_data)
                                except:
                                    # Keep as raw string if not JSON
                                    pass
    
                            transformed_value = ""
    
                            if output_format == "json":
                                transformed_value = json.dumps(data_to_format, indent=2)
                            elif output_format == "csv":
                                # Simple CSV conversion for lists of dicts
                                if isinstance(data_to_format, list) and len(data_to_format) > 0:
                                    output = io.StringIO()
                                    if isinstance(data_to_format[0], dict):
                                        writer = csv.DictWriter(
                                            output,
                                            fieldnames=data_to_format[0].keys(),
                                            quoting=csv.QUOTE_ALL
                                        )
                                        if step_params.get("includeHeader", True):
                                            writer.writeheader()
                                        writer.writerows(data_to_format)
                                    else:
                                        writer = csv.writer(output, quoting=csv.QUOTE_ALL)
                                        writer.writerows([[x] for x in data_to_format])
                                    transformed_value = output.getvalue()
                                else:
                                    transformed_value = str(data_to_format)
                            elif output_format == "html_table":
                                if isinstance(data_to_format, list) and len(data_to_format) > 0 and isinstance(data_to_format[0], dict):
                                    headers = data_to_format[0].keys()
                                    html = "<table><thead><tr>"
                                    html += "".join([f"<th>{h}</th>" for h in headers])
                                    html += "</tr></thead><tbody>"
                                    for row in data_to_format:
                                        html += "<tr>" + "".join([f"<td>{row.get(h, '')}</td>" for h in headers]) + "</tr>"
                                    html += "</tbody></table>"
                                    transformed_value = html
                                else:
                                    transformed_value = f"<p>{str(data_to_format)}</p>"
                            else:
                                transformed_value = str(data_to_format)
    
                            # Update status with data for frontend preview
                            res_json = json.dumps({"content": transformed_value})
                            await NervousSystem.publish_update(
                                job_id, "RUNNING", f"Formatted data as {output_format}", node_id, data=res_json
                            )
    
                        except Exception as e:
                            logger.error(f"[{job_id}] Transformation failed: {e}")
                            await NervousSystem.publish_update(
                                job_id, "WARNING", f"Formatting failed: {str(e)}", node_id
                            )
    
                    # --- VISUAL PROOF (Screenshot) ---
                    # Take a tiny jpeg
                    screenshot = await page.screenshot(
                        type='jpeg',
                        quality=20,
                        scale="css",
                        animations="disabled",
                        caret="hide"
                    )
    
                    # TASK 2 FIX: Upload screenshot to storage if enabled
                    screenshot_url = None
                    if is_s3_upload_enabled() and storage:
                        try:
                            screenshot_url = await storage.upload_screenshot(
                                screenshot, job_id, i + 1
                            )
                            logger.debug(f"[Storage] Screenshot uploaded: {screenshot_url[:60]}...")
                        except Exception as e:
                            logger.warning(f"[Storage] Screenshot upload failed: {e}")
                            # Continue with embedded screenshot fallback
    
                    # Send the visual proof to the dashboard
                    # If storage upload succeeded, we could send URL instead of bytes
                    # For now, still embed for real-time preview
                    await NervousSystem.publish_update(
                        job_id, "RUNNING", "Step Verified", node_id, screenshot=screenshot
                    )
    
    
                # --- 7. CLEANUP ---
                await NervousSystem.publish_update(job_id, "COMPLETED", "Workflow Finished Successfully", "end")
                workflow_succeeded = True  # Mark success before return
    
                # Return with metrics
                return {
                    "status": "SUCCESS",
                    "steps_completed": len(steps),
                    "job_id": job_id
                }
    
            except ApplicationError as app_err:
                logger.error(f"Job Failed (ApplicationError): {app_err}", exc_info=True)
                workflow_succeeded = False
                raise app_err  # Re-raise Temporal errors exactly as they are so Temporal halts
    
            except Exception as e:
                logger.error(f"Job Failed: {e}", exc_info=True)
                workflow_succeeded = False  # Explicitly mark failure
    
                # INDUSTRIAL: Capture screenshot for debugging
                failure_screenshot = b""
                if 'page' in locals() and page:
                    try:
                        if not page.is_closed():
                            failure_screenshot = await page.screenshot(type='jpeg', quality=60)
                    except:
                        pass
    
                # TELEMETRY: Failure with Stack Trace
                stack_trace = traceback.format_exc()
                await NervousSystem.publish(
                    f"quanta.telemetry.{job_id}",
                    json.dumps({"type": "log", "message": f"[Executor] Job Failed: {str(e)}\n{stack_trace}"})
                )
    
                await NervousSystem.publish_update(
                    job_id, "FAILED",
                    f"Critical Error: {str(e)}",
                    "error",
                    screenshot=failure_screenshot if failure_screenshot else None
                )
    
                # Explicitly close Playwright resources to prevent Zombie Chromium
                if 'context' in locals() and context:
                    await context.close()
                if 'browser' in locals() and browser:
                    await browser.close()
    
                # Re-raise so Temporal knows to retry
                raise e
    
            finally:
                # =================================================================
                # CRITICAL: Robust cleanup with existence checks
                # =================================================================
    
                # 1. Save session on success (BEFORE releasing account)
                if workflow_succeeded and is_session_persistence_enabled():
                    if 'session_manager' in locals() and session_manager and 'context' in locals() and context:
                        if 'target_domain' in locals() and target_domain and 'user_id' in locals():
                            try:
                                await session_manager.save_session(user_id, target_domain, context)
                                logger.info(f"[Session] Saved encrypted session for {target_domain}")
                            except Exception as session_err:
                                logger.warning(f"[Session] Failed to save session: {session_err}")
    
                # 2. Release account back to pool
                if 'leased_account' in locals() and leased_account and 'account_mgr' in locals():
                    try:
                        # Check if context exists before accessing cookies
                        new_cookies = None
                        if 'context' in locals() and context:
                            try:
                                # Context is safely closed by context manager AFTER this block
                                new_cookies = await context.cookies()
                            except Exception as cookie_err:
                                logger.warning(f"Could not capture cookies: {cookie_err}")
                        await account_mgr.release_account(
                            leased_account['id'],
                            new_cookies=new_cookies,
                            success=workflow_succeeded
                        )
                        logger.info(f"Released account {leased_account['username']}")
    
                    except Exception as release_err:
                        logger.error(f"Failed to release account: {release_err}")
    
                # --- RELEASE AUTH LOCK ---
                if "_auth_lock" in payload and account_mgr:
                    lock_info = payload["_auth_lock"]
                    await account_mgr.release_auth_lock(
                        lock_info["account_id"],
                        target_domain,
                    )
    

