"""CLI smoke tests: every offline command must work with no key and no network."""

from __future__ import annotations

import json

import pytest

from opus5lean.cli import main

PROMPT = """\
# System

You   are   a   helpful   assistant.


Follow  the  rules.
"""


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    """Guarantee these tests exercise the offline paths."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def prompt_file(tmp_path):
    p = tmp_path / "prompt.md"
    p.write_text(PROMPT, encoding="utf-8")
    return str(p)


def test_models(capsys):
    assert main(["models"]) == 0
    out = capsys.readouterr().out
    assert "claude-opus-5" in out and "512" in out


def test_passes(capsys):
    assert main(["passes"]) == 0
    out = capsys.readouterr().out
    assert "unicode" in out and "aggressive" in out


def test_count(capsys, prompt_file):
    assert main(["count", prompt_file]) == 0
    out = capsys.readouterr().out
    assert "tokens" in out and "estimate" in out
    assert "chars/token" in out


def test_count_json(capsys, prompt_file):
    assert main(["count", prompt_file, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["tokens"] > 0
    assert data["method"] == "estimate"
    assert data["model"] == "claude-opus-5"


def test_count_exact_falls_back_without_a_key(capsys, prompt_file):
    """--exact must degrade gracefully, and say that it did."""
    assert main(["count", prompt_file, "--exact"]) == 0
    assert "estimate" in capsys.readouterr().out


def test_count_missing_file():
    with pytest.raises(SystemExit, match="no such file"):
        main(["count", "/nonexistent/x.md"])


def test_slim_dry_run_writes_nothing(capsys, prompt_file, tmp_path):
    assert main(["slim", prompt_file]) == 0
    out = capsys.readouterr().out
    assert "saved" in out and "dry run" in out
    assert not list(tmp_path.glob("*.lean.md"))


def test_slim_writes_output(capsys, prompt_file, tmp_path):
    dest = tmp_path / "out.md"
    assert main(["slim", prompt_file, "-o", str(dest)]) == 0
    assert "You are a helpful assistant." in dest.read_text()


def test_slim_stdout_emits_only_the_text(capsys, prompt_file):
    assert main(["slim", prompt_file, "--stdout"]) == 0
    out = capsys.readouterr().out
    assert "saved" not in out
    assert "helpful assistant" in out


def test_slim_min_saving_gate(prompt_file):
    assert main(["slim", prompt_file, "--min-saving", "0.1"]) == 0
    assert main(["slim", prompt_file, "--min-saving", "99"]) == 1


def test_slim_unknown_pass_exits_two(capsys, prompt_file):
    assert main(["slim", prompt_file, "--only", "bogus"]) == 2
    assert "unknown pass" in capsys.readouterr().err


def test_cost(capsys):
    assert main(["cost", "-i", "1000000", "-o", "1000000"]) == 0
    out = capsys.readouterr().out
    assert "$5.00" in out and "$25.00" in out and "$30.00" in out


def test_cost_unknown_model_exits_two(capsys):
    assert main(["cost", "-m", "gpt-9", "-i", "10"]) == 2
    assert "unknown model" in capsys.readouterr().err


def test_cache_plan(capsys, tmp_path):
    spec = tmp_path / "segments.json"
    spec.write_text(json.dumps([
        {"name": "tools", "tokens": 4000, "volatility": 0},
        {"name": "system", "tokens": 6000, "volatility": 0},
        {"name": "turn", "tokens": 500, "volatility": 3},
    ]))
    assert main(["cache", str(spec)]) == 0
    out = capsys.readouterr().out
    assert "cache_control" in out and "Break-even" in out


def test_cache_plan_json(capsys, tmp_path):
    spec = tmp_path / "segments.json"
    spec.write_text(json.dumps({
        "model": "claude-opus-5",
        "ttl": "1h",
        "segments": [
            {"name": "system", "tokens": 8000, "volatility": 0},
            {"name": "turn", "tokens": 500, "volatility": 3},
        ],
    }))
    assert main(["cache", str(spec), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["cached_tokens"] == 8000
    assert data["break_even_hits"] == 2
    assert data["breakpoints"][0]["after"] == "system"


def test_cache_counts_inline_text_segments(capsys, tmp_path):
    spec = tmp_path / "segments.json"
    spec.write_text(json.dumps([
        {"name": "system", "text": "You are helpful. " * 200, "volatility": 0},
        {"name": "turn", "text": "hi", "volatility": 3},
    ]))
    assert main(["cache", str(spec), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["cached_tokens"] > 500


def test_cache_reads_segment_files(capsys, tmp_path, prompt_file):
    spec = tmp_path / "segments.json"
    spec.write_text(json.dumps([
        {"name": "system", "file": prompt_file, "volatility": 0},
        {"name": "turn", "tokens": 50, "volatility": 3},
    ]))
    assert main(["cache", str(spec), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total_tokens"] > 50


def test_cache_monthly_projection(capsys, tmp_path):
    spec = tmp_path / "segments.json"
    spec.write_text(json.dumps([
        {"name": "system", "tokens": 30000, "volatility": 0},
        {"name": "turn", "tokens": 500, "volatility": 3},
    ]))
    assert main(["cache", str(spec), "--rpd", "5000"]) == 0
    out = capsys.readouterr().out
    assert "requests/day" in out and "saved" in out


def test_cache_strict_fails_on_warnings(tmp_path):
    spec = tmp_path / "segments.json"
    spec.write_text(json.dumps([
        {"name": "turn", "tokens": 100, "volatility": 3},
        {"name": "system", "tokens": 9000, "volatility": 0},
    ]))
    assert main(["cache", str(spec)]) == 0
    assert main(["cache", str(spec), "--strict"]) == 1


def test_sweep_without_key_explains_itself(prompt_file):
    with pytest.raises(SystemExit, match="Offline commands"):
        main(["sweep", prompt_file])


def test_sweep_rejects_bad_effort_level(prompt_file):
    """Config errors must surface before any file or stdin read."""
    with pytest.raises(SystemExit, match="unknown effort"):
        main(["sweep", prompt_file, "--levels", "ludicrous"])
