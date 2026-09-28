from __future__ import annotations

import subprocess

from easel.commands import ping


def test_step_decodes_openclaw_output_as_utf8(monkeypatch):
    seen = {}

    def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, stdout="PONG", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert ping._step("agent", ["openclaw"]) is True
    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "replace"


def test_ping_uses_ascii_safe_status_marker(monkeypatch, capsys):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(ping.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(ping, "openclaw_base_cmd", lambda: ["openclaw"])
    monkeypatch.setattr(ping, "_step", lambda *_args, **_kwargs: True)

    assert ping.cmd_ping(None) == 0
    output = capsys.readouterr().out
    assert "[OK]" in output
    assert "✓" not in output
