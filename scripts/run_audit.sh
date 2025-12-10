#!/bin/bash
# Re-Audit Execution Script
# Run this to execute the investor audit test with hybrid semantic scoring

set -e  # Exit on error

echo "=============================================================================="
echo "🎯 INVESTOR AUDIT - Re-Audit with Hybrid Semantic Scoring"
echo "=============================================================================="
echo ""

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Step 1: Ensure recipe is seeded
echo -e "${BLUE}[1/4] Seeding investor audit recipe...${NC}"
cd /Users/muhammadaliyan/Programming/Artificial\ Intelligence/e2e-Backend
source apps/execution-plane/venv/bin/activate
python3 scripts/audit_test.py
echo -e "${GREEN}✅ Recipe seeded${NC}"
echo ""

# Step 2: Instructions for starting control plane
echo -e "${YELLOW}[2/4] Start Control Plane (Terminal 1):${NC}"
echo "   cd /Users/muhammadaliyan/Programming/Artificial\\ Intelligence/e2e-Backend/apps/control-plane"
echo "   go run cmd/server/main.go"
echo ""
echo -e "${YELLOW}Press ENTER when control plane is running...${NC}"
read

# Step 3: Instructions for starting execution plane
echo -e "${YELLOW}[3/4] Start Execution Plane (Terminal 2):${NC}"
echo "   cd /Users/muhammadaliyan/Programming/Artificial\\ Intelligence/e2e-Backend/apps/execution-plane"
echo "   source venv/bin/activate"
echo "   python src/worker.py"
echo ""
echo -e "${YELLOW}Press ENTER when worker is running...${NC}"
read

# Step 4: Trigger the audit
echo -e "${BLUE}[4/4] Triggering investor audit...${NC}"
echo ""

RESPONSE=$(curl -s -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"workflow_id": "investor_audit_v1"}')

echo -e "${GREEN}Response:${NC}"
echo "$RESPONSE" | python3 -m json.tool
echo ""

# Extract job_id
JOB_ID=$(echo "$RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['job_id'])" 2>/dev/null || echo "")

if [ -n "$JOB_ID" ]; then
    echo -e "${GREEN}✅ Audit triggered successfully!${NC}"
    echo ""
    echo -e "${BLUE}Monitor progress:${NC}"
    echo "   wscat -c \"ws://localhost:8080/ws?job_id=$JOB_ID\""
    echo ""
    echo -e "${YELLOW}Watch for Step 4: 'link to the person who runs the company'${NC}"
    echo -e "${YELLOW}Expected: Score > 0.60 with METHOD: SEMANTIC${NC}"
else
    echo -e "${YELLOW}⚠️  Could not extract job_id. Check response above.${NC}"
fi

echo ""
echo "=============================================================================="
