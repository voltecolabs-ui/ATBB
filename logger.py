"""Structured logging для BTC Auto Trader"""
import json
import os
import shutil
from datetime import datetime, timezone

LOG_DIR = os.path.expanduser("~/.hermes/profiles/trader/logs")
os.makedirs(LOG_DIR, exist_ok=True)

MAX_LOG_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_LOG_FILES = 5  # Максимум 5 ротаций

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

def _rotate_log(log_file):
    """Ротация лог-файла при превышении размера"""
    if not os.path.exists(log_file):
        return
    
    file_size = os.path.getsize(log_file)
    if file_size < MAX_LOG_SIZE:
        return
    
    # Удалить самую старую ротацию
    for i in range(MAX_LOG_FILES - 1, 0, -1):
        old_file = f"{log_file}.{i}"
        new_file = f"{log_file}.{i + 1}"
        if os.path.exists(old_file):
            if i == MAX_LOG_FILES - 1:
                os.remove(old_file)
            else:
                os.rename(old_file, new_file)
    
    # Текущий файл → .1
    shutil.copy2(log_file, f"{log_file}.1")
    with open(log_file, "w") as f:
        f.write("")

def _write_log(log_type, entry):
    """Записать в лог-файл с ротацией"""
    log_file = os.path.join(LOG_DIR, f"{log_type}.jsonl")
    try:
        _rotate_log(log_file)
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except (IOError, OSError):
        pass
