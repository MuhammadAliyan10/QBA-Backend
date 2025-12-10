"""
Secure Secrets Management - Python Integration

This module demonstrates how to securely handle secrets in activities.py
without logging them to console, NATS, or databases.

CRITICAL: Secrets (passwords, API keys, tokens) must NEVER be logged.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("activity")

# ========================================
# SECURE SECRETS RESOLUTION
# ========================================

def resolve_param_securely(
    value: Any,
    params: Dict[str, str],
    secrets: Dict[str, str]
) -> Any:
    """
    Resolve template variables like {password} from params or secrets.

    SECURITY: Checks secrets map FIRST. Never logs secret values.

    Args:
        value: The parameter value (may contain {variable})
        params: Regular parameters (safe to log)
        secrets: Sensitive credentials (NEVER logged)

    Returns:
        Resolved value with variables replaced

    Example:
        >>> resolve_param_securely("{password}", {}, {"password": "secret123"})
        "secret123"  # Used but NOT logged
    """
    if not isinstance(value, str):
        return value

    # Check if it's a template variable: {key}
    if value.startswith("{") and value.endswith("}"):
        key = value[1:-1]

        # STEP 1: Check secrets FIRST (highest priority)
        if key in secrets:
            # CRITICAL: DO NOT LOG THE VALUE
            logger.debug(f"[Security] Resolved'{key}' from secrets (value redacted)")
            return secrets[key]

        # STEP 2: Fallback to regular params
        if key in params:
            logger.debug(f"[System] Resolved'{key}' from params: {params[key]}")
            return params[key]

        # STEP 3: Not found - return original
        logger.warning(f"[Warning] Variable'{key}' not found in secrets or params")
        return value

    return value


# ========================================
# INTEGRATION INTO activities.py
# ========================================

def browser_automation_activity_with_secrets(job_id: str, node_id: str, config: Dict, steps: list):
    """
    Example of how to integrate secure secrets handling into activities.py
    """

    # Extract secrets from workflow payload
    # CRITICAL: Do NOT log secrets
    secrets = config.get("secrets", {})
    params = config.get("params", {})

    if secrets:
        logger.info(f"[Security] Received {len(secrets)} secure credential(s) (values redacted)")

    # Process each step
    for step in steps:
        action = step["action"]
        step_params = step.get("params", {})

        # Resolve all params SECURELY
        resolved_params = {}
        for key, value in step_params.items():
            resolved_params[key] = resolve_param_securely(value, params, secrets)

        # Example: TYPE action with password
        if action == "TYPE":
            intent = resolved_params.get("intent")
            text = resolved_params.get("text")

            # SECURITY CHECK: Don't log if text came from secrets
            if text in secrets.values():
                logger.info(f"[Security] Typing secure credential into field: {intent}")
            else:
                logger.info(f"[Input] Typing into field'{intent}': {text}")

        # ... rest of action handlers ...


# ========================================
# HUMAN INTERVENTION WITH NATS
# ========================================

def publish_human_intervention(nervous_system, job_id: str, reason: str, prompt: str, options: list):
    """
    Publish human intervention event to NATS for notification dispatcher.

    Args:
        nervous_system: NervousSystem instance (NATS client)
        job_id: Workflow job ID
        reason: Why human input is needed
        prompt: Question for the user
        options: Available choices
    """
    import json
    import time

    event = {
        "job_id": job_id,
        "reason": reason,
        "prompt_message": prompt,
        "options": options,
        "timestamp": int(time.time())
    }

    # Publish to NATS subject: job.alert.<job_id>
    subject = f"job.alert.{job_id}"
    nervous_system.nc.publish(subject, json.dumps(event).encode())

    logger.info(f"[Alert] Published human intervention request: {reason}")


# ========================================
# COMPLETE EXAMPLE
# ========================================

async def example_workflow_with_secrets():
    """
    Full example showing secrets + human intervention integration.
    """
    from core.NervousSystem import NervousSystem

    # Workflow payload from Temporal/API
    workflow_payload = {
        "params": {
            "url": "https://secure-site.com",
            "username": "{username}"
        },
        "secrets": {
            "username": "admin@company.com",
            "password": "SuperSecret123!"  # NEVER logged
        }
    }

    params = workflow_payload["params"]
    secrets = workflow_payload["secrets"]

    # Simulate login step
    username = resolve_param_securely("{username}", params, secrets)
    password = resolve_param_securely("{password}", params, secrets)

    # Log output:
    # 🔒 Resolved 'username' from secrets (value redacted)
    # 🔒 Resolved 'password' from secrets (value redacted)

    # Simulate human intervention
    nervous_system = NervousSystem()
    publish_human_intervention(
        nervous_system,
        job_id="550e8400-e29b-41d4-a716-446655440000",
        reason="High-value transaction detected",
        prompt="Authorize purchase of $1,500 laptop?",
        options=["Approve", "Deny", "Review Later"]
    )

    # This triggers:
    # 1. NATS event → job.alert.<job_id>
    # 2. Go notification dispatcher catches it
    # 3. Logs: 📱 [MOCK WHATSAPP] Sending to User: Authorize purchase...
    # 4. DB updated: status='WAITING_FOR_USER'


if __name__ == "__main__":
    """
    Test secure resolution
    """
    print("=" * 60)
    print("SECURE SECRETS RESOLUTION TEST")
    print("=" * 60)

    params = {"url": "https://example.com"}
    secrets = {"password": "secret123", "api_key": "sk_live_xxx"}

    # Test 1: Resolve from secrets (should NOT log value)
    result = resolve_param_securely("{password}", params, secrets)
    print(f"Password resolved (redacted): {'*' * len(result)}")

    # Test 2: Resolve from params (safe to log)
    result = resolve_param_securely("{url}", params, secrets)
    print(f"URL resolved: {result}")

    # Test 3: Non-template value
    result = resolve_param_securely("Click here", params, secrets)
    print(f"Static value: {result}")

    print("=" * 60)
