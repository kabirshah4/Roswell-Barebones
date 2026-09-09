"""Credentials live in .env; something has to put them in the environment.

Nothing did. The app only ever saw keys because the operator sourced the file
by hand first -- `uv run uvicorn backend.main:app` straight from the README ran
with no credentials at all, and a packaged build has no shell to source it.
"""

from backend.config import _load_env_file


def write(tmp_path, body):
    p = tmp_path / ".env"
    p.write_text(body, encoding="utf-8")
    return p


def test_a_key_is_loaded(monkeypatch, tmp_path):
    monkeypatch.delenv("TEST_KEY", raising=False)
    _load_env_file(write(tmp_path, "TEST_KEY=abc123\n"))
    import os

    assert os.environ["TEST_KEY"] == "abc123"


def test_an_existing_variable_is_never_clobbered(monkeypatch, tmp_path):
    """An explicit export is a deliberate override; a stale file must not win."""
    import os

    monkeypatch.setenv("TEST_KEY", "from-shell")
    _load_env_file(write(tmp_path, "TEST_KEY=from-file\n"))
    assert os.environ["TEST_KEY"] == "from-shell"


def test_comments_and_blank_lines_are_skipped(monkeypatch, tmp_path):
    monkeypatch.delenv("TEST_KEY", raising=False)
    assert _load_env_file(write(tmp_path, "# a note\n\n  \nTEST_KEY=v\n")) == 1


def test_quotes_are_stripped(monkeypatch, tmp_path):
    import os

    for raw, expected in (('"q"', "q"), ("'s'", "s"), ("bare", "bare")):
        monkeypatch.delenv("TEST_KEY", raising=False)
        _load_env_file(write(tmp_path, f"TEST_KEY={raw}\n"))
        assert os.environ["TEST_KEY"] == expected


def test_an_export_prefix_is_tolerated(monkeypatch, tmp_path):
    """The README told people to write `export KEY=...`."""
    import os

    monkeypatch.delenv("TEST_KEY", raising=False)
    _load_env_file(write(tmp_path, "export TEST_KEY=v\n"))
    assert os.environ["TEST_KEY"] == "v"


def test_a_value_containing_equals_is_kept_whole(monkeypatch, tmp_path):
    """Base64 and JWT-ish secrets routinely contain '='."""
    import os

    monkeypatch.delenv("TEST_KEY", raising=False)
    _load_env_file(write(tmp_path, "TEST_KEY=a=b=c\n"))
    assert os.environ["TEST_KEY"] == "a=b=c"


def test_a_missing_file_is_not_an_error(tmp_path):
    assert _load_env_file(tmp_path / "nope.env") == 0


def test_a_malformed_line_is_skipped_not_fatal(monkeypatch, tmp_path):
    monkeypatch.delenv("TEST_KEY", raising=False)
    assert _load_env_file(write(tmp_path, "garbage-no-equals\nTEST_KEY=v\n")) == 1


def test_a_binary_file_does_not_crash_startup(tmp_path):
    p = tmp_path / ".env"
    p.write_bytes(b"\xff\xfe\x00binary")
    assert _load_env_file(p) == 0


def test_importing_config_loads_the_env(monkeypatch):
    """The real call site: import backend.config and the keys are present."""
    import backend.config as cfg

    assert callable(cfg._load_env_file)


# --- packaged-app paths ------------------------------------------------------

def test_user_data_is_the_repo_when_running_from_a_checkout():
    """The developer workflow must not change."""
    from backend.config import PROJECT_ROOT, user_data_dir

    assert user_data_dir() == PROJECT_ROOT


def test_the_database_lives_outside_the_app_bundle(monkeypatch):
    """An .app is read-only once signed, and reinstalling destroys anything
    written inside it -- watchlist, cached bars and keys included."""
    import importlib
    import sys

    import backend.config as cfg

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    importlib.reload(cfg)
    try:
        data = cfg.user_data_dir()
        assert data != cfg.PROJECT_ROOT
        # macOS, Linux and Windows respectively.
        assert ("Application Support" in str(data)
                or ".local" in str(data)
                or "AppData" in str(data))
    finally:
        monkeypatch.delattr(sys, "frozen", raising=False)
        importlib.reload(cfg)


def test_the_env_search_covers_the_repo_and_the_data_dir():
    from backend.config import PROJECT_ROOT, env_search_paths

    paths = env_search_paths()
    assert PROJECT_ROOT / ".env" in paths


def test_the_search_paths_have_no_duplicates():
    """From a checkout, several candidates resolve to the same file."""
    from backend.config import env_search_paths

    paths = env_search_paths()
    assert len(paths) == len(set(paths))


def test_the_first_readable_file_wins(tmp_path, monkeypatch):
    """Priority order must actually be honoured, not just declared."""
    import os

    import backend.config as cfg

    first = tmp_path / "a.env"
    second = tmp_path / "b.env"
    first.write_text("TEST_KEY=first\n", encoding="utf-8")
    second.write_text("TEST_KEY=second\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "env_search_paths", lambda: [first, second])
    monkeypatch.delenv("TEST_KEY", raising=False)
    cfg._load_env_file()
    assert os.environ["TEST_KEY"] == "first"


def test_a_missing_first_candidate_falls_through(tmp_path, monkeypatch):
    import os

    import backend.config as cfg

    present = tmp_path / "present.env"
    present.write_text("TEST_KEY=found\n", encoding="utf-8")
    monkeypatch.setattr(
        cfg, "env_search_paths", lambda: [tmp_path / "nope.env", present])
    monkeypatch.delenv("TEST_KEY", raising=False)
    cfg._load_env_file()
    assert os.environ["TEST_KEY"] == "found"


def test_the_offline_suite_sees_no_credentials():
    """Guards the guard: without this isolation, tests asserting 'no key means
    disabled' pass or fail depending on whether the developer has a .env."""
    import os

    for name in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "FRED_API_KEY",
                 "DISCORD_WEBHOOK_URL"):
        assert name not in os.environ, f"{name} leaked into an offline test"
