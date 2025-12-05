import sqlite3
import logging
import os
from datetime import datetime

logger = logging.getLogger("patternDB")


class PatternDB:
    """
    Muscle Memory Pattern Database.

    Caches successful element selectors based on page structural fingerprints.
    Uses structural hashing (DOM tags, not text) to avoid cache misses on
    dynamic content (prices, timestamps, news headlines).

    Database Schema:
    - domain: Site hostname (e.g., amazon.com)
    - intent: User action (e.g., "search", "login")
    - page_simhash: Structural hash of DOM tag sequence
    - selector: CSS selector that worked
    - success_count: Number of successful uses
    - last_updated: Timestamp of last use
    """

    def __init__(self, db_path="patterns.db"):
        """
        Initialize Pattern Database.

        Args:
            db_path (str): Relative path to sqlite database file
        """
        # Ensure we are in the correct directory (execution-plane root)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.db_path = os.path.join(base_dir, "../../", db_path)
        logger.info(f"🗄️ Initializing PatternDB at: {self.db_path}")
        self._init_db()

    def _get_conn(self):
        """
        Get a new database connection.
        Thread-safe: Each operation gets its own connection.
        """
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        """
        Create the patterns table if it doesn't exist.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL,
                intent TEXT NOT NULL,
                page_simhash TEXT NOT NULL,
                selector TEXT NOT NULL,
                success_count INTEGER DEFAULT 1,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(domain, intent, page_simhash)
            )
        """)
        conn.commit()
        conn.close()
        logger.debug("✅ PatternDB schema initialized")

    def get_pattern(self, domain: str, page_simhash: str, intent: str) -> str | None:
        """
        Retrieve cached selector for a specific page structure and intent.

        Args:
            domain (str): Site hostname (e.g., "amazon.com")
            page_simhash (str): Structural hash of the page
            intent (str): User action (e.g., "search")

        Returns:
            str | None: Cached CSS selector if found, None otherwise
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        # Look for exact hash match, ordered by success_count
        cursor.execute("""
            SELECT selector FROM patterns
            WHERE domain = ? AND intent = ? AND page_simhash = ?
            ORDER BY success_count DESC
            LIMIT 1
        """, (domain, intent, page_simhash))

        row = cursor.fetchone()
        conn.close()

        if row:
            logger.info(f"🧠 Memory Hit: Found cached selector for '{intent}' on {domain}")
            return row[0]

        logger.debug(f"💭 Memory Miss: No pattern for '{intent}' on {domain}")
        return None

    def save_pattern(self, domain: str, page_simhash: str, intent: str, selector: str):
        """
        Save or update a successful selector pattern.

        Uses UPSERT logic:
        - If pattern exists: Increment success_count
        - If new: Insert with success_count = 1

        Args:
            domain (str): Site hostname
            page_simhash (str): Structural hash of the page
            intent (str): User action
            selector (str): CSS selector that worked
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        # Upsert Logic: If exists, increment success_count. If new, insert.
        cursor.execute("""
            INSERT INTO patterns (domain, intent, page_simhash, selector, success_count, last_updated)
            VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(domain, intent, page_simhash)
            DO UPDATE SET
                success_count = success_count + 1,
                last_updated = CURRENT_TIMESTAMP,
                selector = excluded.selector
        """, (domain, intent, page_simhash, selector))

        conn.commit()
        conn.close()
        logger.info(f"📝 Memory Saved: Linked '{intent}' to selector '{selector[:50]}...'")


# ==================== VERIFICATION BLOCK ====================
if __name__ == "__main__":
    """
    Standalone Test: Verify PatternDB CRUD operations.
    """
    import tempfile
    import os

    print("=" * 60)
    print("PATTERNDB - STANDALONE VERIFICATION")
    print("=" * 60)

    # Create temporary database
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    temp_db.close()

    try:
        print(f"\n[Test 1] Database Creation...")
        db = PatternDB(db_path=temp_db.name)
        print("✅ Database initialized")

        print("\n[Test 2] Save Pattern...")
        db.save_pattern(
            domain="amazon.com",
            page_simhash="12345678901234567890",
            intent="search",
            selector="#twotabsearchtextbox"
        )
        print("✅ Pattern saved")

        print("\n[Test 3] Retrieve Pattern (Hit)...")
        result = db.get_pattern("amazon.com", "12345678901234567890", "search")
        print(f"✅ Retrieved: {result}")
        assert result == "#twotabsearchtextbox", "Selector mismatch!"

        print("\n[Test 4] Retrieve Pattern (Miss)...")
        result = db.get_pattern("amazon.com", "99999999999999999999", "search")
        print(f"✅ Result: {result} (Expected: None)")
        assert result is None, "Should return None for cache miss!"

        print("\n[Test 5] Update Pattern (Increment Count)...")
        db.save_pattern("amazon.com", "12345678901234567890", "search", "#twotabsearchtextbox")
        db.save_pattern("amazon.com", "12345678901234567890", "search", "#twotabsearchtextbox")

        # Verify success_count incremented
        conn = db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT success_count FROM patterns WHERE domain = 'amazon.com'")
        count = cursor.fetchone()[0]
        conn.close()
        print(f"✅ Success count: {count} (Expected: 3)")
        assert count == 3, "Success count should be 3!"

        print("\n" + "=" * 60)
        print("ALL TESTS PASSED ✅")
        print("=" * 60)

    finally:
        # Cleanup
        os.unlink(temp_db.name)
