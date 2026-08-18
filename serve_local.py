"""
Local combined API — PCR/Upstox (api/index.py) + Premarket (main.py) on one
port. Production splits these via vercel.json; locally the dashboard expects
both under :8000, so without this Upstox panels show "not connected" / 404.
"""

from dotenv import load_dotenv

load_dotenv()

from api.index import app as app  # noqa: E402  — PCR + Upstox routes
import main as premarket_mod  # noqa: E402

# Attach premarket routes onto the PCR app (paths already include /api/premarket/...).
_existing = {getattr(r, "path", None) for r in app.router.routes}
for route in premarket_mod.app.router.routes:
    path = getattr(route, "path", None)
    if path and path not in _existing:
        app.router.routes.append(route)

app.title = "Trading local (PCR + Premarket)"
