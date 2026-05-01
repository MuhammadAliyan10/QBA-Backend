import logging
import json
from datetime import timedelta
from typing import Dict, Any, List, Optional
from temporalio import workflow
from temporalio.common import RetryPolicy

# Workflow-safe imports (must be inside the unsafe block)
with workflow.unsafe.imports_passed_through():
    from activities.publish_activities import publish_event_activity
    # NodeBuilder is pure Python (no async/await) — safe to use inside workflow
    from core.planning.node_builder import NodeBuilder
    from core.planning.intent_parser import Intent

logger = logging.getLogger("generationWorkflow")

# ── Layout constants ───────────────────────────────────────────────────────────
NODE_X_START   = 150
NODE_X_SPACING = 300
NODE_Y         = 300


# =============================================================================
# GENERATION WORKFLOW
# =============================================================================

@workflow.defn
class GenerateWorkflowRecipe:
    """
    Assertion & Self-Healing Workflow Generation Orchestrator.

    5-Step Pipeline:
      1. Pre-Validation
      2. LLM Workflow Map Generation
      3. Strict Headless Execution Assertion
      4. Self-Healing Loop (Max 3 retries)
      5. Final State Resolution (Save/Notify)
    """

    def __init__(self):
        self.jobId: str = ""
        self.nodes: List[Dict] = []
        self.edges: List[Dict] = []

    @workflow.run
    async def run(self, payload: dict) -> dict:
        self.jobId = payload.get("job_id", workflow.info().workflow_id)
        try:
            return await self._runImpl(payload)
        finally:
            # Always clean up the browser session, even on failure
            try:
                await workflow.execute_activity(
                    "cleanup_browser_activity",
                    {"job_id": self.jobId},
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=RetryPolicy(maximum_attempts=2)
                )
            except Exception as cleanupErr:
                workflow.logger.warning(f"[{self.jobId}] Cleanup error: {cleanupErr}")

    async def _runImpl(self, payload: dict) -> dict:
        jobId   = self.jobId
        prompt  = payload.get("prompt", "")
        url     = payload.get("url", "")
        cookies = payload.get("cookies", [])

        workflow.logger.info(f"[{jobId}] Assertion & Healing generation starting for: '{prompt[:60]}'")
        await self._publishStatus(jobId, "RUNNING", "Starting Assertion & Self-Healing Pipeline...")

        # =====================================================================
        # 1. PRE-VALIDATION (Zero AI Cost)
        # =====================================================================
        workflow.logger.info(f"[{jobId}] Phase 1: Validating request...")
        valResult = await workflow.execute_activity(
            "validateRequestActivity",
            {"url": url, "prompt": prompt},
            start_to_close_timeout=timedelta(seconds=20)
        )
        if not valResult.get("valid"):
            err = valResult.get("error", "Validation Failed")
            await self._publishStatus(jobId, "FAILED", err)
            return await self._emitCompleted(jobId, "failed", err)

        # =====================================================================
        # 2. INTENT SEQUENCING (Tiny AI)
        # =====================================================================
        workflow.logger.info(f"[{jobId}] Phase 2: Generating logical intent sequence...")
        genResult = await workflow.execute_activity(
            "generateIntentSequenceActivity",
            {"url": url, "prompt": prompt, "job_id": jobId},
            start_to_close_timeout=timedelta(seconds=60)
        )
        if not genResult.get("success"):
            err = genResult.get("error", "Sequencing Failed")
            await self._publishStatus(jobId, "FAILED", f"Sequence LLM mapping failed: {err}")
            return await self._emitCompleted(jobId, "failed", err)

        sequence = genResult.get("sequence", [])

        # =====================================================================
        # 3. HYBRID AGENTIC DOM-WALKER (Math-First + AI-Fallback)
        # =====================================================================
        workflow.logger.info(f"[{jobId}] Phase 3: Executing Agentic DOM-Walker...")

        execResult = await workflow.execute_activity(
            "executeHybridWorkflowActivity",
            {"job_id": jobId, "sequence": sequence, "cookies": cookies, "url": url},
            start_to_close_timeout=timedelta(minutes=5)
        )

        success = execResult.get("success", False)
        workflow_map = execResult.get("sequence", sequence)
        final_error = execResult.get("error_trace", "Agentic DOM-Walker Failed")

        # =====================================================================
        # 5. FINAL STATE RESOLUTION
        # =====================================================================
        if not success:
            workflow.logger.error(f"[{jobId}] Workflow generation failed after retries: {final_error}")
            await self._publishStatus(jobId, "FAILED", final_error)
            return await self._emitCompleted(jobId, "failed", final_error)

        # Success! Build Nodes and Edges for the Frontend Canvas
        workflow.logger.info(f"[{jobId}] Phase 5: Success! Building final workflow graph...")

        nodeBuilder = NodeBuilder()
        triggerNode = nodeBuilder.buildTriggerNode(url, triggerType="MANUAL", cron=None)
        self.nodes.append(triggerNode)
        await self._publishNodeVerified(jobId, triggerNode)
        previousNodeId = triggerNode["id"]

        for i, step in enumerate(workflow_map):
            intent = Intent(
                stepNumber=i+1,
                action=step.get("action", "click"),
                targetDescription=step.get("target", ""),
                value=step.get("value", ""),
                qualifier=None,
                rawSentence="",
                confidence=1.0
            )

            # Safely grab TYPE
            try:
                nodeType = NodeBuilder().ACTION_TO_NODE_TYPE.get(intent.action, "CLICK")
            except:
                nodeType = "CLICK"

            if intent.action == "navigate": nodeType = "NAVIGATE"
            if intent.action == "wait": nodeType = "WAIT"
            if intent.action == "type": nodeType = "TYPE"
            if intent.action == "press_key": nodeType = "PRESS_KEY"
            if intent.action == "scrape": nodeType = "SCRAPE"

            nodeId = f"node-{i+1}"
            position = {"x": NODE_X_START + (i+1)*NODE_X_SPACING, "y": NODE_Y}

            # Map parameters
            config = {
                "intent": intent.targetDescription,
                "value": step.get("value", ""),
                "selector": step.get("selector", "")
            }
            if intent.action == "navigate":
                config["url"] = step.get("value") or url
            elif intent.action == "type":
                config["text"] = step.get("value", "")
            elif intent.action == "scrape":
                config["extractType"] = "text"

            if "scrapedValue" in step:
                config["scrapedValue"] = step.get("scrapedValue")

            node = {
                "id": nodeId,
                "type": nodeType,
                "position": position,
                "data": {
                    "label": f"{intent.action.capitalize()} {intent.targetDescription[:20]}",
                    "nodeType": nodeType,
                    "category": "browser",
                    "config": config,
                    "verified": True,
                    "confidence": 1.0,
                    "inputs": [{"id": "input", "label": "Input", "dataType": "trigger"}],
                    "outputs": [{"id": "output", "label": "Output", "dataType": "trigger"}],
                }
            }
            self.nodes.append(node)
            self.edges.append({
                "id": f"e-{previousNodeId}-{nodeId}",
                "source": previousNodeId,
                "target": nodeId,
                "sourceHandle": "output",
                "targetHandle": "input",
                "type": "default",
                "animated": True
            })
            previousNodeId = nodeId
            await self._publishNodeVerified(jobId, node)

        await self._publishStatus(jobId, "COMPLETED", "Workflow generated and asserted successfully!")
        return await self._emitCompleted(jobId, "success", "Workflow verified.")

    # ── PRIVATE HELPERS ────────────────────────────────────────────────────

    async def _publishStatus(self, jobId: str, status: str, message: str):
        try:
            await workflow.execute_activity("publish_event_activity", {"job_id": jobId, "type": "LOG", "status": status, "message": message, "node_id": "workflow"}, start_to_close_timeout=timedelta(seconds=5))
        except Exception: pass

    async def _publishNodeVerified(self, jobId: str, node: dict):
        try:
            await workflow.execute_activity("publish_event_activity", {"job_id": jobId, "type": "NODE_STATUS", "status": "verified", "message": json.dumps({"node": node, "confidence": 1.0}), "node_id": node.get("id", "unknown")}, start_to_close_timeout=timedelta(seconds=5))
        except Exception: pass

    async def _emitCompleted(self, jobId: str, status: str, message: str) -> dict:
        try:
            await workflow.execute_activity("publish_event_activity", {"job_id": jobId, "type": "WORKFLOW_STATUS", "status": "completed" if status != "failed" else "failed", "message": json.dumps({"status": status, "error": message, "nodes": self.nodes, "edges": self.edges}), "node_id": "workflow"}, start_to_close_timeout=timedelta(seconds=10))
        except Exception: pass
        return {"status": status, "nodes": self.nodes, "edges": self.edges, "message": message}
