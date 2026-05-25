# core/planning/sightedPipeline.py
"""
Sighted Pipeline v3.2 — JIT Epoch Orchestration with Workflow Persistence,
KMS Session Injection, and Token Telemetry.

Implements the sense→think→act loop with:
  1. KMS SESSION  — Inject encrypted session cookies to bypass login.
  2. CACHE CHECK  — Hash(objective+url) → load cached EpochPlans if present.
  3. SENSE        — Harvest the active tab's semantic DOM map.
  4. THINK        — Ask the SightedPlanner for an Epoch plan (3-5 actions).
  5. ACT          — Hand the plan to GoalExecutor for late-bound execution.
  6. PERSIST      — On success, serialize all EpochPlans to disk.
  7. TELEMETRY    — Finalize token usage to jobs + user_usage tables.

If the executor raises StateDesyncException during cached replay, the cache
is invalidated and the pipeline falls through to live JIT planning.
"""

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from core.url_utils import resolve_final_url

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from core.planning.harvester import harvest_context
from core.planning.sighted_planner import SightedPlanner, EpochPlan, GoalAction
from core.planning.goal_executor import GoalExecutor, EpochReport, StateDesyncException
from core.planning.token_telemetry import instrument_planner, finalize_telemetry
from core.network_sniffer import NetworkSniffer
from core.nervous_system import NervousSystem

logger = logging.getLogger("sightedPipeline")


# =============================================================================
# KMS SESSION RETRIEVAL — Mock implementation for cookie-based auth bypass
# =============================================================================

async def fetch_session_cookies(session_id: str) -> List[Dict[str, Any]]:
    """
    Retrieve encrypted session state from KMS, decrypt, and return
    a list of Playwright-compatible cookie dicts.

    In production, this calls Redis/Vault with Fernet decryption.
    Currently returns a mock for integration testing.
    """
    try:
        from config import is_session_persistence_enabled, get_fernet_key
        import redis.asyncio as aioredis
        from cryptography.fernet import Fernet

        if not is_session_persistence_enabled():
            logger.warning("[KMS] Session persistence not configured, skipping")
            return []

        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        fernet_key = get_fernet_key()

        client = aioredis.from_url(redis_url, decode_responses=False)
        try:
            encrypted = await client.get(f"session:{session_id}")
            if not encrypted:
                logger.warning(f"[KMS] No session found for {session_id}")
                return []

            fernet = Fernet(fernet_key.encode())
            decrypted = fernet.decrypt(encrypted)
            cookies = json.loads(decrypted)
            logger.info(f"[KMS] Loaded {len(cookies)} cookies for session {session_id}")
            return cookies
        finally:
            await client.aclose()

    except Exception as exc:
        logger.warning(f"[KMS] Session retrieval failed, falling back to unauthenticated: {exc}")
        return []


# =============================================================================
# WORKFLOW CACHE — Filesystem-Backed Plan Persistence
# =============================================================================

class WorkflowCache:
    """
    Persists List[EpochPlan] to disk keyed by a deterministic hash of
    (objective + entry_url). Enables zero-LLM replays for identical prompts.
    """

    def __init__(self, cache_dir: Optional[str] = None):
        base = cache_dir or os.getenv(
            "QUANTA_CACHE_DIR",
            os.path.join(os.path.dirname(__file__), "..", "..", ".quanta_cache"),
        )
        self._dir = Path(base).resolve()
        self._dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def compute_hash(objective: str, url: str) -> str:
        raw = f"{objective.strip().lower()}|{url.strip().lower()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _path(self, workflow_hash: str) -> Path:
        return self._dir / f"{workflow_hash}.json"

    def load(self, workflow_hash: str) -> Optional[List[EpochPlan]]:
        path = self._path(workflow_hash)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            epochs = [EpochPlan(**ep) for ep in data]
            logger.info(f"[Cache] HIT — loaded {len(epochs)} cached epochs for hash {workflow_hash}")
            return epochs
        except Exception as exc:
            logger.warning(f"[Cache] Corrupt cache for {workflow_hash}, wiping: {exc}")
            self.invalidate(workflow_hash)
            return None

    def save(self, workflow_hash: str, epochs: List[EpochPlan]) -> None:
        path = self._path(workflow_hash)
        try:
            serialized = [ep.model_dump(mode="json") for ep in epochs]
            path.write_text(json.dumps(serialized, indent=2), encoding="utf-8")
            logger.info(f"[Cache] Saved {len(epochs)} epochs to {path}")
        except Exception as exc:
            logger.error(f"[Cache] Failed to save: {exc}")

    def invalidate(self, workflow_hash: str) -> None:
        path = self._path(workflow_hash)
        try:
            path.unlink(missing_ok=True)
            logger.info(f"[Cache] Invalidated hash {workflow_hash}")
        except Exception as exc:
            logger.warning(f"[Cache] Failed to invalidate: {exc}")


