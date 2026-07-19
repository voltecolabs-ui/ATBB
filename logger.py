"""Structured logging для BTC Auto Trader"""
import json
import os
from datetime import datetime, timezone

LOG_DIR = os.path.expanduser("~/.hermes/profiles/trader/logs")
os.makedirs(LOG_DIR, exist_ok=True)

def log_trade(action, details):
    """Логировать сделку"""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "trade",
        "action": action,
        **details
    }
    _write_log("trades", entry)

def log_signal(signal_data):
    """Логировать сигнал"""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "signal",
        **signal_data
    }
    _write_log("signals", entry)

def log_error(error, context=""):
    """Логировать ошибку"""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "error",
        "error": str(error),
        "context": context
    }
    _write_log("errors", entry)

def log_status(status_data):
    """Логировать статус"""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "status",
        **status_data
    }
    _write_log("status", entry)

def _write_log(log_type, entry):
    """Записать в лог-файл"""
    log_file = os.path.join(LOG_DIR, f"{log_type}.jsonl")
    try:
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except:
        pass
