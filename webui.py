"""Browser interface for the AirTouch temperature history.

Serves a single-page dashboard (webui.html) plus a small JSON/CSV API over the
SQLite history recorded by climate_service.py. Stdlib only — no dependencies.

Run with:
    python webui.py                # http://localhost:8765, uses ./config.toml
    python webui.py --port 9000
"""

import argparse
import csv
import io
import json
import logging
import sqlite3
import sys
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from climate_service import Config, load_config

_LOGGER = logging.getLogger("webui")

MAX_POINTS_PER_UNIT = 700  # downsample beyond this to keep payloads light
MAX_HOURS = 24 * 90
MAX_LOG_LINES = 2000

# Numeric encoding for the activity column, highest control-priority wins when
# a downsample bucket mixes values. Decoded back to strings client-side.
_ACTIVITY_CODE = "CASE activity WHEN 'heating' THEN 3 WHEN 'cooling' THEN 2 WHEN 'on' THEN 1 ELSE 0 END"
_ACTIVITY_NAMES = {3: "heating", 2: "cooling", 1: "on", 0: None}

# Web app manifest so Chrome on Android installs the dashboard as a
# standalone app ("Add to Home screen"). Chrome only honours standalone
# display on secure origins — on plain LAN HTTP, allowlist the origin via
# chrome://flags/#unsafely-treat-insecure-origin-as-secure on the device.
MANIFEST = {
    "name": "AirTouch temperatures",
    "short_name": "AirTouch",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#f9f9f7",
    "theme_color": "#f9f9f7",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png",
         "purpose": "any maskable"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "any maskable"},
    ],
}


class Api:
    def __init__(self, cfg: Config) -> None:
        if cfg.history_path is None:
            raise SystemExit("history recording is disabled in the config; nothing to serve")
        self._cfg = cfg
        self._db_path = cfg.history_path
        # Unit order = config group order; this is also the fixed color order.
        self._units = [name for group in cfg.groups for name in group.members]

    def _connect(self) -> sqlite3.Connection:
        # Read-only so the dashboard can never interfere with the service.
        return sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)

    def data(self, hours: float) -> dict:
        seconds = int(hours * 3600)
        # Bucket size that keeps each unit under MAX_POINTS_PER_UNIT samples.
        bucket = max(
            int(self._cfg.history_interval),
            seconds // MAX_POINTS_PER_UNIT,
        )
        # closing() is required: `with` on a sqlite3.Connection only manages
        # the transaction — it does not close, and leaked connections exhaust
        # the process file-descriptor limit after a few hours of auto-refresh.
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT (ts / :bucket) * :bucket + :bucket / 2 AS t,
                       unit,
                       ROUND(AVG(temperature), 2),
                       ROUND(AVG(setpoint), 1),
                       MAX(power),
                       MAX({_ACTIVITY_CODE})
                FROM readings
                WHERE ts >= unixepoch('now') - :seconds
                GROUP BY t, unit
                ORDER BY t
                """,
                {"bucket": bucket, "seconds": seconds},
            ).fetchall()
            # Raw newest sample time (not bucketed): the client predicts the
            # next write as latest_ts + sample_interval.
            latest_ts = conn.execute("SELECT MAX(ts) FROM readings").fetchone()[0]
            try:
                weather = conn.execute(
                    """
                    SELECT (ts / :bucket) * :bucket + :bucket / 2 AS t,
                           ROUND(AVG(outdoor_temp), 2),
                           ROUND(AVG(solar), 1)
                    FROM weather
                    WHERE ts >= unixepoch('now') - :seconds
                    GROUP BY t
                    ORDER BY t
                    """,
                    {"bucket": bucket, "seconds": seconds},
                ).fetchall()
            except sqlite3.OperationalError:
                weather = []  # database predates the weather table

        series: dict[str, list] = {unit: [] for unit in self._units}
        for t, unit, temp, setpoint, power, activity in rows:
            if unit in series:
                series[unit].append(
                    [t, temp, setpoint, power, _ACTIVITY_NAMES.get(activity)]
                )
        return {
            "units": self._units,
            "groups": {
                g.name: {"master": g.master, "members": list(g.members)}
                for g in self._cfg.groups
            },
            "ranges": {
                name: [room.target_low, room.target_high]
                for name, room in self._cfg.rooms.items()
            },
            # (start, end) minutes past midnight; the client formats these and
            # decides "active now" with its own clock (same household/timezone).
            "shutdown_windows": [list(w) for w in self._cfg.shutdown_windows],
            "sample_interval": self._cfg.history_interval,
            "poll_interval": self._cfg.poll_interval,
            "latest_ts": latest_ts,
            "bucket": bucket,
            "series": series,
            "weather": weather,  # [[t, outdoor °C, solar W/m²], ...]
        }

    def csv(self, hours: float) -> str:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT ts, unit, temperature, setpoint, power, mode, activity
                FROM readings
                WHERE ts >= unixepoch('now') - ?
                ORDER BY ts, unit
                """,
                (int(hours * 3600),),
            ).fetchall()
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["ts", "unit", "temperature", "setpoint", "power", "mode", "activity"])
        writer.writerows(rows)
        return out.getvalue()

    def log_tail(self, lines: int) -> str:
        """Last `lines` lines of the climate service's log file.

        The file is capped at ~1 MB by the service's rotating handler, so
        reading it whole is cheap.
        """
        if self._cfg.log_path is None:
            return "File logging is disabled in the config ([service] log_file)."
        try:
            text = self._cfg.log_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return (
                f"Log file not found: {self._cfg.log_path}\n"
                "Restart climate_service.py to start file logging."
            )
        return "\n".join(text.splitlines()[-lines:])


