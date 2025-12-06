import sys
import os
# Fix imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from core.AccountManager import AccountManager
from dotenv import load_dotenv

# Load env to get FERNET_KEY
load_dotenv(os.path.join(os.path.dirname(__file__), "../../../.env"))

def seed():
    mgr = AccountManager()
    print("🔐 Seeding Account Vault...")

    # We will pretend 'httpbin.org' is a secure site
    mgr.add_account(
        domain="httpbin.org",
        username="admin",
        password="password123"
    )
    print("✅ Account for 'httpbin.org' added to CockroachDB.")

if __name__ == "__main__":
    seed()
