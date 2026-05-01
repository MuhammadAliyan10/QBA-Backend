# core/planning/siteAtlas.py
"""
Site Atlas — Element-level learning memory.

ARCHITECTURE:
=============
The Site Atlas stores interaction records at the ELEMENT level, not the recipe level.
This allows the system to build a compressed model of how websites work over time.

LEARNING FLYWHEEL:
==================
1. User 1 runs a task on airbnb.com → harvester extracts elements → execution succeeds.
2. SiteAtlas.learn() stores: {domain: "airbnb.com", intent: "price_filter", element: {role: "slider", ...}}.
3. User 2 runs a DIFFERENT task on airbnb.com → planner queries SiteAtlas → gets cached element map.
4. Planning cost for User 2: ZERO (no harvest needed for known domains).

STORAGE:
========
Uses PostgreSQL (pgvector) for semantic search and SQLAlchemy for CRUD.
Falls back to in-memory dict when DB is unavailable.
"""

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("siteAtlas")


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class InteractionRecord:
    """A single element interaction record."""
    domain: str
    page_path: str                      # URL path (e.g., "/search", "/login")
    intent: str                         # What the user wanted (e.g., "price_filter")
    element_tag: str                    # HTML tag (button, input, etc.)
    element_role: str                   # ARIA role
    element_label: str                  # Text / aria-label / placeholder
    element_selector: str               # CSS selector that worked
    interaction_pattern: str            # click, type_text, select_option, extract_text
    success_count: int = 0
    fail_count: int = 0
    last_verified_at: float = 0.0       # Unix timestamp

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return self.success_count / total if total > 0 else 0.0

    @property
    def reliability(self) -> str:
        rate = self.success_rate
        if rate >= 0.9:
            return "HIGH"
        elif rate >= 0.6:
            return "MEDIUM"
        return "LOW"


@dataclass
class SiteProfile:
    """Cached profile of a domain's known interaction patterns."""
    domain: str
    page_profiles: Dict[str, List[InteractionRecord]] = field(default_factory=dict)
    last_harvested_at: float = 0.0
    total_executions: int = 0


# =============================================================================
# SITE ATLAS
# =============================================================================

