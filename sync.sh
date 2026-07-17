#!/bin/bash
# Auto-sync: pull + commit + push every 30 seconds
REPO_DIR="/home/labs/.hermes/profiles/trader/scripts"
LOG_FILE="$REPO_DIR/.git/sync.log"
INTERVAL=30

cd "$REPO_DIR" || exit 1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Auto-sync started" >> "$LOG_FILE"

while true; do
    # 1. Pull latest from remote
    PULL_OUTPUT=$(git pull origin main 2>&1)
    if echo "$PULL_OUTPUT" | grep -q "CONFLICT"; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️ CONFLICT: $PULL_OUTPUT" >> "$LOG_FILE"
    elif ! echo "$PULL_OUTPUT" | grep -q "Already up to date"; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pulled: $PULL_OUTPUT" >> "$LOG_FILE"
    fi

    # 2. Check for local changes
    DIRTY=$(git status --porcelain)
    if [ -n "$DIRTY" ]; then
        # 3. Stage all changes (add + delete)
        git add -A

        # 4. Commit
        MSG="Auto-sync: $(date '+%Y-%m-%d %H:%M:%S')"
        COMMIT_OUTPUT=$(git commit -m "$MSG" 2>&1)

        # 5. Push to remote
        PUSH_OUTPUT=$(git push origin main 2>&1)
        if [ $? -eq 0 ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ Committed & pushed: $PUSH_OUTPUT" >> "$LOG_FILE"
        else
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] ❌ Push error: $PUSH_OUTPUT" >> "$LOG_FILE"
        fi
    fi

    sleep "$INTERVAL"
done
