import json
import sys
from copy import copy
from pathlib import Path

from fastapi.openapi.utils import get_openapi

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app

target = Path("../../packages/api-client/openapi.json")
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(target)

# Keep internal development helpers out of the public API. Derive this separate
# contract from the same FastAPI route/schema objects, never a hand-written DTO.
development_routes = []
for route in app.routes:
    if getattr(route, "path", None) == "/v1/wallet/test-grants":
        development_route = copy(route)
        development_route.include_in_schema = True
        development_routes.append(development_route)
development = target.with_name("openapi-development.json")
development.write_text(
    json.dumps(
        get_openapi(title="Development-only API", version=app.version, routes=development_routes),
        ensure_ascii=False,
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
print(development)
