"""Focused contracts for bounded, read-only Record hosting."""
import json
import threading
from pathlib import Path
from urllib.request import Request, urlopen

from yylo_ledger.artifacts import ArtifactStore
from yylo_ledger.config import Config
from yylo_ledger.documents import DocumentStore
from yylo_ledger.hosting import HostPolicy, HostingApplication, LedgerHTTPServer
from yylo_ledger.profiles import WORKFLOW_SCHEMA_V1
from yylo_ledger.storage import TaskStorage


def application(tmp_path, **policy):
    config_path = tmp_path / ".juno_task" / "tasks" / "config.json"
    config_path.parent.mkdir(parents=True)
    config = json.loads(json.dumps(Config.DEFAULT_CONFIG))
    config["storage"]["base_path"] = str(config_path.parent)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    storage = TaskStorage(Config(str(config_path), auto_create=False))
    return storage, HostingApplication(storage, HostPolicy(**policy))


def test_id_slug_json_raw_html_and_validators(tmp_path):
    storage, app = application(tmp_path)
    wiki = DocumentStore(storage.juno_root).create(
        record_id="Wi1iki", title="Hosted", profile="wiki", media_type="text/markdown",
        text="# Safe\n<script>alert(1)</script>\n", slug="hosted", aliases=("old-hosted",),
    )

    by_id = app.dispatch("GET", "/wiki/Wi1iki", {"Accept": "application/json"})
    by_slug = app.dispatch("GET", "/wiki/old-hosted", {"Accept": "application/json"})
    assert by_id.status == by_slug.status == 200
    assert json.loads(by_id.body)["id"] == json.loads(by_slug.body)["id"] == wiki["id"]
    assert by_slug.headers["Content-Location"] == "/record/Wi1iki"

    raw = app.dispatch("GET", "/wiki/hosted", {"Accept": "text/markdown"})
    assert raw.body == wiki["payload"]["text"].encode()
    assert raw.headers["Content-Type"].startswith("text/markdown")
    rendered = app.dispatch("GET", "/wiki/hosted", {"Accept": "text/html"})
    assert rendered.status == 200
    assert b"<script>" not in rendered.body and b"&lt;script&gt;" in rendered.body
    assert "default-src 'none'" in rendered.headers["Content-Security-Policy"]

    cached = app.dispatch("GET", "/wiki/Wi1iki", {
        "Accept": "application/json", "If-None-Match": by_id.headers["ETag"]})
    assert cached.status == 304 and cached.body == b""

    DocumentStore(storage.juno_root).archive("Wi1iki", expected_revision=1)
    cold = app.dispatch("GET", "/wiki/old-hosted", {"Accept": "text/markdown"})
    assert cold.status == 200 and cold.body == raw.body


def test_artifact_content_range_redirect_and_restriction(tmp_path):
    storage, app = application(tmp_path, allowed_redirect_hosts=("evidence.example",), max_range_bytes=3)
    artifacts = ArtifactStore(storage.juno_root)
    artifacts.create(record_id="Ar1fac", title="bytes", profile="report", mode="inline",
                     content=b"abcdef", media_type="text/plain")
    ranged = app.dispatch("GET", "/artifact/Ar1fac/content", {"Range": "bytes=1-3"})
    assert ranged.status == 206 and ranged.body == b"bcd"
    assert ranged.headers["Content-Range"] == "bytes 1-3/6"
    assert app.dispatch("GET", "/artifact/Ar1fac/content", {"Range": "bytes=0-5"}).status == 416

    artifacts.create(record_id="Ex1ern", title="remote", profile="report", mode="link",
                     uri="https://evidence.example/report", media_type="text/plain")
    redirect = app.dispatch("GET", "/artifact/Ex1ern/download")
    assert redirect.status == 307 and redirect.headers["Location"].startswith("https://")

    restricted = artifacts.create(record_id="Se1ret", title="private", profile="report",
                                  mode="inline", content=b"safe", media_type="text/plain")
    path = artifacts._revision_path(restricted["id"], 1)
    value = json.loads(path.read_text())
    value["system_metadata"]["classification"] = "restricted"
    path.write_text(json.dumps(value), encoding="utf-8")
    refused = app.dispatch("GET", "/artifact/Se1ret")
    assert refused.status == 403
    assert json.loads(refused.body)["error"] == {
        "code": "RECORD_RESTRICTED", "id": "Se1ret",
        "message": "Record is restricted by hosting policy",
    }


def test_routes_fail_closed_and_requests_do_not_write(tmp_path):
    storage, app = application(tmp_path)
    DocumentStore(storage.juno_root).create(
        record_id="Fl1low", title="Flow", profile="workflow", media_type="application/yaml",
        text="schema_version: v1\nworkflow_id: flow\nsteps: []\n",
        schema_ref=WORKFLOW_SCHEMA_V1,
    )
    before = {str(path.relative_to(storage.juno_root)): path.read_bytes()
              for path in storage.juno_root.rglob("*") if path.is_file()}

    assert app.dispatch("POST", "/record/Fl1low").status == 405
    assert app.dispatch("GET", "/record/%2e%2e").status == 400
    assert app.dispatch("GET", "/workflow/Fl1low", {"Accept": "application/yaml"}).status == 200
    assert app.dispatch("GET", "/workflow/Fl1low", {"Accept": "image/png"}).status == 406

    after = {str(path.relative_to(storage.juno_root)): path.read_bytes()
             for path in storage.juno_root.rglob("*") if path.is_file()}
    assert after == before


def test_network_server_starts_serves_and_shuts_down(tmp_path):
    storage, app = application(tmp_path)
    DocumentStore(storage.juno_root).create(
        record_id="Ne1wrk", title="Network", profile="wiki", media_type="text/markdown",
        text="served\n",
    )
    server = LedgerHTTPServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(f"http://127.0.0.1:{server.server_port}/wiki/Ne1wrk",
                          headers={"Accept": "text/markdown"})
        with urlopen(request, timeout=2) as response:
            assert response.status == 200 and response.read() == b"served\n"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert not thread.is_alive()


def test_symlink_escape_and_unapproved_redirect_fail_closed(tmp_path):
    storage, app = application(tmp_path)
    artifacts = ArtifactStore(storage.juno_root)
    artifacts.create(record_id="Lo1cal", title="local", profile="report", mode="local",
                     content=b"content", media_type="text/plain")
    object_path = next((storage.juno_root / "objects").glob("sha256/*/*"))
    outside = tmp_path / "outside"
    outside.write_bytes(b"content")
    object_path.unlink()
    object_path.symlink_to(outside)
    assert app.dispatch("GET", "/artifact/Lo1cal/content").status == 409

    # Use a clean fixture so the global symlink refusal does not mask redirect policy.
    storage2, app2 = application(tmp_path / "second")
    ArtifactStore(storage2.juno_root).create(
        record_id="Li1ink", title="link", profile="report", mode="link",
        uri="https://not-approved.example/x", media_type="text/plain")
    assert app2.dispatch("GET", "/artifact/Li1ink/download").status == 403
