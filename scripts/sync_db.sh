#!/bin/bash

# =============================================================================
# sync_db.sh - Database Schema Sync Script
# =============================================================================
# Purpose: Copy Prisma schema from frontend to Python service (Single Source of Truth)
# Usage: ./backend/scripts/sync_db.sh
# =============================================================================

set -e  # Exit on error

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}[Sync DB] Starting schema synchronization...${NC}"

# Paths (relative to project root)
FRONTEND_SCHEMA="frontend/prisma/schema.prisma"
PYTHON_SERVICE_DIR="backend/apps/execution-plane"
PYTHON_SCHEMA_DIR="$PYTHON_SERVICE_DIR/prisma"
PYTHON_SCHEMA="$PYTHON_SCHEMA_DIR/schema.prisma"

# Verify we're in project root
if [ ! -f "$FRONTEND_SCHEMA" ]; then
    echo -e "${RED}[Error] Frontend schema not found at $FRONTEND_SCHEMA${NC}"
    echo "Please run this script from the project root directory."
    exit 1
fi

# Create Python schema directory if it doesn't exist
mkdir -p "$PYTHON_SCHEMA_DIR"

# Copy schema
echo -e "${GREEN}[Sync DB] Copying schema: $FRONTEND_SCHEMA → $PYTHON_SCHEMA${NC}"
cp "$FRONTEND_SCHEMA" "$PYTHON_SCHEMA"

# Generate Prisma client for Python
echo -e "${GREEN}[Sync DB] Generating Prisma client for Python...${NC}"
cd "$PYTHON_SERVICE_DIR"

if command -v prisma &> /dev/null; then
    prisma generate --schema=prisma/schema.prisma
    echo -e "${GREEN}[Sync DB] ✓ Prisma client generated${NC}"
else
    echo -e "${YELLOW}[Warning] 'prisma' command not found. Skipping generation.${NC}"
    echo "Run 'pip install prisma' and then 'prisma generate' manually."
fi

# Return to project root
cd ../../../

# Print Go model reminder
echo ""
echo -e "${YELLOW}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${YELLOW}║  REMINDER: Go Structs (GORM)                                  ║${NC}"
echo -e "${YELLOW}╠════════════════════════════════════════════════════════════════╣${NC}"
echo -e "${YELLOW}║  If you modified the Prisma schema, you MUST update:          ║${NC}"
echo -e "${YELLOW}║  • backend/apps/control-plane/internal/models/                 ║${NC}"
echo -e "${YELLOW}║                                                                ║${NC}"
echo -e "${YELLOW}║  Go does not support auto-generation from Prisma.             ║${NC}"
echo -e "${YELLOW}║  Manually sync struct definitions.                            ║${NC}"
echo -e "${YELLOW}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

echo -e "${GREEN}[Sync DB] ✓ Schema sync complete${NC}"
