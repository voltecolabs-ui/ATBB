#!/bin/bash
# Auto-sync script: pulls latest changes from GitHub every 30 seconds
REPO_DIR="/home/labs/.hermes/profiles/trader/scripts"
LOG_FILE="$REPO_DIR/.git/sync.log"
INTERVAL=30

cd "$REPO_DIR" || exit 1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Auto-sync started" >> "$LOG_FILE"

while true; do
    # Stash local changes if any
    STASHED=$(git stash list 2>/dev/null)
    DIRTY=$(git status --porcelain)

    if [ -n "$DIRTY" ]; then
        git stash --quiet 2>/dev/null
    fi

    # Pull latest from remote
    OUTPUT=$(git pull origin main 2>&1)
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 0 ] && echo "$OUTPUT" | grep -q "Already up to date"; then
        : # No changes
    elif [ $EXIT_CODE -eq 0 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pulled: $OUTPUT" >> "$LOG_FILE"
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pull error: $OUTPUT" >> "$LOG_FILE"
    fi

    # Restore stashed changes
    if [ -n "$DIRTY" ]; then
        git stash pop --quiet 2>/dev/null
    fi

    sleep "$INTERVAL"
done
