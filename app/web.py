"""Small dependency-free web interface for AstroSearch."""

from __future__ import annotations

import argparse
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.main import crossmatch
from app.models import AstroSearchError, InvalidCoordinateError, Settings


INDEX_PATH = Path(__file__).resolve().parent / 'static' / 'index.html'
MAX_REQUEST_BYTES = 64 * 1024


class AstroSearchHTTPServer(ThreadingHTTPServer):
    settings: Settings | None


def parse_crossmatch_request(payload: Any) -> tuple[Any, Any, float | None]:
    """Validate the small JSON request accepted by the browser UI."""

    if not isinstance(payload, dict):
        raise TypeError('Request body must be a JSON object.')
    if 'ra' not in payload or 'dec' not in payload:
        raise ValueError('Both ra and dec are required.')
    radius = payload.get('radius_arcsec')
    if radius is not None:
        try:
            radius = float(radius)
        except (TypeError, ValueError) as exc:
            raise ValueError('radius_arcsec must be numeric.') from exc
    return payload['ra'], payload['dec'], radius


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, default=str).encode('utf-8')


class AstroSearchRequestHandler(BaseHTTPRequestHandler):
    server: AstroSearchHTTPServer
    protocol_version = 'HTTP/1.0'

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(status, _json_bytes(payload), 'application/json; charset=utf-8')

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        path = urlparse(self.path).path
        if path in {'/', '/index.html'}:
            try:
                body = INDEX_PATH.read_bytes()
            except OSError:
                self._send_json(500, {'error': 'UI asset is unavailable.'})
                return
            self._send(200, body, 'text/html; charset=utf-8')
            return
        if path == '/health':
            self._send_json(200, {'status': 'ok'})
            return
        self._send_json(404, {'error': 'Not found.'})

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if urlparse(self.path).path != '/api/crossmatch':
            self._send_json(404, {'error': 'Not found.'})
            return
        try:
            content_length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self._send_json(400, {'error': 'Content-Length must be an integer.'})
            return
        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            self._send_json(413, {'error': 'Request body is missing or too large.'})
            return
        try:
            payload = json.loads(self.rfile.read(content_length))
            ra, dec, radius = parse_crossmatch_request(payload)
            result = asyncio.run(crossmatch(ra, dec, radius_arcsec=radius, settings=self.server.settings))
        except (InvalidCoordinateError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {'error': str(exc)})
            return
        except AstroSearchError as exc:
            self._send_json(502, {'error': str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {'error': 'Crossmatch request failed.'})
            return
        self._send_json(200, result.as_dict())

    def log_message(self, format: str, *args: Any) -> None:
        return


def create_server(host: str = '127.0.0.1', port: int = 8000, *, settings: Settings | None = None) -> AstroSearchHTTPServer:
    server = AstroSearchHTTPServer((host, port), AstroSearchRequestHandler)
    server.settings = settings
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description='Run the AstroSearch browser UI.')
    parser.add_argument('--host', default='127.0.0.1', help='Interface to bind (default: 127.0.0.1)')
    parser.add_argument('--port', type=int, default=8000, help='Port to bind (default: 8000)')
    args = parser.parse_args()
    server = create_server(args.host, args.port)
    print(f'AstroSearch UI running at http://{args.host}:{args.port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
