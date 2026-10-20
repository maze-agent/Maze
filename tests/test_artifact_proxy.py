import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from maze.core.files.lineage import (
    _artifact_http_session,
    _download_artifact,
    _upload_artifact,
)


@pytest.fixture
def artifact_server(monkeypatch):
    content = b"test artifact bytes"
    digest = hashlib.sha256(content).hexdigest()
    uploaded = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            assert self.headers.get("Authorization") == "Bearer test-capability"
            assert self.path == f"/artifacts/sha256/{digest}"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(content)

        def do_PUT(self):
            assert self.headers.get("Authorization") == "Bearer test-capability"
            uploaded.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"sha256": digest}).encode())

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    # Reproduce the lowercase variable winning over the uppercase bypass list.
    monkeypatch.setenv("no_proxy", "localhost")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.0/8,10.0.0.0/8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = {"artifact_store": {
        "base_url": f"http://127.0.0.1:{server.server_port}",
        "capability": "test-capability",
    }}
    try:
        yield context, content, digest, uploaded
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_private_download_bypasses_internet_proxy(artifact_server, tmp_path):
    context, content, digest, _ = artifact_server
    target = tmp_path / "example.wav"
    _download_artifact(context, {"sha256": digest, "path": "samples/example.wav"}, target)
    assert target.read_bytes() == content


def test_private_upload_bypasses_internet_proxy(artifact_server, tmp_path):
    context, content, digest, uploaded = artifact_server
    source = tmp_path / "output.txt"
    source.write_bytes(content)
    assert _upload_artifact(context, source, digest)["sha256"] == digest
    assert uploaded == [content]


def test_public_artifact_endpoint_keeps_proxy_support():
    with _artifact_http_session({"artifact_store": {"base_url": "https://example.org"}}) as session:
        assert session.trust_env is True


def test_private_https_keeps_configured_ca_bundle(monkeypatch):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/tmp/cluster-ca.pem")
    with _artifact_http_session({"artifact_store": {"base_url": "https://10.1.2.3:8000"}}) as session:
        assert session.trust_env is False
        assert session.verify == "/tmp/cluster-ca.pem"
