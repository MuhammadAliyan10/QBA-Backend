"""
db.py - Prisma ORM Database Client

Single Source of Truth: frontend/prisma/schema.prisma
Synced via: backend/scripts/sync_db.sh

Usage:
    from db import prisma, connect_db, disconnect_db

    await connect_db()
    users = await prisma.userprofile.find_many()
    await disconnect_db()
"""

import logging
from prisma import Prisma

logger = logging.getLogger("db")

# Global Prisma client instance
prisma = Prisma()

async def connect_db():
    """
    Connect to the database using Prisma.

    Raises:
        Exception: If connection fails
    """
    try:
        await prisma.connect()
        logger.info("[DB] ✓ Connected to database (Prisma)")
    except Exception as e:
        logger.error(f"[DB] Failed to connect: {e}")
        raise

async def disconnect_db():
    """
    Disconnect from the database.
    """
    try:
        await prisma.disconnect()
        logger.info("[DB] Disconnected from database")
    except Exception as e:
        logger.warning(f"[DB] Disconnect warning: {e}")
