import json
import os


def _load_json(filepath, default):
    """Read a JSON file; return default on missing/corrupt. Kept for migration use."""
    os.makedirs("data", exist_ok=True)
    try:
        with open(filepath) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default
