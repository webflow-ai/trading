"""
Vercel serverless entrypoint for the Nifty Pre-Market Analysis Engine.
The actual app lives in main.py (repo root) so it stays importable/testable
without going through Vercel's Python runtime — this file only exists
because @vercel/python needs an `app` variable in the file vercel.json
points at.
"""

from main import app  # noqa: F401
