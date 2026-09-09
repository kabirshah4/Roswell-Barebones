from pathlib import Path

from backend.config import Config, config


def test_config_has_sane_defaults():
    assert config.quote_interval_seconds == 45.0
    assert config.sparkline_every_n_cycles == 7
    assert config.backoff_ceiling_seconds == 300.0
    assert config.host == "127.0.0.1"
    assert config.port == 8000


def test_config_paths_are_absolute():
    assert config.db_path.is_absolute()
    assert config.frontend_dir.is_absolute()
    assert config.frontend_dir.name == "frontend"


def test_config_is_immutable():
    import dataclasses
    import pytest

    with pytest.raises(dataclasses.FrozenInstanceError):
        config.port = 9999  # type: ignore[misc]


def test_config_can_be_overridden_for_tests(tmp_path: Path):
    custom = Config(db_path=tmp_path / "t.db", quote_interval_seconds=1.0)
    assert custom.db_path == tmp_path / "t.db"
    assert custom.quote_interval_seconds == 1.0
    assert custom.port == 8000


def test_the_packaged_data_dir_is_named_roswell_on_every_platform(monkeypatch):
    """One app, one name.

    Windows previously wrote to %APPDATA%\\Obelisk -- a leftover from an
    earlier name -- so a packaged Windows build put its database somewhere
    no other platform, and no part of the documentation, referred to.
    """
    from backend import config as config_module

    monkeypatch.setattr(config_module, "IS_FROZEN", True)

    for platform, expected in (
        ("darwin", "Roswell"),
        ("win32", "Roswell"),
        ("linux", "roswell"),
    ):
        monkeypatch.setattr(config_module.sys, "platform", platform)
        assert config_module.user_data_dir().name == expected
