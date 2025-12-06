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
from typing import Dict, Optional
from datetime import datetime
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from cryptography.fernet import Fernet

logger = logging.getLogger("accountManager")


class AccountManager:
    """
    The Account Pool Manager.
    Provides atomic account leasing with encryption and session rehydration.
    """

    def __init__(self):
        """
        Initialize AccountManager with database connection and encryption.

        Requires environment variables:
            - DATABASE_URL: PostgreSQL/CockroachDB connection string
            - FERNET_KEY: Encryption key (generate with Fernet.generate_key())
        """
        # Database connection
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            raise ValueError("DATABASE_URL environment variable not set")

        # Create engine with connection pooling
        self.engine = create_engine(
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

        logger.info("AccountManager initialized")

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

    def lease_account(self, domain: str) -> Optional[Dict]:
        """
        Atomically lease an available account for the specified domain.

        Uses PostgreSQL's FOR UPDATE SKIP LOCKED to prevent race conditions.
        Prioritizes accounts with cookies (Fast Path).

        Args:
            domain: Domain name (e.g., "example.com")

        Returns:
            Dictionary with account data:
                {
                    'id': UUID,
                    'username': str,
                    'password': str (decrypted),
                    'cookies': dict or None
                }
            Returns None if no accounts available.
        """
        try:
            with self.engine.begin() as conn:
                # Atomic SELECT + UPDATE with SKIP LOCKED
                # Prioritize accounts with cookies first
                query = text("""
    def lease_account(self, domain: str) -> dict | None:
        """
        Atomically lease an available account for a specific domain.

        CRITICAL: Uses FOR UPDATE SKIP LOCKED to prevent race conditions.
        This ensures two concurrent jobs NEVER get the same account.

        Race Condition Protection:
        - Job A queries: SELECT ... FOR UPDATE SKIP LOCKED
        - Job B queries simultaneously: SKIPS locked row, gets next account
        - No collision possible!

        Args:
            domain: Target domain (e.g., "amazon.com")

        Returns:
            Account dict with credentials and cookies, or None if pool empty
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        try:
            # ATOMIC LEASE: Lock the row immediately
            cursor.execute("""
                SELECT id, username, password, cookies, domain
                FROM accounts
                WHERE domain = ?
                  AND status = 'AVAILABLE'
                  AND (expires_at IS NULL OR expires_at > datetime('now'))
                ORDER BY last_used ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            """, (domain,))

            row = cursor.fetchone()

            if not row:
                conn.close()
                logger.warning(f"[AccountManager] No available accounts for domain: {domain}")
                return None

            account_id, username, password, cookies_json, domain = row

            # Update status immediately while we hold the lock
            cursor.execute("""
                UPDATE accounts
                SET status = 'LEASED',
                    leased_at = datetime('now'),
                    last_used = datetime('now')
                WHERE id = ?
            """, (account_id,))

            conn.commit()

            # Parse cookies if they exist
            cookies = None
            if cookies_json:
                try:
                    cookies = json.loads(cookies_json)
                except json.JSONDecodeError:
                    logger.warning(f"[AccountManager] Invalid cookie JSON for account {account_id}")

            account_data = {
                "id": account_id,
                "username": username,
                "password": password,
                "cookies": cookies,
                "domain": domain
            }

            logger.info(f"[Security] Leased account '{username}' for {domain} (ID: {account_id})")
            return account_data

        except Exception as e:
            conn.rollback()
            logger.error(f"[AccountManager] Failed to lease account for {domain}: {e}")
            return None
        finally:
            conn.close()

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

    def add_account(
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
            with self.engine.begin() as conn:
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

                result = conn.execute(query, {
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

    def update_success_rate(self, account_id: str, success: bool):
        """
        Update account success rate using exponential moving average.

        Args:
            account_id: UUID of the account
            success: Whether the last operation succeeded
        """
        try:
            with self.engine.begin() as conn:
                # Exponential moving average: new_rate = 0.9 * old_rate + 0.1 * current
                weight = 0.1
                current_value = 1.0 if success else 0.0

                query = text("""
                    UPDATE account_pool
                    SET success_rate = (success_rate * :decay + :current * :weight)
                    WHERE id = :account_id
                """)

                conn.execute(query, {
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
