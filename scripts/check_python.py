"""Fail early with a clear message if Python is too old (google-genai needs 3.11+)."""
import sys

if sys.version_info < (3, 11):
    sys.exit(f"Python 3.11+ is required, found {sys.version.split()[0]}. "
             "Install a newer Python or run: make install PY=python3.12")
print(f"Python {sys.version.split()[0]} OK")
