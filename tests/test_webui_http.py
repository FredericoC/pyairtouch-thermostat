"""The webui HTTP layer: routing, query parsing/clamping and error responses,
against a real ThreadingHTTPServer on an ephemeral port."""

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import ThreadingHTTPServer

import pytest

from webui import MAX_STAT_HOURS, Api, make_handler
from conftest import make_config, make_history_db


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes

    def json(self):
        return json.loads(self.body)


class Server:
    def __init__(self, base: str, tmp_path) -> None:
        self.base = base
        self.tmp_path = tmp_path

    def _do(self, request) -> Response:
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return Response(resp.status, dict(resp.headers), resp.read())
        except urllib.error.HTTPError as exc:
            return Response(exc.code, dict(exc.headers), exc.read())

    def get(self, path: str) -> Response:
        return self._do(urllib.request.Request(self.base + path))

    def post(self, path: str, body: bytes) -> Response:
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        return self._do(req)


@pytest.fixture
def server(tmp_path):
    now = int(time.time())
    db = tmp_path / "history.db"
    make_history_db(
        db,
        [
            (now - 120, "A", 20.0, 23.0, 1, "HEAT", "heating"),
            (now - 60, "A", 20.5, 23.0, 1, "HEAT", "heating"),
            (now - 60, "B", 22.0, None, 0, None, "idle"),
        ],
    )
    log = tmp_path / "climate.log"
    log.write_text("one\ntwo\nthree\n")
    html = tmp_path / "webui.html"
    html.write_text("<h1>dashboard</h1>")
    (tmp_path / "stats.html").write_text("<h1>stats</h1>")
    cfg = make_config(
        history_path=db,
        log_path=log,
        override_path=tmp_path / "control_override.json",
        shutdown_windows=((1200, 480),),
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Api(cfg), html))
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield Server(f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class TestPages:
    def test_index(self, server):
        r = server.get("/")
        assert r.status == 200
        assert r.headers["Content-Type"] == "text/html; charset=utf-8"
        assert r.headers["Cache-Control"] == "no-store"
        assert r.body == b"<h1>dashboard</h1>"

    def test_stats_page_is_read_beside_index(self, server):
        assert server.get("/stats").body == b"<h1>stats</h1>"

    def test_index_reread_per_request(self, server):
        (server.tmp_path / "webui.html").write_text("<h1>edited</h1>")
        assert server.get("/").body == b"<h1>edited</h1>"

    def test_pwa_assets(self, server):
        manifest = server.get("/manifest.webmanifest")
        assert manifest.headers["Content-Type"] == "application/manifest+json"
        assert manifest.json()["display"] == "standalone"
        sw = server.get("/sw.js")
        assert sw.status == 200
        assert sw.headers["Content-Type"] == "application/javascript"
        icon = server.get("/icon-192.png")
        assert icon.status == 200
        assert icon.body[:8] == b"\x89PNG\r\n\x1a\n"
        assert icon.headers["Cache-Control"] == "public, max-age=86400"

    def test_unknown_path_is_404(self, server):
        r = server.get("/nope")
        assert r.status == 404
        assert r.body == b"not found"


class TestApiGet:
    def test_data(self, server):
        r = server.get("/api/data?hours=1")
        assert r.status == 200
        assert r.headers["Content-Type"] == "application/json"
        data = r.json()
        assert data["units"] == ["A", "B"]
        assert len(data["series"]["A"]) == 2

    def test_data_hours_clamped_low(self, server):
        # hours=0 clamps to 0.1h: the fixture rows (1–2 min old) still fit.
        assert server.get("/api/data?hours=0").status == 200

    def test_stats_daily_default(self, server):
        stats = server.get("/api/stats").json()
        assert stats["bucket_seconds"] == 86400
        assert len(stats["buckets"]) == 14

    def test_stats_days(self, server):
        stats = server.get("/api/stats?days=3").json()
        assert len(stats["buckets"]) == 3

    def test_stats_hours_takes_precedence_and_clamps(self, server):
        stats = server.get("/api/stats?hours=2&days=30").json()
        assert stats["bucket_seconds"] == 3600
        assert len(stats["buckets"]) == 2
        huge = server.get(f"/api/stats?hours={MAX_STAT_HOURS * 10}").json()
        assert len(huge["buckets"]) == MAX_STAT_HOURS

    def test_csv(self, server):
        r = server.get("/api/readings.csv?hours=1")
        assert r.headers["Content-Type"] == "text/csv"
        lines = r.body.decode().strip().splitlines()
        assert lines[0] == "ts,unit,temperature,setpoint,power,mode,activity"
        assert len(lines) == 4

    def test_log_tail(self, server):
        r = server.get("/api/log?lines=2")
        assert r.headers["Content-Type"] == "text/plain; charset=utf-8"
        assert r.body == b"two\nthree"
        assert server.get("/api/log?lines=0").body == b"three"  # clamped to 1

    def test_bad_query_value_is_500(self, server):
        # Documents current behaviour: unparsable numbers surface as a 500,
        # not a 400. Not a contract — tighten to 400 if the handler learns to.
        r = server.get("/api/data?hours=abc")
        assert r.status == 500
        assert r.body == b"internal error"


class TestApiPost:
    def test_pause_writes_override_file(self, server):
        r = server.post("/api/override", b'{"pause": true}')
        assert r.status == 200
        assert r.json() == {"override": {"pause": True}}
        path = server.tmp_path / "control_override.json"
        assert json.loads(path.read_text()) == {"pause": True}

    def test_shutdown_override_gets_expiry(self, server):
        override = server.post("/api/override", b'{"shutdown": false}').json()["override"]
        assert override["shutdown"] is False
        assert override["expires"] > time.time()

    def test_clear(self, server):
        server.post("/api/override", b'{"pause": true}')
        r = server.post("/api/override", b'{"shutdown": null}')
        assert r.json() == {"override": None}
        assert not (server.tmp_path / "control_override.json").exists()

    def test_empty_body_is_a_no_op_clear(self, server):
        r = server.post("/api/override", b"")
        assert r.status == 200
        assert r.json() == {"override": None}

    @pytest.mark.parametrize(
        ("body", "fragment"),
        [
            (b"{not json", b""),  # any 400 will do for a JSON syntax error
            (b"[1, 2]", b"JSON object"),
            (b'{"pause": "yes"}', b"pause"),
            (b'{"shutdown": 1}', b"shutdown"),
        ],
    )
    def test_bad_bodies_are_400(self, server, body, fragment):
        r = server.post("/api/override", body)
        assert r.status == 400
        assert fragment in r.body

    def test_post_elsewhere_is_404(self, server):
        assert server.post("/api/data", b"{}").status == 404
