"""
Feature Flag Configuration for Execution Plane

All flags default to False for safe deployment.
Set via environment variables:
- ENABLE_BILLING
- ENABLE_S3_UPLOAD
- ENABLE_NOTIFICATIONS
"""

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class FeatureFlags:
    """Immutable feature flag configuration."""
    enable_billing: bool = False
    enable_s3_upload: bool = False
    enable_notifications: bool = False


def _parse_bool(value: str, default: bool = False) -> bool:
    """Parse boolean from string with default value."""
    if not value:
        return default
    return value.lower().strip() in ("true", "1", "yes", "on")


@lru_cache(maxsize=1)
def get_flags() -> FeatureFlags:
    """
    Get feature flags singleton (cached).

    Call get_flags.cache_clear() to reload from env.
    """
    return FeatureFlags(
        enable_billing=_parse_bool(os.getenv("ENABLE_BILLING"), False),
        enable_s3_upload=_parse_bool(os.getenv("ENABLE_S3_UPLOAD"), False),
        enable_notifications=_parse_bool(os.getenv("ENABLE_NOTIFICATIONS"), False),
    )


def is_billing_enabled() -> bool:
    """Check if billing features are active."""
    return get_flags().enable_billing


def is_s3_upload_enabled() -> bool:
    """Check if S3 upload is active."""
    return get_flags().enable_s3_upload


def is_notifications_enabled() -> bool:
    """Check if notification integrations are active."""
    return get_flags().enable_notifications


# Convenience getters for common config values
def get_env(key: str, default: str = "") -> str:
    """Get environment variable with default."""
    return os.getenv(key, default)


def get_env_int(key: str, default: int = 0) -> int:
    """Get integer environment variable with default."""
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def get_fernet_key() -> str:
    """
    Get the Fernet encryption key for session storage.

    Returns:
        The encryption key as a string

    Raises:
        RuntimeError: If FERNET_KEY is not set
    """
    key = os.getenv("FERNET_KEY")
    if not key:
        raise RuntimeError(
            "Missing Encryption Key for Session Storage. "
            "Set FERNET_KEY in .env file. "
            "Generate with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        )
    return key


def is_session_persistence_enabled() -> bool:
    """
    Check if session persistence is available.

    Returns True only if both Redis and FERNET_KEY are configured.
    """
    has_redis = bool(os.getenv("REDIS_URL"))
    has_fernet = bool(os.getenv("FERNET_KEY"))
    return has_redis and has_fernet


# =============================================================================
# CENTRALIZED TIMEOUT CONFIGURATION
# =============================================================================
# All timeouts in one place for easy tuning  and visibility.
# Override via environment variables for production tuning.
# =============================================================================

@dataclass(frozen=True)
class TimeoutConfig:
    """
    Centralized timeout configuration.

    All values are in seconds unless noted otherwise.
    Override via environment variables:
    - TIMEOUT_ACTIVITY_SEC (default: 300 = 5 minutes)
    - TIMEOUT_HUMAN_WAIT_SEC (default: 86400 = 24 hours)
    - TIMEOUT_LLM_SEC (default: 30)
    - TIMEOUT_VECTOR_DB_SEC (default: 5)
    - TIMEOUT_WORKFLOW_START_SEC (default: 30)
    - TIMEOUT_GRACEFUL_SHUTDOWN_SEC (default: 15)
    """
    # Temporal Activity timeout (max time for browser automation)
    activity_timeout_sec: int = 300  # 5 minutes

    # Human-in-the-loop wait timeout (max hibernation time)
    human_wait_timeout_sec: int = 86400  # 24 hours

    # LLM API call timeout (Layer 4 cognitive)
    llm_timeout_sec: int = 30

    # Vector DB query timeout (Layer 3 semantic)
    vector_db_timeout_sec: int = 5

    # Workflow start timeout (Go API → Temporal)
    workflow_start_timeout_sec: int = 30

    # Worker graceful shutdown timeout
    graceful_shutdown_sec: int = 15

    # Browser action timeouts
    click_timeout_ms: int = 5000  # 5 seconds
    navigation_timeout_ms: int = 30000  # 30 seconds

    # Retry configuration
    max_retry_attempts: int = 3
    initial_retry_interval_sec: int = 2
    retry_backoff_coefficient: float = 2.0


@lru_cache(maxsize=1)
def get_timeouts() -> TimeoutConfig:
    """
    Get timeout configuration singleton (cached).

    Values are loaded from environment variables with sensible defaults.
    Call get_timeouts.cache_clear() to reload from env.
    """
    return TimeoutConfig(
        activity_timeout_sec=get_env_int("TIMEOUT_ACTIVITY_SEC", 300),
        human_wait_timeout_sec=get_env_int("TIMEOUT_HUMAN_WAIT_SEC", 86400),
        llm_timeout_sec=get_env_int("TIMEOUT_LLM_SEC", 30),
        vector_db_timeout_sec=get_env_int("TIMEOUT_VECTOR_DB_SEC", 5),
        workflow_start_timeout_sec=get_env_int("TIMEOUT_WORKFLOW_START_SEC", 30),
        graceful_shutdown_sec=get_env_int("TIMEOUT_GRACEFUL_SHUTDOWN_SEC", 15),
        click_timeout_ms=get_env_int("TIMEOUT_CLICK_MS", 5000),
        navigation_timeout_ms=get_env_int("TIMEOUT_NAVIGATION_MS", 30000),
        max_retry_attempts=get_env_int("MAX_RETRY_ATTEMPTS", 3),
        initial_retry_interval_sec=get_env_int("INITIAL_RETRY_INTERVAL_SEC", 2),
        retry_backoff_coefficient=float(get_env("RETRY_BACKOFF_COEFFICIENT", "2.0")),
    )


# Convenience accessors
def get_activity_timeout_sec() -> int:
    """Get activity timeout in seconds."""
    return get_timeouts().activity_timeout_sec


def get_human_wait_timeout_sec() -> int:
    """Get human-in-the-loop wait timeout in seconds."""
    return get_timeouts().human_wait_timeout_sec


def get_llm_timeout_sec() -> int:
    """Get LLM API timeout in seconds."""
    return get_timeouts().llm_timeout_sec


def get_max_retry_attempts() -> int:
    """Get maximum retry attempts."""
    return get_timeouts().max_retry_attempts
