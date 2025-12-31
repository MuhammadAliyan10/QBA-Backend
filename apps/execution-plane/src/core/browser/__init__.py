"""
Core Browser Module

Provides browser session management and persistence utilities.
"""

from .session import (
    SessionManager,
    SessionManagerError,
    EncryptionKeyMissing,
    SessionDecryptionError,
    get_session_manager,
)

__all__ = [
    "SessionManager",
    "SessionManagerError",
    "EncryptionKeyMissing",
    "SessionDecryptionError",
    "get_session_manager",
]