# =============================================================================
# PIPELINE RESULT
# =============================================================================

@dataclass
class SightedPipelineResult:
    """Complete result of the JIT sighted pipeline."""
    success: bool
    job_id: str = ""
    status: str = "COMPLETED"
    error: Optional[str] = None
    epochs_run: int = 0
    desyncs_caught: int = 0
    goals_planned: int = 0
    goals_completed: int = 0
    total_duration_ms: int = 0
    extracted_data: Dict[str, Any] = field(default_factory=dict)
    cache_hit: bool = False
    llm_calls: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "job_id": self.job_id,
            "status": self.status,
            "error": self.error,
            "epochs_run": self.epochs_run,
            "desyncs_caught": self.desyncs_caught,
            "goals_planned": self.goals_planned,
            "goals_completed": self.goals_completed,
            "total_duration_ms": self.total_duration_ms,
            "extracted_data": self.extracted_data,
            "cache_hit": self.cache_hit,
            "llm_calls": self.llm_calls,
        }


# =============================================================================
# SIGHTED PIPELINE
# =============================================================================

class SightedPipeline:
    """
    Reactive JIT Epoch Loop Orchestrator with Workflow Persistence.

    Manages the lifecycle of an autonomous agent session across multiple
    epochs, tabs, and page transitions. Implements:
      - Reflex Arc: StateDesyncException → re-harvest → re-plan → resume.
      - Plan Cache: identical (objective, url) replays skip the LLM entirely.
    """

    MAX_EPOCHS = 15
    MAX_CONSECUTIVE_DESYNCS = 3
    STABILITY_DELAY_MS = 800

    def __init__(self, cache_dir: Optional[str] = None):
        self.planner = SightedPlanner()
        self.cache = WorkflowCache(cache_dir)

    async def run(
        self,
        url: str,
        objective: str,
        job_id: str = "",
        user_id: str = "",
        session_id: str = "",
        headless: bool = True,
        proxy: Optional[Dict] = None,
        enable_cache: bool = True,
        sniffer: Optional[NetworkSniffer] = None,
    ) -> SightedPipelineResult:
        """Execute the JIT Epoch loop until the objective is met or budget exhausted."""
        start_time = time.time()
        result = SightedPipelineResult(success=False, job_id=job_id)
        history: List[str] = []
        accumulated_epochs: List[EpochPlan] = []
        consecutive_desyncs = 0

        workflow_hash = WorkflowCache.compute_hash(objective, url)
        cached_epochs: Optional[List[EpochPlan]] = None

        if enable_cache:
            cached_epochs = self.cache.load(workflow_hash)
            if cached_epochs:
                result.cache_hit = True

        async with async_playwright() as pw:
            browser: Optional[Browser] = None
            try:
                # ==============================================================
                # PHASE 1: INITIALIZE BROWSER CONTEXT
                # ==============================================================
                await NervousSystem.publish_update(
                    job_id, "RUNNING", "[Pipeline] Launching browser...", "init",
                )

                launch_args: Dict[str, Any] = {
                    "headless": headless,
                    "args": ["--no-sandbox", "--disable-setuid-sandbox"],
                }
                if proxy:
                    launch_args["proxy"] = proxy

                browser = await pw.chromium.launch(**launch_args)
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                )

                # ==============================================================
                # KMS SESSION INJECTION — Bypass login with stored cookies
                # ==============================================================
                if session_id:
                    logger.info(f"[Pipeline] Injecting KMS session: {session_id}")
                    await NervousSystem.publish_update(
                        job_id, "RUNNING",
                        f"[Pipeline] Injecting session cookies...", "kms",
                    )
                    cookies = await fetch_session_cookies(session_id)
                    if cookies:
                        # Ensure cookies have required domain field
                        domain = urlparse(url).hostname
                        for cookie in cookies:
                            if "domain" not in cookie:
                                cookie["domain"] = domain
                        await context.add_cookies(cookies)
                        logger.info(f"[Pipeline] Injected {len(cookies)} session cookies")

                page = await context.new_page()

                # PHASE 4: Hybrid Network Sniffer
                target_domain = urlparse(url).hostname
                active_sniffer = sniffer or NetworkSniffer(target_domain=target_domain)
                await active_sniffer.start_sniffing(page)

                # Instrument planner with token telemetry
                if user_id or job_id:
                    instrument_planner(self.planner, user_id, job_id)

                # Pre-resolve redirects so Chromium doesn't crash on
                # bare-domain TLS redirects (e.g. amazon.com → www.amazon.com)
                url = await resolve_final_url(url)

                await NervousSystem.publish_update(
                    job_id, "RUNNING", f"[Pipeline] Navigating to {url}", "init",
                )
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

                executor = GoalExecutor(context, job_id=job_id)
                active_page = page

                # ==============================================================
                # PHASE 2A: CACHED REPLAY PATH
                # ==============================================================
                if cached_epochs:
                    logger.info(f"[Pipeline] Cache HIT — replaying {len(cached_epochs)} epochs (0 LLM calls)")
                    await NervousSystem.publish_update(
                        job_id, "RUNNING",
                        f"[Pipeline] Cache HIT — replaying {len(cached_epochs)} cached epochs",
                        "cache",
                    )

                    try:
                        for epoch_idx, epoch_plan in enumerate(cached_epochs):
                            result.epochs_run += 1
                            epoch_label = f"Cached Epoch {result.epochs_run}"

                            result.goals_planned += len(epoch_plan.intents)
                            logger.info(
                                f"[Pipeline] [{epoch_label}] Replaying: "
                                f"{epoch_plan.strategic_objective} "
                                f"({len(epoch_plan.intents)} intents)"
                            )

                            await NervousSystem.publish_update(
                                job_id, "RUNNING",
                                f"[{epoch_label}] {epoch_plan.strategic_objective}",
                                "act",
                            )

                            report = await executor.execute_epoch(epoch_plan, active_page)

                            result.goals_completed += sum(
                                1 for r in report.results if r.success
                            )
                            result.extracted_data.update(report.extracted_data)

                            if not report.success:
                                result.status = "FAILED"
                                result.error = report.error
                                break

                            if epoch_plan.is_final_step:
                                result.success = True
                                result.status = "COMPLETED"
                                break

                            await active_page.wait_for_timeout(self.STABILITY_DELAY_MS)

                        if result.success:
                            await NervousSystem.publish_update(
                                job_id, "COMPLETED",
                                "[Pipeline] Objective achieved (from cache).",
                                "complete",
                            )

                    except StateDesyncException as desync:
                        # Cache is stale — wipe it and fall through to live planning
                        logger.warning(
                            f"[Pipeline] Cache STALE — StateDesync during replay: {desync.reason}. "
                            f"Invalidating cache and switching to live JIT planning."
                        )
                        self.cache.invalidate(workflow_hash)
                        result.cache_hit = False
                        result.desyncs_caught += 1
                        cached_epochs = None
                        history.append(f"[CACHE_INVALIDATED] {desync.reason}")

                        # Re-sync active page
                        remaining = context.pages
                        if remaining:
                            active_page = remaining[-1]
                            await active_page.bring_to_front()

                        try:
                            await active_page.wait_for_load_state(
                                "domcontentloaded", timeout=5000,
                            )
                        except Exception:
                            await active_page.wait_for_timeout(1500)

                        # Fall through to live JIT loop below

                # ==============================================================
                # PHASE 2B: LIVE JIT EPOCH LOOP (cold start or cache invalidated)
                # ==============================================================
                if not result.success and result.status not in ("FAILED", "REJECTED"):
                    while result.epochs_run < self.MAX_EPOCHS:
                        result.epochs_run += 1
                        epoch_label = f"Epoch {result.epochs_run}"

                        try:
                            # --- 1. SENSE: Harvest Active Tab ---
                            await NervousSystem.publish_update(
                                job_id, "RUNNING",
                                f"[{epoch_label}] Harvesting DOM & Network context...", "sense",
                            )

                            all_pages = context.pages
                            if active_page not in all_pages:
                                active_page = all_pages[-1] if all_pages else page

                            # Get sniffed payloads
                            network_payloads = active_sniffer.get_captured_responses() if active_sniffer else []
                            
                            harvest = await harvest_context(active_page, network_payloads=network_payloads)

                            active_tab_data = {
                                "index": all_pages.index(active_page),
                                "url": harvest.get("url", active_page.url),
                                "title": harvest.get("title", ""),
                                "dom_map_text": json.dumps(
                                    harvest.get("dom_map"), indent=2
                                )[:6000],
                                "network_payloads": harvest.get("network_payloads", []),
                            }

                            background_tabs = []
                            for i, tab_page in enumerate(all_pages):
                                if tab_page is not active_page:
                                    try:
                                        tab_title = await tab_page.title()
                                    except Exception:
                                        tab_title = "(inaccessible)"
                                    background_tabs.append({
                                        "index": i,
                                        "url": tab_page.url,
                                        "title": tab_title,
                                    })

                            # --- 2. THINK: Ask Planner for Epoch ---
                            await NervousSystem.publish_update(
                                job_id, "RUNNING",
                                f"[{epoch_label}] Planning actions...", "think",
                            )
                            epoch_plan = await self.planner.plan_epoch(
                                objective, history, active_tab_data, background_tabs, result.extracted_data
                            )
                            result.llm_calls += 1

                            if not epoch_plan.feasible:
                                logger.warning(
                                    f"[Pipeline] Planner rejected: {epoch_plan.rejection_reason}"
                                )
                                result.status = "REJECTED"
                                result.error = epoch_plan.rejection_reason
                                await NervousSystem.publish_update(
                                    job_id, "FAILED",
                                    f"[Pipeline] Not feasible: {epoch_plan.rejection_reason}",
                                    "plan",
                                )
                                break

                            accumulated_epochs.append(epoch_plan)
                            result.goals_planned += len(epoch_plan.intents)
                            executed_actions_str = ", ".join(f"{g.action.value}('{g.intent}')" for g in epoch_plan.intents)
                            history.append(f"{epoch_plan.strategic_objective} [Actions: {executed_actions_str}]")

                            logger.info(
                                f"[Pipeline] [{epoch_label}] Strategy: "
                                f"{epoch_plan.strategic_objective} "
                                f"({len(epoch_plan.intents)} intents)"
                            )

                            # --- 3. ACT: Execute Epoch ---
                            await NervousSystem.publish_update(
                                job_id, "RUNNING",
                                f"[{epoch_label}] Executing {len(epoch_plan.intents)} actions...",
                                "act",
                            )
                            report = await executor.execute_epoch(epoch_plan, active_page)

                            result.goals_completed += sum(
                                1 for r in report.results if r.success
                            )
                            result.extracted_data.update(report.extracted_data)
                            consecutive_desyncs = 0

                            if not report.success:
                                result.status = "FAILED"
                                result.error = report.error
                                await NervousSystem.publish_update(
                                    job_id, "FAILED",
                                    f"[{epoch_label}] Execution failed: {report.error}",
                                    "act",
                                )
                                break

                            if epoch_plan.is_final_step:
                                result.success = True
                                result.status = "COMPLETED"
                                await NervousSystem.publish_update(
                                    job_id, "COMPLETED",
                                    "[Pipeline] Objective achieved.",
                                    "complete",
                                )
                                break

                            await active_page.wait_for_timeout(self.STABILITY_DELAY_MS)

                        except StateDesyncException as desync:
                            consecutive_desyncs += 1
                            result.desyncs_caught += 1

                            logger.warning(
                                f"[Pipeline] [{epoch_label}] StateDesync caught "
                                f"({consecutive_desyncs}/{self.MAX_CONSECUTIVE_DESYNCS}): "
                                f"{desync.reason}"
                            )
                            history.append(f"[DESYNC] {desync.reason}")

                            await NervousSystem.publish_update(
                                job_id, "RUNNING",
                                f"[{epoch_label}] State changed, re-planning... ({desync.reason[:80]})",
                                "desync",
                            )

                            if consecutive_desyncs >= self.MAX_CONSECUTIVE_DESYNCS:
                                result.status = "FAILED"
                                result.error = (
                                    f"Too many consecutive state desyncs "
                                    f"({self.MAX_CONSECUTIVE_DESYNCS}): {desync.reason}"
                                )
                                await NervousSystem.publish_update(
                                    job_id, "FAILED", f"[Pipeline] {result.error}", "desync",
                                )
                                break

                            remaining_pages = context.pages
                            if remaining_pages:
                                active_page = remaining_pages[-1]
                                await active_page.bring_to_front()

                            try:
                                await active_page.wait_for_load_state(
                                    "domcontentloaded", timeout=5000,
                                )
                            except Exception:
                                await active_page.wait_for_timeout(1500)

                            continue

                    if not result.success and result.status not in ("FAILED", "REJECTED"):
                        result.status = "TIMEOUT"
                        result.error = f"Exhausted {self.MAX_EPOCHS} epochs without completing objective."
                        await NervousSystem.publish_update(
                            job_id, "FAILED",
                            f"[Pipeline] {result.error}", "timeout",
                        )

                # ==============================================================
                # PHASE 3: PERSIST SUCCESSFUL PLANS TO CACHE
                # ==============================================================
                if result.success and accumulated_epochs and enable_cache:
                    self.cache.save(workflow_hash, accumulated_epochs)

            except Exception as exc:
                result.status = "FAILED"
                result.error = f"Pipeline crash: {str(exc)[:300]}"
                logger.error(f"[Pipeline] {result.error}", exc_info=True)
                await NervousSystem.publish_update(
                    job_id, "FAILED", f"[Pipeline] {result.error}", "crash",
                )

            finally:
                result.total_duration_ms = int((time.time() - start_time) * 1000)

                # Finalize token telemetry → persist to DB
                if job_id:
                    try:
                        ledger = await finalize_telemetry(job_id)
                        if ledger:
                            logger.info(
                                f"[Pipeline] Token usage: {ledger.total_tokens}t "
                                f"({ledger.prompt_tokens}p + {ledger.completion_tokens}c) "
                                f"across {ledger.llm_calls} calls"
                            )
                    except Exception as tel_exc:
                        logger.warning(f"[Pipeline] Telemetry finalization failed: {tel_exc}")

                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass

                logger.info(
                    f"[Pipeline] Finished | success={result.success} | "
                    f"epochs={result.epochs_run} | desyncs={result.desyncs_caught} | "
                    f"goals={result.goals_completed}/{result.goals_planned} | "
                    f"llm_calls={result.llm_calls} | cache_hit={result.cache_hit} | "
                    f"{result.total_duration_ms}ms"
                )

        return result
