"""
AccountManager - Account Pool Manager for Session Rehydration

Manages a shared pool of authentication accounts with:
- Atomic locking (FOR UPDATE SKIP LOCKED) to prevent race conditions
- Fernet encryption for passwords at rest
- Session rehydration via cookie injection
- Smart prioritization (accounts with cookies first)

Usage:
    mgr = AccountManager()
    account = mgr.lease_account("example.com")
    if account:
        # Use account
        mgr.release_account(account['id'], new_cookies, success=True)
"""

import os
import logging
import json
import uuid
import asyncio
from typing import Dict, Optional, Tuple
from datetime import datetime
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from cryptography.fernet import Fernet
import redis.asyncio as aioredis

logger = logging.getLogger("accountManager")


class SessionHydrationTimeout(Exception):
    """Raised when waiting for a session lock exceeds the maximum threshold."""
    pass


class AccountManager:
    """
    The Account Pool Manager.
    Provides atomic account leasing with encryption and session rehydration.
    """

    def __init__(self):
        """
        Initialize AccountManager with database connection and encryption.

        Requires environment variables:
            - DATABASE_URL: PostgreSQL (Supabase) connection string
            - FERNET_KEY: Encryption key (generate with Fernet.generate_key())
        """
        # Database connection
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            raise ValueError("DATABASE_URL environment variable not set")

        # Force asyncpg driver for non-blocking I/O in async worker
        if db_url.startswith("postgresql://"):
            db_url = db_url.replace("postgresql://", "postgresql+asyncpg://")

        # Create engine with connection pooling
        self.engine = create_async_engine(
            db_url,
            pool_pre_ping=True,  # Verify connections before use
            pool_size=5,
            max_overflow=10
        )

        # Encryption setup
        fernet_key = os.getenv("FERNET_KEY")
        if not fernet_key:
            logger.warning("FERNET_KEY not set. Generating temporary key (NOT SAFE FOR PRODUCTION)")
            fernet_key = Fernet.generate_key().decode()

        self.cipher = Fernet(fernet_key.encode() if isinstance(fernet_key, str) else fernet_key)

        # Redis for distributed locking
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        self.redis = aioredis.from_url(redis_url, decode_responses=True)

        # Lua script for safe lock release: only delete if UUID matches
        self._release_lua = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """

        logger.info("AccountManager initialized with Redis locking and asyncpg")

    def _encrypt_password(self, plaintext: str) -> str:
        """
        Encrypt password using Fernet.

        Args:
            plaintext: Plain text password

        Returns:
            Base64-encoded encrypted password
        """
        encrypted_bytes = self.cipher.encrypt(plaintext.encode())
        return encrypted_bytes.decode()

    def _decrypt_password(self, encrypted: str) -> str:
        """
        Decrypt password using Fernet.

        Args:
            encrypted: Base64-encoded encrypted password

        Returns:
            Plain text password
        """
        decrypted_bytes = self.cipher.decrypt(encrypted.encode())
        return decrypted_bytes.decode()

    async def lease_account(self, domain: str) -> Optional[Dict]:
        """
        Atomically lease an available account for the specified domain.

        Uses PostgreSQL's FOR UPDATE SKIP LOCKED to prevent race conditions.
        Prioritizes accounts with cookies (Fast Path).

        Args:
            domain: Domain name (e.g., "example.com")

        Returns:
            Dictionary with account data or None if no accounts available
        """
        try:
            async with self.engine.begin() as conn:
                # Atomic SELECT + UPDATE with SKIP LOCKED
                # Prioritize accounts with cookies first
                query = text("""
                    SELECT id, username, password_encrypted, cookies, domain
                    FROM account_pool
                    WHERE domain = :domain
                      AND status = 'AVAILABLE'
                      AND (expires_at IS NULL OR expires_at > NOW())
                    ORDER BY
                        (cookies IS NOT NULL) DESC,  -- Prioritize accounts with cookies
                        last_used_at ASC NULLS FIRST
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                """)

                result = await conn.execute(query, {"domain": domain})
                row = result.fetchone()

                if not row:
                    logger.warning(f"[AccountManager] No available accounts for domain: {domain}")
                    return None

                account_id, username, password_enc, cookies_json, domain_name = row

                # Update status immediately while we hold the lock
                update_query = text("""
                    UPDATE account_pool
                    SET status = 'LEASED',
                        leased_at = NOW(),
                        last_used_at = NOW()
                    WHERE id = :account_id
                """)

                await conn.execute(update_query, {"account_id": account_id})

                # Decrypt password
                password = self._decrypt_password(password_enc)

                # Parse cookies if they exist
                cookies = None
                if cookies_json:
                    try:
                        cookies = json.loads(cookies_json) if isinstance(cookies_json, str) else cookies_json
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(f"[AccountManager] Invalid cookie JSON for account {account_id}")

                account_data = {
                    "id": str(account_id),
                    "username": username,
                    "password": password,
                    "cookies": cookies,
                    "domain": domain_name
                }

                logger.info(f"[Security] Leased account '{username}' for {domain} (ID: {account_id})")
                return account_data

        except Exception as e:
            logger.error(f"[AccountManager] Failed to lease account for {domain}: {e}", exc_info=True)
            return None

    # --- DISTRIBUTED AUTH LOCK (Post-Thundering Herd) ---

    async def acquire_auth_lock(
        self,
        account_id: str,
        domain: str,
        timeout: int = 60,
        max_wait: int = 90
    ) -> tuple[bool, Optional[str]]:
        """
        Attempt to acquire a distributed lock for authentication.

        Logic:
        - Leader: Acquires lock (SETNX), returns (True, lock_uuid).
        - Follower: Fails lock, enters bounded polling loop (2s intervals).

        Args:
            account_id: The account being authenticated
            domain: The target portal domain
            timeout: Lock TTL in seconds (prevent deadlocks)
            max_wait: Max seconds to wait as a follower before timing out

        Returns:
            Tuple: (is_leader, lock_uuid)

        Raises:
            SessionHydrationTimeout: If the 90s polling limit is exceeded.
        """
        lock_key = f"auth_lock:{account_id}:{domain}"
        lock_uuid = str(uuid.uuid4())

        # Attempt to become Leader
        acquired = await self.redis.set(lock_key, lock_uuid, nx=True, ex=timeout)

        if acquired:
            logger.info(f"[AuthLock] LEADER | Job acquired lock for {account_id}@{domain}")
            return True, lock_uuid

        # Become Follower: Bounded Polling
        logger.info(f"[AuthLock] FOLLOWER | Waiting for active lock on {account_id}@{domain}")
        start_time = asyncio.get_event_loop().time()

        while (asyncio.get_event_loop().time() - start_time) < max_wait:
            await asyncio.sleep(2)

            # Check if lock still exists
            exists = await self.redis.exists(lock_key)
            if not exists:
                logger.info(f"[AuthLock] RESOLVED | Lock released for {account_id}@{domain}")
                return False, None

        # Polling threshold exceeded
        logger.error(f"[AuthLock] TIMEOUT | Follower waited >{max_wait}s for {account_id}@{domain}")
        raise SessionHydrationTimeout(f"Timed out waiting for Leader to finish auth for {account_id}@{domain}")

    async def release_auth_lock(self, account_id: str, domain: str, lock_uuid: str):
        """
        Atomically release the auth lock using Lua to verify ownership.
        """
        lock_key = f"auth_lock:{account_id}:{domain}"

        try:
            # Execute Lua script for safe deletion
            result = await self.redis.eval(self._release_lua, 1, lock_key, lock_uuid)

            if result:
                logger.info(f"[AuthLock] RELEASED | Success for {account_id}@{domain}")
            else:
                logger.warning(f"[AuthLock] RELEASE_FAILED | UUID mismatch or lock expired for {account_id}@{domain}")

        except Exception as e:
            logger.error(f"[AuthLock] ERROR | Failed to release lock: {e}")



    def release_account(
        self,
        account_id: str,
        new_cookies: Optional[Dict] = None,
        success: bool = True
    ):
        """
        Release account back to the pool with updated state.

        Args:
            account_id: UUID of the account
            new_cookies: Updated cookies to store (if any)
            success: Whether the operation was successful

        Note:
            - If success=True, status set to AVAILABLE
            - If success=False, status set to NEEDS_CHECK
            - Always updates last_used_at timestamp
        """
        try:
            with self.engine.begin() as conn:
                # Determine new status
                new_status = "AVAILABLE" if success else "NEEDS_CHECK"

                # Serialize cookies to JSON
                cookies_json = json.dumps(new_cookies) if new_cookies else None

                query = text("""
                    UPDATE account_pool
                    SET
                        status = :status,
                        last_used_at = NOW(),
                        cookies = COALESCE(:cookies::jsonb, cookies),  -- Update if provided
                        leased_at = NULL,
                        updated_at = NOW()
                    WHERE id = :account_id
                """)

                conn.execute(query, {
                    "account_id": account_id,
                    "status": new_status,
                    "cookies": cookies_json
                })

                logger.info(f"Released account {account_id} (status: {new_status}, cookies: {'updated' if new_cookies else 'unchanged'})")

        except Exception as e:
            logger.error(f"Failed to release account {account_id}: {e}", exc_info=True)

    async def add_account(
        self,
        domain: str,
        username: str,
        password: str,
        cookies: Optional[Dict] = None
    ) -> Optional[str]:
        """
        Add a new account to the pool.

        Args:
            domain: Domain name
            username: Username
            password: Plain text password (will be encrypted)
            cookies: Optional cookies dictionary

        Returns:
            Account ID (UUID) if successful, None otherwise
        """
        try:
            async with self.engine.begin() as conn:
                # Encrypt password
                password_encrypted = self._encrypt_password(password)

                # Serialize cookies
                cookies_json = json.dumps(cookies) if cookies else None

                query = text("""
                    INSERT INTO account_pool
                        (domain, username, password_encrypted, cookies, status)
                    VALUES
                        (:domain, :username, :password_encrypted, :cookies::jsonb, 'AVAILABLE')
                    ON CONFLICT (domain, username)
                    DO UPDATE SET
                        password_encrypted = EXCLUDED.password_encrypted,
                        cookies = EXCLUDED.cookies,
                        updated_at = NOW()
                    RETURNING id
                """)

                result = await conn.execute(query, {
                    "domain": domain,
                    "username": username,
                    "password_encrypted": password_encrypted,
                    "cookies": cookies_json
                })

                account_id = result.fetchone()[0]
                logger.info(f"Added/Updated account: {username}@{domain}")
                return str(account_id)

        except Exception as e:
            logger.error(f"Failed to add account: {e}", exc_info=True)
            return None

    async def update_success_rate(self, account_id: str, success: bool):
        """
        Update account success rate using exponential moving average.

        Args:
            account_id: UUID of the account
            success: Whether the last operation succeeded
        """
        try:
            async with self.engine.begin() as conn:
                # Exponential moving average: new_rate = 0.9 * old_rate + 0.1 * current
                weight = 0.1
                current_value = 1.0 if success else 0.0

                query = text("""
                    UPDATE account_pool
                    SET success_rate = (success_rate * :decay + :current * :weight)
                    WHERE id = :account_id
                """)

                await conn.execute(query, {
                    "account_id": account_id,
                    "decay": 1 - weight,
                    "weight": weight,
                    "current": current_value
                })

        except Exception as e:
            logger.error(f"Failed to update success rate: {e}", exc_info=True)


# Example usage
if __name__ == "__main__":
    """
    Setup and testing script.
    """
    import sys

    # Generate a Fernet key if needed
    if len(sys.argv) > 1 and sys.argv[1] == "generate-key":
        key = Fernet.generate_key()
        print(f"Generated Fernet Key:\n{key.decode()}")
        print("\nAdd this to your .env file:")
        print(f"FERNET_KEY={key.decode()}")
        sys.exit(0)

    # Test the account manager
    try:
        mgr = AccountManager()

        # Add a test account
        account_id = mgr.add_account(
            domain="example.com",
            username="testuser",
            password="secret123",
            cookies={"session": "abc123"}
        )
        print(f"Added account: {account_id}")

        # Lease the account
        account = mgr.lease_account("example.com")
        if account:
            print(f"Leased account: {account['username']}")
            print(f"Has cookies: {account['cookies'] is not None}")

            # Release it
            mgr.release_account(account['id'], success=True)
            print("Released account")
        else:
            print("No accounts available")

    except Exception as e:
        print(f"Error: {e}")