class SiteAtlas:
    """
    Element-level interaction memory.

    Stores what worked on which website so the planner can skip harvesting
    for known domains and provide higher-accuracy plans.
    """

    # Time-to-live: re-harvest if profile is older than 7 days
    PROFILE_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days

    def __init__(self):
        self._memory: Dict[str, SiteProfile] = {}
        self._db_available = False
        self._init_db()

    def _init_db(self):
        """Try to connect to PostgreSQL for persistent storage."""
        try:
            import os
            db_url = os.getenv("DATABASE_URL")
            if not db_url:
                logger.info("[SiteAtlas] No DATABASE_URL — using in-memory storage.")
                return

            from sqlalchemy import create_engine, text
            engine = create_engine(db_url)
            with engine.connect() as conn:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS site_atlas (
                        id SERIAL PRIMARY KEY,
                        domain TEXT NOT NULL,
                        page_path TEXT NOT NULL DEFAULT '/',
                        intent TEXT NOT NULL,
                        element_tag TEXT NOT NULL DEFAULT '',
                        element_role TEXT NOT NULL DEFAULT '',
                        element_label TEXT NOT NULL DEFAULT '',
                        element_selector TEXT NOT NULL DEFAULT '',
                        interaction_pattern TEXT NOT NULL DEFAULT 'click',
                        success_count INTEGER NOT NULL DEFAULT 0,
                        fail_count INTEGER NOT NULL DEFAULT 0,
                        last_verified_at DOUBLE PRECISION NOT NULL DEFAULT 0,
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW(),
                        UNIQUE(domain, page_path, intent)
                    )
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_site_atlas_domain
                    ON site_atlas(domain)
                """))
                conn.commit()

            self._engine = engine
            self._db_available = True
            logger.info("[SiteAtlas] Connected to PostgreSQL.")

        except Exception as e:
            logger.warning(f"[SiteAtlas] DB init failed (using in-memory): {e}")

    @staticmethod
    def _extract_domain(url: str) -> str:
        """Extract clean domain from URL."""
        parsed = urlparse(url)
        domain = parsed.netloc.replace("www.", "")
        return domain

    @staticmethod
    def _extract_path(url: str) -> str:
        """Extract path from URL (normalized)."""
        parsed = urlparse(url)
        path = parsed.path.rstrip("/") or "/"
        return path

    def query(self, url: str, intent: str) -> Optional[InteractionRecord]:
        """
        Look up a known interaction for a domain + intent.

        Args:
            url: Full URL or domain.
            intent: What the user wants (e.g., "search input", "price filter").

        Returns:
            InteractionRecord if found and still fresh, else None.
        """
        domain = self._extract_domain(url)
        path = self._extract_path(url)
        intent_lower = intent.lower().strip()

        # Try DB first
        if self._db_available:
            try:
                return self._query_db(domain, path, intent_lower)
            except Exception as e:
                logger.debug(f"[SiteAtlas] DB query failed: {e}")

        # Fallback to in-memory
        profile = self._memory.get(domain)
        if not profile:
            return None

        records = profile.page_profiles.get(path, [])
        for record in records:
            if record.intent == intent_lower:
                # Check freshness
                age = time.time() - record.last_verified_at
                if age < self.PROFILE_TTL_SECONDS:
                    return record

        return None

    def query_site_profile(self, url: str) -> Optional[SiteProfile]:
        """Get the full site profile for a domain (all known interactions)."""
        domain = self._extract_domain(url)

        if self._db_available:
            try:
                return self._load_profile_db(domain)
            except Exception as e:
                logger.debug(f"[SiteAtlas] DB profile load failed: {e}")

        return self._memory.get(domain)

    def learn(
        self,
        url: str,
        intent: str,
        element_tag: str,
        element_role: str,
        element_label: str,
        element_selector: str,
        interaction_pattern: str,
        success: bool,
    ):
        """
        Record an interaction result for future planning.

        Called after each goal execution in GoalExecutor.
        Successful interactions increase the element's reliability score.

        Args:
            url: Full URL where interaction happened.
            intent: What the user wanted.
            element_tag: HTML tag of the element that was used.
            element_role: ARIA role.
            element_label: Text label of the element.
            element_selector: CSS selector that resolved.
            interaction_pattern: Action type (click, type_text, etc.).
            success: Whether the interaction succeeded.
        """
        domain = self._extract_domain(url)
        path = self._extract_path(url)
        intent_lower = intent.lower().strip()

        # Persist to DB
        if self._db_available:
            try:
                self._upsert_db(
                    domain, path, intent_lower,
                    element_tag, element_role, element_label,
                    element_selector, interaction_pattern, success,
                )
                return
            except Exception as e:
                logger.debug(f"[SiteAtlas] DB upsert failed: {e}")

        # Fallback to in-memory
        if domain not in self._memory:
            self._memory[domain] = SiteProfile(domain=domain)

        profile = self._memory[domain]
        if path not in profile.page_profiles:
            profile.page_profiles[path] = []

        # Find existing record
        existing = None
        for record in profile.page_profiles[path]:
            if record.intent == intent_lower:
                existing = record
                break

        if existing:
            if success:
                existing.success_count += 1
            else:
                existing.fail_count += 1
            existing.last_verified_at = time.time()
            existing.element_selector = element_selector
        else:
            profile.page_profiles[path].append(InteractionRecord(
                domain=domain,
                page_path=path,
                intent=intent_lower,
                element_tag=element_tag,
                element_role=element_role,
                element_label=element_label,
                element_selector=element_selector,
                interaction_pattern=interaction_pattern,
                success_count=1 if success else 0,
                fail_count=0 if success else 1,
                last_verified_at=time.time(),
            ))

        profile.total_executions += 1

    def is_domain_known(self, url: str) -> bool:
        """Check if we have any records for this domain."""
        domain = self._extract_domain(url)

        if self._db_available:
            try:
                from sqlalchemy import text
                with self._engine.connect() as conn:
                    result = conn.execute(
                        text("SELECT COUNT(*) FROM site_atlas WHERE domain = :d"),
                        {"d": domain}
                    )
                    return result.scalar() > 0
            except Exception:
                pass

        return domain in self._memory

    # =========================================================================
    # DATABASE OPERATIONS
    # =========================================================================

    def _query_db(self, domain: str, path: str, intent: str) -> Optional[InteractionRecord]:
        """Query PostgreSQL for a specific interaction record."""
        from sqlalchemy import text
        with self._engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT * FROM site_atlas
                    WHERE domain = :domain AND page_path = :path AND intent = :intent
                    AND last_verified_at > :ttl_cutoff
                    LIMIT 1
                """),
                {
                    "domain": domain,
                    "path": path,
                    "intent": intent,
                    "ttl_cutoff": time.time() - self.PROFILE_TTL_SECONDS,
                }
            )
            row = result.mappings().fetchone()
            if not row:
                return None

            return InteractionRecord(
                domain=row["domain"],
                page_path=row["page_path"],
                intent=row["intent"],
                element_tag=row["element_tag"],
                element_role=row["element_role"],
                element_label=row["element_label"],
                element_selector=row["element_selector"],
                interaction_pattern=row["interaction_pattern"],
                success_count=row["success_count"],
                fail_count=row["fail_count"],
                last_verified_at=row["last_verified_at"],
            )

    def _load_profile_db(self, domain: str) -> Optional[SiteProfile]:
        """Load a full site profile from PostgreSQL."""
        from sqlalchemy import text
        with self._engine.connect() as conn:
            result = conn.execute(
                text("SELECT * FROM site_atlas WHERE domain = :domain"),
                {"domain": domain}
            )
            rows = result.mappings().fetchall()
            if not rows:
                return None

            profile = SiteProfile(domain=domain)
            for row in rows:
                path = row["page_path"]
                if path not in profile.page_profiles:
                    profile.page_profiles[path] = []

                profile.page_profiles[path].append(InteractionRecord(
                    domain=row["domain"],
                    page_path=row["page_path"],
                    intent=row["intent"],
                    element_tag=row["element_tag"],
                    element_role=row["element_role"],
                    element_label=row["element_label"],
                    element_selector=row["element_selector"],
                    interaction_pattern=row["interaction_pattern"],
                    success_count=row["success_count"],
                    fail_count=row["fail_count"],
                    last_verified_at=row["last_verified_at"],
                ))
                profile.total_executions += row["success_count"] + row["fail_count"]

            return profile

    def _upsert_db(
        self,
        domain: str, path: str, intent: str,
        element_tag: str, element_role: str, element_label: str,
        element_selector: str, interaction_pattern: str, success: bool,
    ):
        """Upsert an interaction record in PostgreSQL."""
        from sqlalchemy import text
        with self._engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO site_atlas (domain, page_path, intent, element_tag, element_role,
                        element_label, element_selector, interaction_pattern,
                        success_count, fail_count, last_verified_at)
                    VALUES (:domain, :path, :intent, :tag, :role, :label, :selector, :pattern,
                        :sc, :fc, :ts)
                    ON CONFLICT (domain, page_path, intent) DO UPDATE SET
                        element_tag = :tag,
                        element_role = :role,
                        element_label = :label,
                        element_selector = :selector,
                        interaction_pattern = :pattern,
                        success_count = site_atlas.success_count + :sc,
                        fail_count = site_atlas.fail_count + :fc,
                        last_verified_at = :ts,
                        updated_at = NOW()
                """),
                {
                    "domain": domain, "path": path, "intent": intent,
                    "tag": element_tag, "role": element_role, "label": element_label,
                    "selector": element_selector, "pattern": interaction_pattern,
                    "sc": 1 if success else 0, "fc": 0 if success else 1,
                    "ts": time.time(),
                }
            )
            conn.commit()
