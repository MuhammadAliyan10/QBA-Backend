"""
Admin Script: Generate and Store API Key

Creates a new API key with SHA-256 hashing and stores it in PostgreSQL.
The plaintext key is shown ONCE and should be given to the user immediately.

Usage:
    python scripts/admin_create_key.py --user-id <uuid> --name "Production Key"
"""

import sys
import os
import secrets
import hashlib
import uuid
import psycopg2
from datetime import datetime

# Database connection (use environment variable or default)
DB_DSN = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/quanta")


def generate_api_key(prefix="sk_live"):
    """
    Generate a secure random API key.

    Format: sk_live_<32-char-random-string>

    Args:
        prefix: Key prefix (sk_live or sk_test)

    Returns:
        Plaintext API key
    """
    random_part = secrets.token_urlsafe(32)
    return f"{prefix}_{random_part}"


def hash_api_key(api_key: str) -> str:
    """
    Hash API key using SHA-256.

    Args:
        api_key: Plaintext API key

   Returns:
        Hexadecimal hash string
    """
    return hashlib.sha256(api_key.encode()).hexdigest()


def create_api_key(user_id: str, name: str, prefix="sk_live"):
    """
    Generate and store API key in database.

    Args:
        user_id: UUID of the user
        name: Human-readable name for the key
        prefix: Key prefix (sk_live or sk_test)

    Returns:
        Tuple of (plaintext_key, key_id)
    """
    # Generate key
    api_key = generate_api_key(prefix)
    key_hash = hash_api_key(api_key)
    key_prefix = api_key[:16] + "..."  # First 16 chars for display

    # Connect to database
    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()

    try:
        # Insert into api_keys table
        cur.execute("""
            INSERT INTO api_keys (user_id, key_hash, key_prefix, name, active, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            user_id,
            key_hash,
            key_prefix,
            name,
            True,
            datetime.now()
        ))

        key_id = cur.fetchone()[0]
        conn.commit()

        return (api_key, key_id)

    finally:
        cur.close()
        conn.close()


def main():
    """Main CLI interface."""
    import argparse

    parser = argparse.ArgumentParser(description="Generate and store API key")
    parser.add_argument("--user-id", required=True, help="User UUID")
    parser.add_argument("--name", required=True, help="Key name (e.g., 'Production Key')")
    parser.add_argument("--prefix", default="sk_live", choices=["sk_live", "sk_test"], help="Key prefix")

    args = parser.parse_args()

    # Validate user-id is a valid UUID
    try:
        uuid.UUID(args.user_id)
    except ValueError:
        print(f"❌ Error: Invalid user-id format. Must be a valid UUID.")
        sys.exit(1)

    print("=" * 60)
    print("API KEY GENERATION")
    print("=" * 60)
    print(f"\nUser ID: {args.user_id}")
    print(f"Key Name: {args.name}")
    print(f"Prefix: {args.prefix}")
    print("\nGenerating key...")

    try:
        api_key, key_id = create_api_key(args.user_id, args.name, args.prefix)

        print("\n" + "=" * 60)
        print("✅ API KEY CREATED SUCCESSFULLY")
        print("=" * 60)
        print(f"\n🔑 API Key (SHOW THIS ONLY ONCE):")
        print(f"\n    {api_key}\n")
        print("=" * 60)
        print("\n⚠️  IMPORTANT:")
        print("   - Save this key immediately")
        print("   - It will NOT be shown again")
        print("   - Store it securely (password manager/env variable)")
        print("\n📊 Key Details:")
        print(f"   - Key ID: {key_id}")
        print(f"   - Display: {api_key[:16]}...")
        print(f"   - Status: Active")
        print("=" * 60)

    except psycopg2.Error as e:
        print(f"\n❌ Database Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
