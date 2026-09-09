"""The desktop launcher. Nothing here starts a real server except the smoke test."""

import socket

import pytest

import desktop


# --- port reservation --------------------------------------------------------

def test_it_reserves_a_usable_loopback_port():
    sock, port = desktop.reserve_loopback_socket()
    try:
        assert 1024 < port < 65536
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()


def test_it_binds_loopback_only_never_all_interfaces():
    """This process holds the user's API keys and serves an unauthenticated API."""
    sock, _ = desktop.reserve_loopback_socket()
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
        assert sock.getsockname()[0] != "0.0.0.0"
    finally:
        sock.close()


def test_the_socket_is_handed_over_still_bound():
    """Closing it and passing the number leaves a window for a port thief."""
    sock, port = desktop.reserve_loopback_socket()
    try:
        rival = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(OSError):
            rival.bind(("127.0.0.1", port))
        rival.close()
    finally:
        sock.close()


def test_two_reservations_do_not_collide():
    a, port_a = desktop.reserve_loopback_socket()
    b, port_b = desktop.reserve_loopback_socket()
    try:
        assert port_a != port_b
    finally:
        a.close()
        b.close()


def test_no_hardcoded_port():
    """Port 8000 is frequently already in use; the app must not assume it."""
    source = __import__("pathlib").Path("desktop.py").read_text()
    assert "8000" not in source


# --- health wait -------------------------------------------------------------

def test_it_gives_up_rather_than_hanging_forever():
    sock, port = desktop.reserve_loopback_socket()
    sock.close()   # nothing is listening
    assert desktop.wait_for_health(f"http://127.0.0.1:{port}/api/health",
                                   timeout=0.5) is False


def test_a_connection_refused_is_retried_not_raised():
    """The server takes seconds to boot; the first probes always fail."""
    assert desktop.wait_for_health("http://127.0.0.1:1/api/health",
                                   timeout=0.3) is False


def test_the_startup_timeout_allows_for_a_first_run():
    """First run seeds a 500-ticker universe before it can serve anything."""
    assert desktop.STARTUP_TIMEOUT >= 30


# --- window ------------------------------------------------------------------

def test_a_missing_pywebview_falls_back_rather_than_crashing(monkeypatch):
    """pywebview must not become a hard dependency of the app."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "webview":
            raise ImportError("no pywebview")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert desktop.open_window("http://127.0.0.1:1/") is False


def test_pywebview_is_not_a_project_dependency():
    """The web app must keep working on a machine that cannot build it."""
    deps = __import__("pathlib").Path("pyproject.toml").read_text()
    project = deps.split("[dependency-groups]")[0]
    assert "pywebview" not in project


def test_the_server_thread_is_a_daemon():
    """Closing the window must end the process, not orphan a server holding
    the port and the database."""
    source = __import__("pathlib").Path("desktop.py").read_text()
    assert "daemon=True" in source


# --- end to end --------------------------------------------------------------

def test_the_launcher_serves_a_working_app(tmp_path, monkeypatch):
    """Boots the real server on a real port and fetches the real index.

    Against a scratch database: pointed at the developer's own, this passed or
    failed depending on whether a password was set.
    """
    import json
    import urllib.request

    from backend.config import Config
    from backend.main import create_app

    sock, port = desktop.reserve_loopback_socket()
    server = desktop.serve(sock, create_app(cfg=Config(db_path=tmp_path / "t.db"),
                                            start_poller=False))
    url = f"http://127.0.0.1:{port}/"
    try:
        assert desktop.wait_for_health(url + "api/health"), "server never came up"
        with urllib.request.urlopen(url + "api/health") as r:
            assert json.load(r)["status"] == "ok"
        with urllib.request.urlopen(url) as r:
            assert r.status == 200
            assert b"ROSWELL" in r.read()
    finally:
        server.should_exit = True


# --- packaging ---------------------------------------------------------------

def test_the_spec_bundles_the_files_read_from_disk_at_runtime():
    """The frontend and schema are not importable modules, so PyInstaller's
    static analysis cannot find them -- without these the app boots and then
    404s on every page."""
    spec = __import__("pathlib").Path("roswell.spec").read_text()
    for asset in ("frontend", "backend/db/schema.sql", "backend/data"):
        assert asset in spec, f"{asset} is read at runtime and must be bundled"


def test_the_spec_collects_the_runtime_resolved_imports():
    """yfinance, uvicorn and google-genai all resolve pieces at runtime."""
    spec = __import__("pathlib").Path("roswell.spec").read_text()
    for module in ("uvicorn", "yfinance", "google.genai"):
        assert module in spec


def test_build_artifacts_are_gitignored():
    """dist/ can hold a .env copied next to the .app."""
    ignored = __import__("pathlib").Path(".gitignore").read_text()
    assert "dist/" in ignored and "build/" in ignored


def test_a_locked_server_still_counts_as_up(tmp_path):
    """Liveness, not authorization. When the terminal grew a lock screen a
    configured password made /api/health return 401, and the launcher sat for
    the whole timeout before reporting that the server never started."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Locked(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"detail": "Not authenticated"}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Locked)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/health"
        assert desktop.wait_for_health(url, timeout=3) is True
    finally:
        server.shutdown()
