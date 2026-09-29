from __future__ import annotations

import inspect
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "web"))
sys.path.insert(0, str(PROJECT_ROOT / "skills" / "shared" / "scripts"))

import app as web  # noqa: E402
import web_publisher  # noqa: E402


def test_proxy_env_forces_utf8_over_legacy_windows_encoding(monkeypatch):
    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.setenv("PYTHONIOENCODING", "gbk")

    env = web._proxy_env()

    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_success_log_cannot_turn_success_into_error():
    class GbkOnlyStream:
        def write(self, text):
            raise UnicodeEncodeError("gbk", text, 0, 1, "illegal multibyte sequence")

        def flush(self):
            pass

    # A logging-only UnicodeEncodeError must not escape into cmd_login_qr's broad
    # exception handler, which would overwrite an already-written success state.
    web_publisher._best_effort_print("✅ 微信视频号登录成功", file=GbkOnlyStream())


def test_windows_service_launcher_enables_utf8():
    launcher = (PROJECT_ROOT / "scripts" / "easel-services.ps1").read_text(encoding="utf-8")
    assert "$env:PYTHONUTF8 = '1'" in launcher
    assert "$env:PYTHONIOENCODING = 'utf-8'" in launcher


def test_channels_publish_enforces_single_submit():
    source = inspect.getsource(web_publisher._publish_weixin_channels)
    assert "submit_attempted" in source
    assert source.count('page.keyboard.press("Enter")') == 1
    assert "未自动重发" in source


def test_channels_draft_is_separate_and_requires_readback():
    source = inspect.getsource(web_publisher._publish_weixin_channels)
    readback = inspect.getsource(web_publisher._weixin_draft_readback)
    assert 'target_text = "保存草稿" if save_draft else "发表"' in source
    assert "button.click" in source
    assert "_weixin_draft_readback" in source
    assert "草稿箱已找到" in readback


def test_channels_create_readiness_uses_real_controls_not_route_only():
    source = inspect.getsource(web_publisher._weixin_wait_create_ready)
    assert 'input[type=file]' in source
    assert "attempts: int = 3" in source
    assert "wait_for_url" not in source


def test_channels_short_title_replaces_unsupported_punctuation():
    assert web_publisher._weixin_short_title("你看到的90%只是演示，不是测试") == \
        "你看到的90%只是演示 不是测试"
    assert web_publisher._weixin_short_title("《测试》：温度30℃？") == "《测试》：温度30℃？"


def test_channels_skill_uses_project_interpreter():
    skill = (PROJECT_ROOT / "skills" / "openclaw" / "skill-channels-upload" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    agents = (PROJECT_ROOT / "openclaw" / "workspace" / "AGENTS.md").read_text(encoding="utf-8")
    assert ".venv\\Scripts\\python.exe" in skill
    assert "python $WP" not in skill
    assert "Hermes venv" in skill
    assert ".venv\\Scripts\\python.exe" in agents
    assert "--draft" in skill
    assert "草稿箱回读" in skill


def test_gateway_naked_restart_delegates_to_protected_launcher():
    script = (PROJECT_ROOT / "scripts" / "gateway.ps1").read_text(encoding="utf-8")
    assert "function Test-ProtectedEnvironment" in script
    assert "easel-services.ps1" in script
    assert "SecretSurfaceUnavailableError" in script
