import json


def _load_json(filepath, default):
    """Read a JSON file; return default on missing/corrupt. Kept for migration use."""
    try:
        with open(filepath) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default
