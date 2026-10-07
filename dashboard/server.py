"""Read-only HTTP dashboard served from a daemon thread inside the charger process."""
import hmac
import json
import threading
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from core import config
from dashboard.data import get_dashboard_data
from reporting.logger import log

_PAGE = Path(__file__).with_name("index.html")
_STATIC = Path(__file__).with_name("static")
# Not sensitive, and fetched by the phone without the page's ?token=, so served openly.
_PUBLIC_ICONS = {"/icon-180.png", "/icon-192.png", "/icon-512.png"}


def _manifest(token: str | None) -> dict:
    # A home-screen app does not keep the browser's URL, so the token rides in start_url.
    start = f"./?token={quote(token)}" if token else "./"
    return {
        "name": "Home Energy",
        "short_name": "Energy",
        "description": "Solar, Powerwall, and EV charging at a glance",
        "start_url": start,
        "scope": "./",
        "display": "standalone",
        "background_color": "#0d0d0d",
        "theme_color": "#0d0d0d",
        "icons": [
            {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "SmartEVDashboard"

    def _authorized(self, query: dict) -> bool:
        if not config.DASHBOARD_TOKEN:
            return True
        supplied = (query.get("token") or [""])[0]
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            supplied = header[7:]
        return hmac.compare_digest(supplied.encode(), config.DASHBOARD_TOKEN.encode())

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path in _PUBLIC_ICONS:
            self._send(200, (_STATIC / url.path.lstrip("/")).read_bytes(), "image/png")
            return
        if not self._authorized(query):
            self._send(401, b"Unauthorized", "text/plain")
            return
        token = (query.get("token") or [None])[0] if config.DASHBOARD_TOKEN else None
        try:
            if url.path == "/":
                page = _PAGE.read_text()
                if token:
                    page = page.replace('href="manifest.webmanifest"',
                                        f'href="manifest.webmanifest?token={escape(quote(token))}"')
                self._send(200, page.encode(), "text/html; charset=utf-8")
            elif url.path == "/manifest.webmanifest":
                self._send(200, json.dumps(_manifest(token)).encode(), "application/manifest+json")
            elif url.path == "/api/dashboard":
                body = json.dumps(get_dashboard_data(), default=str).encode()
                self._send(200, body, "application/json")
            else:
                self._send(404, b"Not found", "text/plain")
        except Exception as e:
            log.error(f"DASHBOARD | Failed to serve {url.path}: {e}", exc_info=True)
            self._send(500, b"Dashboard error", "text/plain")

    def log_message(self, format, *args):
        pass  # Page polls every minute; keep it out of the charger log.


def start_dashboard_server() -> ThreadingHTTPServer | None:
    """Starts the dashboard on DASHBOARD_HOST:DASHBOARD_PORT, or does nothing when the port is 0."""
    if not config.DASHBOARD_PORT:
        return None
    try:
        server = ThreadingHTTPServer((config.DASHBOARD_HOST, config.DASHBOARD_PORT), _Handler)
    except OSError as e:
        log.error(f"DASHBOARD | Could not bind {config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}: {e}")
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="Dashboard").start()
    log.info(f"DASHBOARD | Serving on http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
    return server
