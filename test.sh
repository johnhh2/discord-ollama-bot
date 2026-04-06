#!/usr/bin/env bash
set -e
pip install pytest pytest-asyncio -q
pytest "$@"
