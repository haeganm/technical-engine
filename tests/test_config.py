"""The .env parser -- the most likely first-run failure for a new user."""
import pytest

from engine import config


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    def write(text, encoding="utf-8"):
        (tmp_path / ".env").write_text(text, encoding=encoding)
    return write


def test_missing_file_returns_none(env_file):
    assert config.env("DISCORD_WEBHOOK_URL") is None


def test_plain_value(env_file):
    env_file("DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1/abc\n")
    assert config.env("DISCORD_WEBHOOK_URL").endswith("/1/abc")


def test_quotes_comments_spaces_and_export(env_file):
    env_file("# a comment\n"
             "export  DISCORD_WEBHOOK_URL = 'https://x/1'  \n"
             "OTHER=2\n")
    assert config.env("DISCORD_WEBHOOK_URL") == "https://x/1"
    assert config.env("OTHER") == "2"


def test_value_may_contain_equals(env_file):
    env_file("DISCORD_WEBHOOK_URL=https://x/1?a=b&c=d\n")
    assert config.env("DISCORD_WEBHOOK_URL") == "https://x/1?a=b&c=d"


def test_empty_value_is_none(env_file):
    env_file("DISCORD_WEBHOOK_URL=\n")
    assert config.env("DISCORD_WEBHOOK_URL") is None


def test_utf8_bom_from_notepad(env_file):
    env_file("DISCORD_WEBHOOK_URL=https://x/1\n", encoding="utf-8-sig")
    assert config.env("DISCORD_WEBHOOK_URL") == "https://x/1"


def test_real_environment_wins(env_file, monkeypatch):
    env_file("DISCORD_WEBHOOK_URL=https://from-file\n")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://from-env")
    assert config.env("DISCORD_WEBHOOK_URL") == "https://from-env"


def test_universe_merges_sp500_and_nasdaq100():
    u = config.universe()
    assert len(u) > 450, "S&P 500 + Nasdaq-100 union should be ~520 names"
    assert all(t == t.upper() and "." not in t for t in u)
    assert len(set(u)) == len(u), "no duplicate tickers across the two lists"
    assert "SPY" in u, "context ticker rides along in the fetch list"
    assert "AAPL" in u and "BRK-B" in u
