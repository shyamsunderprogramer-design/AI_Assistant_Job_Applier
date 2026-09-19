"""Writing .env must not damage what is already in it.

The bug this exists to prevent happened for real: a value appended to a file
whose last line had no trailing newline produced

    MAIL_APP_PASSWORD=abcdRESULTS_PASSPHRASE=xyz

and mail authentication stopped working silently for a day.
"""

import stat

import pytest

from config.envfile import read_env, write_env


def test_appending_to_a_file_with_no_trailing_newline(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"MAIL_APP_PASSWORD=abcd")        # no newline, as it was
    write_env(env, {"RESULTS_PASSPHRASE": "xyz"})

    assert read_env(env) == {"MAIL_APP_PASSWORD": "abcd", "RESULTS_PASSPHRASE": "xyz"}
    assert env.read_text().endswith("\n")


def test_comments_and_unknown_keys_survive(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# how to get a key\nANTHROPIC_API_KEY=old\n\n"
                   "# something we do not manage\nCUSTOM_THING=keep me\n")
    write_env(env, {"ANTHROPIC_API_KEY": "new"})

    text = env.read_text()
    assert "# how to get a key" in text
    assert "CUSTOM_THING=keep me" in text
    assert read_env(env)["ANTHROPIC_API_KEY"] == "new"
    # Rewritten in place, so the comment above it still explains it.
    assert text.index("# how to get a key") < text.index("ANTHROPIC_API_KEY=new")


def test_none_removes_a_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\nB=2\n")
    assert write_env(env, {"A": None}) == ["A"]
    assert read_env(env) == {"B": "2"}


def test_returns_only_what_actually_changed(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    assert write_env(env, {"A": "1"}) == []          # same value, no write
    assert write_env(env, {"A": "2"}) == ["A"]


def test_values_needing_quotes_survive_a_round_trip(tmp_path):
    env = tmp_path / ".env"
    tricky = 'pass word "with" #hash and = sign'
    write_env(env, {"SECRET": tricky})
    assert read_env(env)["SECRET"] == tricky


def test_export_and_quoted_forms_are_read(tmp_path):
    env = tmp_path / ".env"
    env.write_text("export A=1\nB='two'\nC=\"three\"\n#D=commented\n")
    values = read_env(env)
    assert values == {"A": "1", "B": "two", "C": "three"}


def test_a_new_file_is_owner_only(tmp_path):
    env = tmp_path / ".env"
    write_env(env, {"ANTHROPIC_API_KEY": "sk-secret"})
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_missing_file_reads_as_empty(tmp_path):
    assert read_env(tmp_path / "nope") == {}
