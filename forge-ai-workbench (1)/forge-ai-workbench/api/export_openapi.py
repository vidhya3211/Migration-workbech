"""Write ``api/openapi.yaml`` from the implementation (the contract test keeps them identical; the console's
TypeScript types are generated from this file with ``npm run gen:api``).

    python -m api.export_openapi
"""
from __future__ import annotations

from pathlib import Path

import yaml

OUT = Path(__file__).with_name("openapi.yaml")


def spec() -> dict:
    from fastapi.openapi.utils import get_openapi
    from api.app import create_app
    from unittest.mock import MagicMock
    services = MagicMock()
    services.settings = None
    from config.settings import load_settings
    settings = load_settings(overrides={"jobs.embedded_worker": False})
    app = create_app(settings, services, MagicMock(), start_workers=False)
    doc = get_openapi(title="AI Workbench API", version=app.version, routes=app.routes,
                      description=("Migration-assurance API. Every resource under /v1/projects/{project_id} is scoped "
                                   "to that project. Sign in with POST /v1/auth/login and send the returned bearer "
                                   "token. Large bundles are uploaded in chunks (begin, PUT each chunk, complete)."))
    doc["components"].setdefault("securitySchemes", {})["bearer"] = {"type": "http", "scheme": "bearer",
                                                                     "bearerFormat": "JWT"}
    doc["security"] = [{"bearer": []}]
    return doc


def main() -> None:
    OUT.write_text(yaml.safe_dump(spec(), sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
