#!/usr/bin/env python3
"""
Fernet Key Generator

Generates a valid Fernet encryption key for session storage.
Add the output to your .env file as FERNET_KEY.

Usage:
    python scripts/generate_fernet_key.py

    # Or directly:
    python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
"""

from cryptography.fernet import Fernet


def generate_key() -> str:
    """Generate a new Fernet encryption key."""
    return Fernet.generate_key().decode()


if __name__ == "__main__":
    key = generate_key()
    print("\n" + "=" * 60)
    print("FERNET KEY GENERATED")
    print("=" * 60)
    print(f"\nFERNET_KEY={key}\n")
    print("Add this line to your .env file.")
    print("Keep this key SECRET and NEVER commit it to version control.")
    print("=" * 60 + "\n")
