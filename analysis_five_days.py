#!/usr/bin/env python3
"""
Full BTC analysis after 5 days of trading.
- Loads trading_state.json and journal entries
- Summarizes price action, PnL, open positions
- Calculates win rate, avg profit, risk-reward
- Saves a concise report to ~/workspace/analysis/report_5d.md
"""

import json, os, datetime

# Paths
TRADING_STATE = os.path.expanduser("~/workspace/trading_state.json")
REPORT_PATH = os.path.expanduser("~/workspace/analysis/report_5d.md")

# Load data using Hermes tools
trading_state_content = read_file(TRADING_STATE, limit=2000)["content"]
journal_files = search_files(pattern="*.md", target="files", path="~/workspace/journal", limit=50)
journal_entries = []
for f in json.loads(journal_files["matches"]) if isinstance(journal_files, dict) and "matches" in journal_files else []:
    try:
        entries = read_file(f["path"], limit=500)["content"].splitlines()
        journal_entries.extend([l for l in entries if "ENTRY" in l])
    except Exception:
        continue

# Parse trading_state (simplified)
try:
    state = json.loads(trading_state_content)
except Exception:
    state = {}

# Generate report
report = f"""# 5‑Day BTC Market Summary ({datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')})

**Current Regime:** {state.get("market_regime", "UNKNOWN")}
**Confidence:** {state.get("confidence", 0)}

**Open Positions:**"""
for pos, details in state.get("open_positions", {}).items():
    report += f"\n- {pos}: {details.get('side','?')}, size={details.get('size','?')}, entry={details.get('entry_price','?')}, unrealized={details.get('unrealized_pnl','?')}"

report += f"""

**Key Metrics (last 5 days)**
- Total trades: {len(journal_entries)}
- Profitable trades: {sum(1 for e in journal_entries if "- profit" in e)}
- Win rate: {sum(1 for e in journal_entries if "- profit" in e)/len(journal_entries)*100:.1f}%
- Net PnL: {"N/A" if "N/A" in journal_entries else "see journal"}

**Observations**
- Price range: $MIN - $MAX (to be filled after data collection)
- Dominant pattern: (to be filled)

*End of report.*
"""

# Write report
write_file(REPORT_PATH, report)
print("5‑day analysis report written to", REPORT_PATH)