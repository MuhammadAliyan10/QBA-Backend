#!/bin/bash
# =============================================================================
# GIT HISTORY SECURITY SCANNER
# =============================================================================
# Scans git history for accidental commits of secrets (.env, .pem, etc.)
# Checks .git directory size for binary bloat.
# =============================================================================

echo "🔍 Starting Security Scan..."
echo "=============================="

# 1. Check for Secrets in History
# -----------------------------------------------------------------------------
echo "Checking git history for sensitive files (.env, *.pem, *.key)..."

# List of patterns to search for
PATTERNS="**/.env* **/*.pem **/*.key id_rsa"

# Run git log search
# --all: all refs
# --full-history: all history
# --: separate options from paths
FOUND_SECRETS=$(git log --all --full-history --name-only --format="" -- $PATTERNS | sort | uniq)

if [ -n "$FOUND_SECRETS" ]; then
    echo "❌ DANGER: SECRETS FOUND IN GIT HISTORY!"
    echo "The following files were committed at some point (even if deleted now):"
    echo "$FOUND_SECRETS"
    echo ""
    echo "ACTION REQUIRED: You must purge these from history using 'git filter-repo' or BFG Repo-Cleaner before making the repo public."
    EXIT_CODE=1
else
    echo "✅ No secrets found in git history."
    EXIT_CODE=0
fi

echo "------------------------------"

# 2. Check .git Directory Size
# -----------------------------------------------------------------------------
echo "Checking repository size..."
GIT_SIZE=$(du -sh .git | cut -f1)
echo "📂 .git folder size: $GIT_SIZE"

# Simple check if size is > 100M (rough heuristic)
# Note: This is a simple string comparison, might need adjustment for exact logic
if [[ "$GIT_SIZE" == *G ]]; then
    echo "⚠️  WARNING: Repository is very large (Gigabytes). Check for large binary files."
elif [[ "$GIT_SIZE" == *M ]]; then
    # Extract number
    SIZE_NUM=$(echo $GIT_SIZE | sed 's/M//')
    if [ "$SIZE_NUM" -gt 500 ]; then
        echo "⚠️  WARNING: .git folder is over 500MB. Consider using Git LFS for binaries."
    else
        echo "✅ Repository size is healthy."
    fi
else
    echo "✅ Repository size is healthy."
fi

echo "=============================="
if [ $EXIT_CODE -eq 0 ]; then
    echo "✅ SCAN PASSED: Ready for Deployment"
else
    echo "❌ SCAN FAILED: Fix issues before deploying"
fi

exit $EXIT_CODE
