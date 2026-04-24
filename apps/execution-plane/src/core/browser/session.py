"""
Session Manager - Encrypted Browser Session Persistence

Provides secure storage and retrieval of browser sessions (cookies + localStorage)
using Fernet symmetric encryption and Redis.

Security:
- All session data encrypted at rest with Fernet (AES-128-CBC + HMAC)
- Session TTL of 7 days by default
- Encryption key must be provided via FERNET_KEY environment variable

Usage:
    manager = SessionManager(redis_client)

    # Restore session before navigation
    session = await manager.get_session(user_id, "linkedin.com")
    if session:
        context = await browser.new_context(storage_state=session)

    # Save session after successful execution
    await manager.save_session(user_id, "linkedin.com", context)
"""

import os
import json
import logging
from typing import Optional, Dict, Any
from urllib.parse import urlparse

import redis.asyncio as aioredis
from cryptography.fernet import Fernet, InvalidToken
from playwright.async_api import BrowserContext, Page, Response


# Deferred imports inside methods to break circular dependencies
# from core.planning.elementMatcher import ElementMatcher, Intent

from core.browser.domHarvester import DOMHarvester

logger = logging.getLogger("session_manager")


class SessionManagerError(Exception):
    """Base exception for session manager errors."""
    pass


class EncryptionKeyMissing(SessionManagerError):
    """Raised when FERNET_KEY is not configured."""
    pass


class SessionDecryptionError(SessionManagerError):
    """Raised when session decryption fails."""
    pass