def make_handler(api: Api, html_path: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            try:
                self._route()
            except BrokenPipeError:
                pass
            except Exception:
                _LOGGER.exception("error handling %s", self.path)
                self._respond(500, "text/plain", b"internal error")

        def _route(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            hours = min(MAX_HOURS, max(0.1, float(query.get("hours", ["24"])[0])))

            if parsed.path == "/":
                # Read per request so page tweaks don't need a restart.
                self._respond(200, "text/html; charset=utf-8", html_path.read_bytes())
            elif parsed.path == "/api/data":
                body = json.dumps(api.data(hours)).encode()
                self._respond(200, "application/json", body)
            elif parsed.path == "/api/readings.csv":
                self._respond(200, "text/csv", api.csv(hours).encode())
            elif parsed.path == "/api/log":
                lines = min(MAX_LOG_LINES, max(1, int(query.get("lines", ["300"])[0])))
                body = api.log_tail(lines).encode()
                self._respond(200, "text/plain; charset=utf-8", body)
            elif parsed.path == "/manifest.webmanifest":
                self._respond(200, "application/manifest+json",
                              json.dumps(MANIFEST).encode())
            elif parsed.path == "/sw.js":
                sw = Path(__file__).parent / "sw.js"
                self._respond(200, "application/javascript", sw.read_bytes())
            elif parsed.path in ("/icon-192.png", "/icon-512.png"):
                icon = Path(__file__).parent / parsed.path.lstrip("/")
                self._respond(200, "image/png", icon.read_bytes(),
                              cache="public, max-age=86400")
            else:
                self._respond(404, "text/plain", b"not found")

        def _respond(
            self, status: int, content_type: str, body: bytes,
            cache: str = "no-store",
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            _LOGGER.debug("%s %s", self.address_string(), format % args)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).parent / "config.toml",
        help="path to the service TOML config (default: config.toml beside this script)",
    )
    parser.add_argument("--host", default="0.0.0.0",
                        help="bind address (default: all interfaces, LAN-accessible)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    api = Api(load_config(args.config))
    handler = make_handler(api, Path(__file__).parent / "webui.html")
    server = ThreadingHTTPServer((args.host, args.port), handler)
    _LOGGER.info("Serving dashboard on http://%s:%d",
                 "localhost" if args.host == "0.0.0.0" else args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
