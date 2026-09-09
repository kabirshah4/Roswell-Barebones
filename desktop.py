"""Run Roswell as a desktop window.

Starts the server bound to loopback on an OS-assigned port, waits for it to
answer, then opens a native window pointing at it. Falls back to the default
browser when pywebview is unavailable, so this never becomes a hard dependency
of the app itself.

Usage:
    uv run python desktop.py
"""

import logging
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("desktop")

WINDOW_TITLE = "Roswell"
STARTUP_TIMEOUT = 45.0


def reserve_loopback_socket() -> tuple[socket.socket, int]:
    """Bind a loopback socket on a free port and hand it over unclosed.

    Asking the OS for a port, closing it, then telling the server to use that
    number leaves a window where something else can take it. Passing the bound
    socket straight to uvicorn closes that race.

    Loopback only, never 0.0.0.0: this process holds the user's API keys and
    serves an unauthenticated API.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    return sock, sock.getsockname()[1]


def wait_for_health(url: str, timeout: float = STARTUP_TIMEOUT) -> bool:
    """Poll until the server answers, or give up.

    The first run seeds a 500-ticker universe and opens the database, so the
    window must not appear before the app can actually serve it.

    Any HTTP reply counts as up, including 401. This is a liveness check, not
    an authorization one: once the terminal grew a lock screen, a configured
    password made /api/health return 401 and the launcher concluded the server
    had never started — then sat for the full timeout before giving up.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status:
                    return True
        except urllib.error.HTTPError:
            # The server replied; the status is not this function's business.
            return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.25)
    return False


def serve(sock: socket.socket, app: "object | None" = None) -> "object":
    """Start uvicorn on the pre-bound socket in a daemon thread.

    `app` is injectable so a test can serve an app pointed at a scratch
    database. Building it unconditionally here made the launcher test depend on
    whatever was in the developer's real terminal.db — including whether a
    password happened to be set.
    """
    import uvicorn

    from backend.main import create_app

    config = uvicorn.Config(app or create_app(), log_level="info")
    server = uvicorn.Server(config)

    def run() -> None:
        try:
            server.run(sockets=[sock])
        except Exception:
            logger.exception("Server stopped unexpectedly")

    # Daemon: closing the window should end the process, not leave a server
    # holding the port and the database open.
    threading.Thread(target=run, daemon=True, name="uvicorn").start()
    return server


def open_window(url: str) -> bool:
    """Open a native window. False when pywebview is not available."""
    try:
        import webview
    except ImportError:
        logger.info("pywebview not installed; falling back to the browser")
        return False

    webview.create_window(WINDOW_TITLE, url, width=1600, height=1000)
    webview.start()
    return True


def main() -> int:
    sock, port = reserve_loopback_socket()
    url = f"http://127.0.0.1:{port}/"
    logger.info("Starting %s on %s", WINDOW_TITLE, url)

    server = serve(sock)

    if not wait_for_health(url + "api/health"):
        logger.error("Server did not start within %.0fs", STARTUP_TIMEOUT)
        return 1

    if not open_window(url):
        import webbrowser

        webbrowser.open(url)
        print(f"\n{WINDOW_TITLE} is running at {url}\nPress Ctrl+C to stop.\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    # Reached when the window closes (or Ctrl+C in browser mode).
    server.should_exit = True
    logger.info("Shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