class SessionManager:
    """
    Manages encrypted browser sessions in Redis.

    Thread-safe and async-compatible.
    """

    # Session TTL in seconds (7 days)
    DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60

    # Redis key prefix
    KEY_PREFIX = "session"

    def __init__(
        self,
        redis_client: aioredis.Redis,
        encryption_key: Optional[str] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS
    ):
        """
        Initialize SessionManager.

        Args:
            redis_client: Async Redis client
            encryption_key: Fernet-compatible encryption key (base64-encoded 32 bytes)
                           If not provided, reads from FERNET_KEY env var
            ttl_seconds: Session expiration time (default: 7 days)

        Raises:
            EncryptionKeyMissing: If no encryption key is available
        """
        self._redis = redis_client
        self._ttl = ttl_seconds

        # Get encryption key
        key = encryption_key or os.getenv("FERNET_KEY")
        if not key:
            raise EncryptionKeyMissing(
                "Missing Encryption Key for Session Storage. "
                "Set FERNET_KEY in environment or provide encryption_key parameter."
            )

        # Initialize Fernet cipher
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except Exception as e:
            raise EncryptionKeyMissing(
                f"Invalid FERNET_KEY format. Generate with: "
                f"python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
            ) from e

        logger.info("[SessionManager] Initialized with encrypted storage")

    def _make_key(self, user_id: str, domain: str) -> str:
        """Generate Redis key for user/domain pair."""
        # Sanitize inputs to prevent key injection
        safe_user = user_id.replace(":", "_").replace(" ", "_")
        safe_domain = domain.replace(":", "_").replace(" ", "_").lower()
        return f"{self.KEY_PREFIX}:{safe_user}:{safe_domain}"

    @staticmethod
    def extract_domain(url: str) -> str:
        """
        Extract and normalize the domain from a URL.

        TASK 4 FIX: Robust input validation with edge case handling.

        Handles:
        - Standard URLs: "https://www.linkedin.com/in/profile" -> "linkedin.com"
        - Missing protocols: "google.com" -> "google.com"
        - Subdomains: "www.example.com" -> "example.com"
        - Ports: "localhost:8080" -> "localhost"
        - IP addresses: "192.168.1.1" -> "192.168.1.1"
        - Trailing slashes: "https://example.com/" -> "example.com"

        Blocks:
        - javascript: URLs -> ""
        - data: URLs -> ""
        - file: URLs -> ""
        - Empty/None input -> ""

        Returns:
            Normalized domain string, or empty string if invalid/unsafe
        """
        # Handle None/empty input
        if not url or not isinstance(url, str):
            return ""

        # Strip whitespace
        url = url.strip()

        if not url:
            return ""

        # Security: Block dangerous URL schemes
        BLOCKED_SCHEMES = {"javascript", "data", "file", "vbscript", "about"}

        # Check for blocked schemes (case-insensitive)
        lower_url = url.lower()
        for scheme in BLOCKED_SCHEMES:
            if lower_url.startswith(f"{scheme}:"):
                logger.warning(f"[Session] Blocked dangerous URL scheme: {scheme}")
                return ""

        # Add protocol if missing (needed for urlparse to work correctly)
        # urlparse treats "google.com" as a path, not netloc
        if not url.startswith(("http://", "https://")):
            # Check if it looks like a domain (contains at least one dot or is localhost)
            if "." in url or url.startswith("localhost"):
                url = f"https://{url}"
            else:
                # Single word without protocol - could be a local path, not a domain
                logger.warning(f"[Session] Ambiguous URL without protocol: {url}")
                return ""

        try:
            parsed = urlparse(url)

            # Extract netloc (domain with optional port)
            domain = parsed.netloc or parsed.path.split("/")[0]

            if not domain:
                return ""

            # Remove port if present (e.g., "localhost:8080" -> "localhost")
            domain = domain.split(":")[0]

            # Remove www. prefix
            if domain.startswith("www."):
                domain = domain[4:]

            # Normalize to lowercase
            domain = domain.lower()

            # Final validation: domain should have at least one character
            # and not contain special characters except dots and hyphens
            if not domain:
                return ""

            # Validate domain characters (letters, digits, dots, hyphens)
            for char in domain:
                if not (char.isalnum() or char in ".-"):
                    logger.warning(f"[Session] Invalid character in domain: {char}")
                    return ""

            # Domain shouldn't start or end with hyphen or dot
            if domain.startswith(("-", ".")) or domain.endswith(("-", ".")):
                return ""

            return domain

        except Exception as e:
            logger.error(f"[Session] Failed to parse URL '{url}': {e}")
            return ""

    async def get_session(
        self,
        user_id: str,
        domain: str
    ) -> Optional[dict[str, Any]]:
        """
        Retrieve and decrypt a stored browser session.

        Args:
            user_id: Unique user identifier
            domain: Target domain (e.g., "linkedin.com")

        Returns:
            Decrypted session data (Playwright storage_state format) or None if not found

        Raises:
            SessionDecryptionError: If stored session cannot be decrypted
        """
        key = self._make_key(user_id, domain)

        try:
            # Fetch from Redis
            encrypted_data = await self._redis.get(key)

            if not encrypted_data:
                logger.debug(f"[Session] No session found for {user_id}@{domain}")
                return None

            # Decrypt
            try:
                decrypted_bytes = self._fernet.decrypt(encrypted_data)
                session_data = json.loads(decrypted_bytes.decode("utf-8"))
            except InvalidToken:
                logger.warning(f"[Session] Decryption failed for {user_id}@{domain} - key mismatch or corruption")
                # Delete corrupted session
                await self._redis.delete(key)
                return None
            except json.JSONDecodeError as e:
                logger.error(f"[Session] JSON decode error for {user_id}@{domain}: {e}")
                await self._redis.delete(key)
                return None

            # Validate structure
            if not isinstance(session_data, dict):
                logger.warning(f"[Session] Invalid session structure for {user_id}@{domain}")
                return None

            # Check if session has required fields
            if "cookies" not in session_data and "origins" not in session_data:
                logger.warning(f"[Session] Session missing cookies/origins for {user_id}@{domain}")
                return None

            logger.info(f"[Session] Restored session for {user_id}@{domain} ({len(session_data.get('cookies', []))} cookies)")
            return session_data

        except aioredis.RedisError as e:
            logger.error(f"[Session] Redis error fetching {user_id}@{domain}: {e}")
            return None

    async def save_session(
        self,
        user_id: str,
        domain: str,
        context: BrowserContext
    ) -> bool:
        """
        Encrypt and save browser session to Redis.

        Args:
            user_id: Unique user identifier
            domain: Target domain (e.g., "linkedin.com")
            context: Playwright BrowserContext to extract session from

        Returns:
            True if session saved successfully, False otherwise
        """
        key = self._make_key(user_id, domain)

        try:
            # Extract storage state from browser context
            storage_state = await context.storage_state()

            # Validate we have meaningful data
            cookie_count = len(storage_state.get("cookies", []))
            origin_count = len(storage_state.get("origins", []))

            if cookie_count == 0 and origin_count == 0:
                logger.debug(f"[Session] No session data to save for {user_id}@{domain}")
                return False

            # Serialize to JSON
            json_data = json.dumps(storage_state, separators=(",", ":"))  # Compact JSON

            # Encrypt
            encrypted_data = self._fernet.encrypt(json_data.encode("utf-8"))

            # Save to Redis with TTL
            await self._redis.setex(key, self._ttl, encrypted_data)

            logger.info(
                f"[Session] Saved session for {user_id}@{domain} "
                f"({cookie_count} cookies, {origin_count} origins, TTL={self._ttl//86400}d)"
            )
            return True

        except Exception as e:
            logger.error(f"[Session] Failed to save session for {user_id}@{domain}: {e}")
            return False

    # --- GHOST SESSION DETECTION (Zero LLM) ---

    async def verify_session(self, page: Page, target_url: str) -> bool:
        """
        Navigate to target and verify if the session is still valid.

        Logic:
        1. Intercept 401 Unauthorized responses.
        2. Use ElementMatcher to find 'Logout' or 'Account' signatures.
        3. Check for obvious login page indicators in URL.
        """
        logger.info(f"[Session] Verifying ghost state for {target_url}")

        unauthorized_detected = False

        # 1. Listen for 401s during navigation
        async def handle_response(response: Response):
            nonlocal unauthorized_detected
            if response.status == 401:
                logger.warning(f"[Session] Intercepted 401 on {response.url}")
                unauthorized_detected = True

        page.on("response", handle_response)

        try:
            # Navigate with a short timeout for verification
            await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)

            if unauthorized_detected:
                return False

            # 2. Check for login-indicative URL redirects
            current_url = page.url.lower()
            if any(path in current_url for path in ["/login", "/signin", "/auth"]):
                logger.warning(f"[Session] Redirected to login path: {current_url}")
                return False

            # 3. DOM Signature Matching (Math-First)
            # Harvest full DOM snapshot
            harvester = DOMHarvester()
            snapshot = await harvester.harvest(page)

            # Deferred import to break circular dependency with core.browser
            from core.planning.elementMatcher import ElementMatcher
            from core.planning.intentParser import Intent

            # Use ElementMatcher to look for "Logout" or "Sign Out"
            matcher = ElementMatcher()

            # We use a provisional Intent for the matcher to look for auth markers
            auth_intent = Intent(
                action="CLICK",
                targetDescription="Logout or Sign Out or Log off",
                qualifier="first"
            )

            account_intent = Intent(
                action="SEE",
                targetDescription="My Account or Profile or User Menu",
                qualifier="first"
            )

            try:
                # If we find a confident 'Logout' button, session is definitely active
                logout_match = await matcher.match(auth_intent, snapshot)
                if logout_match.confidence > 0.7:
                    logger.info(f"[Session] Ghost check passed: Found '{logout_match.element.text}'")
                    return True
            except Exception:
                pass # Matcher might raise AIFallbackTriggered if not found

            try:
                # Secondary check: Profile indicators
                account_match = await matcher.match(account_intent, snapshot)
                if account_match.confidence > 0.7:
                    logger.info(f"[Session] Ghost check passed: Found profile identity marker")
                    return True
            except Exception:
                pass

            logger.warning("[Session] Ghost check FAILED: No authenticated signatures found")
            return False

        except Exception as e:
            logger.error(f"[Session] Verification crash: {e}")
            return False
        finally:
            page.remove_listener("response", handle_response)

    async def delete_session(self, user_id: str, domain: str) -> bool:
        """
        Delete a stored session.

        Args:
            user_id: Unique user identifier
            domain: Target domain

        Returns:
            True if session was deleted, False if not found
        """
        key = self._make_key(user_id, domain)

        try:
            deleted = await self._redis.delete(key)
            if deleted:
                logger.info(f"[Session] Deleted session for {user_id}@{domain}")
            return bool(deleted)
        except aioredis.RedisError as e:
            logger.error(f"[Session] Redis error deleting {user_id}@{domain}: {e}")
            return False

    async def has_session(self, user_id: str, domain: str) -> bool:
        """
        Check if a session exists (without decrypting).

        Args:
            user_id: Unique user identifier
            domain: Target domain

        Returns:
            True if session exists in Redis
        """
        key = self._make_key(user_id, domain)

        try:
            return await self._redis.exists(key) > 0
        except aioredis.RedisError:
            return False

    async def get_session_ttl(self, user_id: str, domain: str) -> int:
        """
        Get remaining TTL for a session.

        Returns:
            Remaining seconds, or -1 if no TTL, or -2 if key doesn't exist
        """
        key = self._make_key(user_id, domain)

        try:
            return await self._redis.ttl(key)
        except aioredis.RedisError:
            return -2


# Singleton instance for global access
_session_manager: Optional[SessionManager] = None


async def get_session_manager() -> Optional[SessionManager]:
    """
    Get or create SessionManager singleton.

    Returns None if Redis is not available or encryption key is missing.
    """
    global _session_manager

    if _session_manager is not None:
        return _session_manager

    try:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        redis_client = aioredis.from_url(redis_url)

        # Test connection
        await redis_client.ping()

        _session_manager = SessionManager(redis_client)
        return _session_manager

    except EncryptionKeyMissing:
        logger.warning("[SessionManager] Disabled - FERNET_KEY not configured")
        return None
    except Exception as e:
        logger.warning(f"[SessionManager] Disabled - Redis unavailable: {e}")
        return None
