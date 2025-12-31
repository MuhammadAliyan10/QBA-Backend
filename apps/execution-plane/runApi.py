#!/usr/bin/env python3
"""
runApi.py - Quick API Server for Testing Preflight

Run with:
    python runApi.py

Endpoints:
    POST /api/engine/preflight  - Test the preflight pipeline
    GET  /api/engine/health     - Health check
"""

import os
import sys
import asyncio
import logging

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from flask import Flask, request, jsonify
from flask_cors import CORS

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("api")

app = Flask(__name__)
CORS(app)


# =============================================================================
# PREFLIGHT ENDPOINT
# =============================================================================

@app.route("/api/engine/preflight", methods=["POST"])
def preflight_endpoint():
    """Run the tri-layer preflight pipeline."""
    from core.rag.preflight import handle_preflight_request

    payload = request.get_json()

    if not payload:
        return jsonify({"success": False, "error": "No JSON body"}), 400

    logger.info(f"[API] Preflight request: {payload.get('url')}")

    # Run async function in sync context
    result = asyncio.run(handle_preflight_request(payload))

    return jsonify(result)


# =============================================================================
# EXECUTE ENDPOINT
# =============================================================================

@app.route("/api/engine/execute", methods=["POST"])
def execute_endpoint():
    """Execute a hardened recipe."""
    from activities.recipeActivity import run_recipe_execution
    import time

    payload = request.get_json()

    if not payload or not payload.get("recipe"):
        return jsonify({"success": False, "error": "Missing recipe"}), 400

    if not payload.get("job_id"):
        payload["job_id"] = f"job-{int(time.time())}"

    logger.info(f"[API] Execute request: {payload['job_id']}")

    result = asyncio.run(run_recipe_execution(payload))

    return jsonify(result)


# =============================================================================
# HEALTH CHECK
# =============================================================================

@app.route("/api/engine/health", methods=["GET"])
def health_check():
    """Check service health."""
    health = {
        "status": "healthy",
        "services": {}
    }

    # Check OpenAI
    openai_key = os.getenv("OPENAI_API_KEY")
    health["services"]["openai"] = "configured" if openai_key else "missing"

    # Check Database
    db_url = os.getenv("DATABASE_URL")
    health["services"]["database"] = "configured" if db_url else "missing"

    if not openai_key:
        health["status"] = "degraded"
        health["warning"] = "OPENAI_API_KEY not set - Planner will fail"

    return jsonify(health)


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    port = int(os.getenv("API_PORT", "8001"))

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║              QUANTA PREFLIGHT API SERVER                     ║
╠══════════════════════════════════════════════════════════════╣
║  Endpoints:                                                  ║
║    POST /api/engine/preflight  - Tri-layer pipeline          ║
║    POST /api/engine/execute    - Run hardened recipe         ║
║    GET  /api/engine/health     - Health check                ║
╠══════════════════════════════════════════════════════════════╣
║  Test with:                                                  ║
║    curl http://localhost:{port}/api/engine/health              ║
╚══════════════════════════════════════════════════════════════╝
    """)

    app.run(host="0.0.0.0", port=port, debug=True)
