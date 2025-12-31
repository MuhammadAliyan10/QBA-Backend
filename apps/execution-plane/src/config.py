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

