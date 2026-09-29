"""Node-skill persistence and approval tests; never touch the real shared vault."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from easel.workflow_skills import NODES, NodeSkillStore, WorkflowSkillConflict


@pytest.fixture
def store(tmp_path):
    return NodeSkillStore(tmp_path / "vault" / "_Agent", tmp_path / "project")


def unpack(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name).decode("utf-8") for name in archive.namelist()}


def test_initial_reads_and_exports_leave_shared_and_project_files_untouched(store):
    for node in NODES:
        skill = store.get(node)
        assert skill["node"] == node
        assert skill["name"] == f"easel-node-{node}"
        assert skill["source"] == "builtin"
        assert not skill["personalized"]
        assert skill["inputs"] and skill["outputs"]
        assert skill["version"] == store.get(node)["version"]
    assert len(unpack(store.export())) == 12
    assert not store.personal_root.exists()
    assert not store.project_root.exists()


def test_proposal_is_durable_diff_without_changing_live_skill(store):
    before = store.get("build")
    proposal = store.propose("build", "字幕使用 Noto Sans SC，背景为暖灰色。")
    restarted = NodeSkillStore(store.personal_root, store.project_root)
    assert restarted.get("build") == before
    assert not store.personal_root.exists()
    assert proposal["before"] == before["content"]
    assert proposal["base_version"] == before["version"]
    assert proposal["diff"].startswith("--- a/easel-node-build/SKILL.md\n+++ b/easel-node-build/SKILL.md\n")
    assert "+字幕使用 Noto Sans SC" in proposal["diff"]
    assert restarted.get_proposal("build", proposal["proposal_id"]) == proposal


def test_apply_updates_authority_only_and_future_reads_and_keeps_history(store):
    unrelated = store.personal_root / "skills" / "remotion-video-production" / "SKILL.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("existing shared skill", encoding="utf-8")
    old_execution_snapshot = store.get("build")
    proposal = store.propose("build", "短视频用竖屏安全区，字幕避免挡住脸部。")
    current = store.apply("build", proposal["proposal_id"])
    assert current["personalized"]
    assert current["version"] != old_execution_snapshot["version"]
    assert old_execution_snapshot["content"] == proposal["before"]
    assert current["content"] == proposal["after"]
    assert Path(current["source"]) == store.personal_root / "skills" / "easel-node-build" / "SKILL.md"
    assert unrelated.read_text(encoding="utf-8") == "existing shared skill"
    assert {item["version"] for item in store.history("build")} == {old_execution_snapshot["version"], current["version"]}
    assert store.apply("build", proposal["proposal_id"]) == current


def test_existing_personal_rule_is_read_not_replaced_by_baseline(store):
    proposal = store.propose("script", "开头直接陈述观点，不使用悬念问句。")
    store.apply("script", proposal["proposal_id"])
    other_project = NodeSkillStore(store.personal_root, store.project_root / "another")
    assert "不使用悬念问句" in other_project.get("script")["content"]
    assert other_project.get("script")["source"] == store.get("script")["source"]


def test_stale_proposal_cannot_overwrite_newer_rule(store):
    first = store.propose("script", "正文每段只讲一个观点。")
    stale = store.propose("script", "每篇结尾留一句行动建议。")
    current = store.apply("script", first["proposal_id"])
    with pytest.raises(WorkflowSkillConflict):
        store.apply("script", stale["proposal_id"])
    assert store.get("script") == current


def test_manual_personal_edit_invalidates_reviewed_proposal(store):
    proposal = store.propose("brief", "先明确面向施工工程师。")
    path = Path(store.get("brief")["target_path"])
    path.parent.mkdir(parents=True)
    path.write_text(store.get("brief")["content"] + "\n手动补充的规则。\n", encoding="utf-8")
    with pytest.raises(WorkflowSkillConflict):
        store.apply("brief", proposal["proposal_id"])
    assert "手动补充的规则" in store.get("brief")["content"]


def test_concurrent_cross_project_applies_have_only_one_winner(store):
    other = NodeSkillStore(store.personal_root, store.project_root / "other")
    first = store.propose("deliver", "横版封面保留大标题。")
    second = other.propose("deliver", "竖版封面保留上方安全区。")

    def apply(which, proposal):
        try:
            return which.apply("deliver", proposal["proposal_id"])["version"]
        except WorkflowSkillConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: apply(*pair), [(store, first), (other, second)]))
    assert results.count("conflict") == 1
    assert store.get("deliver")["version"] in results


def test_export_only_current_selected_skills_with_portable_manifest(store):
    accepted = store.propose("script", "用短句描述操作。")
    current = store.apply("script", accepted["proposal_id"])
    pending = store.propose("script", "这个待审核规则不得导出。")
    (store.project_root / "private-recording.mp4").write_bytes(b"private media")
    files = unpack(store.export(["script", "publish", "script"]))
    assert set(files) == {"skills/easel-node-script/SKILL.md", "skills/easel-node-publish/SKILL.md", "manifest.json", "INSTALL.md"}
    assert files["skills/easel-node-script/SKILL.md"] == current["content"]
    assert pending["instruction"] not in "".join(files.values())
    assert "private media" not in "".join(files.values())
    assert str(store.personal_root) not in "".join(files.values())
    manifest = json.loads(files["manifest.json"])
    assert [entry["node"] for entry in manifest["skills"]] == ["script", "publish"]
    assert manifest["skills"][0]["version"] == current["version"]
    assert manifest["skills"][1]["dependencies"]
    assert "不包含 Easel video-pipeline-sdk" in files["INSTALL.md"]


def test_build_export_bundles_only_portable_template_source(store):
    template = store.project_root / "assets" / "workflow-template"
    portable = {
        "package.json": '{"dependencies":{"remotion":"4.0.503"}}',
        "README.md": "# 两套模板\n媒体与中文字体须由目标环境提供。\n",
        "src/index.ts": "import {registerRoot} from 'remotion';\nimport {Root} from './Root';\nregisterRoot(Root);\n",
        "src/Root.tsx": "export const variants = ['documentary', 'editorial'];\n",
    }
    private = {
        "public/source.mp4": "private recording",
        "node_modules/package/index.js": "runtime dependency",
        "props.json": "private captions",
        "src/runtime/secret.ts": "nested private source",
        "src/private.json": "private metadata",
        ".env": "KEY=local-secret",
    }
    for relative, content in {**portable, **private}.items():
        path = template / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    files = unpack(store.export(["build"]))
    prefix = "templates/workflow-template/"
    assert {name: content for name, content in files.items() if name.startswith(prefix)} == {
        prefix + relative: content for relative, content in portable.items()
    }
    details = json.loads(files["manifest.json"])["templates"][0]
    assert details["bundled"] and details["complete_source"] and details["readme_bundled"]
    assert details["missing_required_files"] == []
    for record in details["files"]:
        assert record["sha256"] == hashlib.sha256(files[record["path"]].encode()).hexdigest()
    for content in private.values():
        assert content not in "".join(files.values())
    assert str(store.project_root) not in "".join(files.values())
    assert not store.personal_root.exists()
    assert not any(name.startswith(prefix) for name in unpack(store.export(["script"])))


def test_missing_template_is_a_declared_dependency_not_an_export_error(store):
    files = unpack(store.export(["build"]))
    details = json.loads(files["manifest.json"])["templates"][0]
    assert not details["bundled"] and not details["complete_source"]
    assert details["reason"] == "template_source_unavailable"
    assert not store.project_root.exists()
    assert not store.personal_root.exists()


def test_template_source_symlink_cannot_export_arbitrary_local_file(store, tmp_path):
    private = tmp_path / "private-notes.txt"
    private.write_text("private notes must remain local", encoding="utf-8")
    src = store.project_root / "assets" / "workflow-template" / "src"
    src.mkdir(parents=True)
    try:
        (src / "Root.tsx").symlink_to(private)
    except OSError:
        pytest.skip("OS does not permit symlink creation")
    with pytest.raises(ValueError):
        store.export(["build"])


def test_credential_accidentally_added_to_template_is_not_exported(store):
    path = store.project_root / "assets" / "workflow-template" / "src" / "Root.tsx"
    path.parent.mkdir(parents=True)
    path.write_text('const API_KEY = "local-secret-123";', encoding="utf-8")
    with pytest.raises(ValueError, match="凭证"):
        store.export(["build"])


def test_context_retains_only_references_never_private_material(store):
    proposal = store.propose("review", "反馈附上片段的时间位置。", context={
        "run_id": "run_123", "feedback_id": "feedback-4",
        "draft": "未公开的原稿全文", "transcript": "private recording words",
        "api_key": "sensitive-value", "node_run_id": "sk-do-not-copy-this-secret",
    })
    assert proposal["context"] == {"run_id": "run_123", "feedback_id": "feedback-4"}
    all_stored = "".join(path.read_text(encoding="utf-8") for path in store.proposal_root.glob("*.json"))
    for private in ("未公开的原稿全文", "private recording words", "sensitive-value", "sk-do-not-copy-this-secret"):
        assert private not in all_stored
    store.apply("review", proposal["proposal_id"])
    assert "run_123" not in "".join(unpack(store.export(["review"])).values())


@pytest.mark.parametrize("instruction", [
    "API_KEY=secret-value-123", "password = abcdefg", "密码： abcdefgh",
    "使用 sk-abcdefghijklmnopqrstuv", "Authorization: Bearer abcdefghijklmnop",
    "-----BEGIN RSA PRIVATE KEY-----\nsecret\n-----END RSA PRIVATE KEY-----",
])
def test_credential_literals_rejected_before_any_persistence(store, instruction):
    with pytest.raises(ValueError, match="凭证"):
        store.propose("publish", instruction)
    assert not store.personal_root.exists()
    assert not store.project_root.exists()


@pytest.mark.parametrize("node", ["../build", "other", "build/../script", "", None])
def test_invalid_node_cannot_escape_store(store, node):
    with pytest.raises(ValueError):
        store.get(node)
    assert not store.personal_root.exists()


def test_invalid_or_cross_node_proposal_does_not_write_shared_files(store):
    proposal = store.propose("script", "句子简洁。")
    with pytest.raises(ValueError):
        store.apply("build", proposal["proposal_id"])
    with pytest.raises(ValueError):
        store.apply("script", "../proposal")
    assert not store.personal_root.exists()


def test_corrupted_proposal_cannot_apply_without_new_diff(store):
    proposal = store.propose("storyboard", "每张信息卡只放一个重点。")
    proposal["after"] += "\nunreviewed change\n"
    (store.proposal_root / f"{proposal['proposal_id']}.json").write_text(json.dumps(proposal), encoding="utf-8")
    with pytest.raises(WorkflowSkillConflict):
        store.apply("storyboard", proposal["proposal_id"])
    assert not store.personal_root.exists()


def test_secret_manually_inserted_in_personal_skill_is_not_exported(store):
    baseline = store.get("publish")
    path = Path(baseline["target_path"])
    path.parent.mkdir(parents=True)
    path.write_text(baseline["content"] + "\nAPI_KEY=do-not-export-secret\n", encoding="utf-8")
    with pytest.raises(ValueError, match="凭证"):
        store.export(["publish"])


def test_empty_export_and_empty_instruction_are_rejected(store):
    with pytest.raises(ValueError):
        store.export([])
    with pytest.raises(ValueError):
        store.export("script")
    with pytest.raises(ValueError):
        store.propose("script", " ")


def test_node_skill_symlink_cannot_overwrite_existing_shared_skill(store, tmp_path):
    target = tmp_path / "other-shared-skill"
    target.mkdir()
    (target / "SKILL.md").write_text("untouched", encoding="utf-8")
    link = store.personal_root / "skills" / "easel-node-script"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit symlink creation")
    with pytest.raises(ValueError):
        store.get("script")
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "untouched"
