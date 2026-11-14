"""`python -m leadhub` — run the web app (binds to localhost by default)."""

import logging
import os

import uvicorn

from .main import create_app

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def run() -> None:
    host = os.getenv("LEADHUB_HOST", "127.0.0.1")
    port = int(os.getenv("LEADHUB_PORT", "8000"))
    app = create_app()
    if host not in LOOPBACK_HOSTS and not app.state.settings.cookie_secure:
        logging.getLogger("leadhub").warning(
            "Listening on %s over plain HTTP: the session cookie can be sniffed on this network. "
            "Put Lead Hub behind an HTTPS proxy and set LEADHUB_COOKIE_SECURE=1.", host,
        )
    uvicorn.run(app, host=host, port=port, proxy_headers=False)


if __name__ == "__main__":
    run()
