import json
from pathlib import Path

import pytest

from easel.workflow_archive import ArchiveConflict, ArchiveError, apply_archive, preview_archive


@pytest.fixture
def archive_case(tmp_path):
    vault = tmp_path / "vault"
    root = tmp_path / "easel"
    (vault / "3-输出").mkdir(parents=True)
    root.mkdir()
    # Existing user-authored records and dashboard must remain byte-identical.
    (vault / "3-输出" / "001_20260906_已有作品.md").write_text(
        "---\nproject_id: 1\ncreate_date: 2026-09-01\nstatus: 已发布\nplatform: [视频号]\n---\n原文\n",
        encoding="utf-8",
    )
    (vault / "3-输出" / "📊自媒体发布总览.md").write_text("用户的 Dataview 看板\n", encoding="utf-8")
    project = {
        "id": "video-abc", "title": "企业 AI 的流程", "kind": "video",
        "created_at": "2026-09-29T08:00:00Z", "content_version": 2,
        "primary_manuscript_id": "m2",
        "manuscripts": [
            {"id": "m1", "title": "初稿", "content": "不要归档的旧稿", "version": 1},
            {"id": "m2", "title": "最终稿", "content": "先把流程跑通，再考虑降低模型成本。", "version": 2},
        ],
        "nodes": [], "settings": {"platforms": ["视频号"]},
    }
    return project, vault, root


def receipt_path(project, root):
    return root / "outputs" / "视频工作流" / project["id"] / "artifacts" / "publication-receipt.json"


def write_receipt(project, root, **overrides):
    path = receipt_path(project, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {"workflow_id": project["id"], "action": "publish", "outcome": "published",
               "verified": True, "platform": "weixin-channels", "content_version": 2,
               "verified_at": "2026-09-29T09:00:00Z", "evidence": {"public_url": "https://example.com/video"}}
    receipt.update(overrides)
    path.write_text(json.dumps(receipt), encoding="utf-8")


def test_preview_is_read_only_and_matches_existing_dashboard_schema(archive_case):
    project, vault, root = archive_case
    before = {str(path): path.read_bytes() for path in vault.rglob("*") if path.is_file()}
    plan = preview_archive(project, vault, root)
    assert plan["status"] == "ready"
    assert plan["targets"][0]["action"] == "create"
    assert Path(plan["plan"]["note_path"]).name == "002_20260929_企业 AI 的流程.md"
    assert Path(plan["plan"]["note_path"]).parent == vault / "3-输出" / "草稿"
    assert "project_id: 2\ncreate_date: 2026-09-29\nstatus: 待发布\nplatform:" in plan["content"]
    assert "## 视频脚本" in plan["content"]
    assert "不要归档的旧稿" not in plan["content"]
    assert "先把流程跑通" in plan["content"]
    assert not list(root.iterdir())
    assert before == {str(path): path.read_bytes() for path in vault.rglob("*") if path.is_file()}


def test_apply_is_idempotent_and_preserves_originals(archive_case):
    project, vault, root = archive_case
    originals = {str(path): path.read_bytes() for path in vault.rglob("*.md")}
    plan = preview_archive(project, vault, root)
    result = apply_archive(project, vault, root, plan["hash"])
    assert result["status"] == "applied"
    note = Path(result["plan"]["note_path"])
    stamp = note.stat().st_mtime_ns
    repeat = apply_archive(project, vault, root)
    assert repeat["status"] == "unchanged"
    assert note.stat().st_mtime_ns == stamp
    assert len(list((vault / "3-输出" / "草稿").glob("*.md"))) == 1
    assert all(Path(path).read_bytes() == value for path, value in originals.items())
    assert project["manuscripts"][0]["content"] == "不要归档的旧稿"


def test_owned_archive_updates_same_path_without_moving_original(archive_case):
    project, vault, root = archive_case
    first = apply_archive(project, vault, root)
    note = Path(first["plan"]["note_path"])
    project["title"] = "修订标题"
    project["manuscripts"][1]["content"] = "经本人修订后的稿件。"
    preview = preview_archive(project, vault, root)
    assert preview["targets"][0]["action"] == "update"
    result = apply_archive(project, vault, root, preview["hash"])
    assert Path(result["plan"]["note_path"]) == note
    assert "经本人修订后的稿件" in note.read_text(encoding="utf-8")


def test_user_edit_is_never_overwritten(archive_case):
    project, vault, root = archive_case
    applied = apply_archive(project, vault, root)
    note = Path(applied["plan"]["note_path"])
    note.write_text("本人在 Obsidian 中改写的内容", encoding="utf-8")
    assert preview_archive(project, vault, root)["status"] == "conflict"
    with pytest.raises(ArchiveConflict):
        apply_archive(project, vault, root)
    assert note.read_text(encoding="utf-8") == "本人在 Obsidian 中改写的内容"


def test_existing_unowned_note_is_never_overwritten(archive_case):
    project, vault, root = archive_case
    project["settings"]["archive"] = {"target_path": "3-输出/001_20260906_已有作品.md"}
    plan = preview_archive(project, vault, root)
    assert plan["status"] == "conflict"
    with pytest.raises(ArchiveConflict):
        apply_archive(project, vault, root)


def test_preview_hash_protects_changed_manuscript_and_publication_evidence(archive_case):
    project, vault, root = archive_case
    old = preview_archive(project, vault, root)
    project["manuscripts"][1]["content"] += " 新内容。"
    with pytest.raises(ArchiveConflict, match="预览已过期"):
        apply_archive(project, vault, root, old["hash"])
    assert not list((vault / "3-输出").glob("草稿/*"))
    current = preview_archive(project, vault, root)
    write_receipt(project, root)
    with pytest.raises(ArchiveConflict, match="预览已过期"):
        apply_archive(project, vault, root, current["hash"])


@pytest.mark.parametrize("field", ["publication", "archive"])
def test_user_supplied_published_flag_is_not_evidence(archive_case, field):
    project, vault, root = archive_case
    project[field] = {"status": "published", "verified": True, "outcome": "published"}
    plan = preview_archive(project, vault, root)
    assert plan["plan"]["publication_status"] == "待发布"
    assert "status: 已发布" not in plan["content"]


@pytest.mark.parametrize("overrides", [
    {"verified": False}, {"outcome": "submitted"}, {"outcome": "unknown"},
    {"workflow_id": "another-project"}, {"content_version": 1}, {"evidence": {}},
    {"verified_at": ""}, {"action": "platform_draft", "outcome": "published"},
])
def test_only_version_matched_verified_publication_is_published(archive_case, overrides):
    project, vault, root = archive_case
    write_receipt(project, root, **overrides)
    assert preview_archive(project, vault, root)["plan"]["publication_status"] != "已发布"


def test_verified_receipt_publishes_only_its_platform(archive_case):
    project, vault, root = archive_case
    write_receipt(project, root)
    plan = preview_archive(project, vault, root)
    assert plan["plan"]["publication_status"] == "已发布"
    assert Path(plan["plan"]["note_path"]).parent.name == "已发布"
    project["settings"]["platforms"].append("抖音")
    mixed = preview_archive(project, vault, root)
    assert mixed["plan"]["publication_status"] == "部分已发布"
    assert "| 抖音 | 待发布 |" in mixed["content"]


def test_draft_receipt_is_not_publication(archive_case):
    project, vault, root = archive_case
    write_receipt(project, root, action="platform_draft", outcome="draft_saved")
    plan = preview_archive(project, vault, root)
    assert plan["plan"]["publication_status"] == "已存草稿"
    assert Path(plan["plan"]["note_path"]).parent.name == "草稿"


def test_article_can_archive_before_any_execution_nodes(archive_case):
    project, vault, root = archive_case
    project["kind"] = "article"
    project["nodes"] = [{"id": "render", "status": "pending"}]
    result = apply_archive(project, vault, root)
    assert result["plan"]["publication_status"] == "待审核"
    assert "## 正文" in result["content"]


def test_multiple_manuscripts_need_unambiguous_primary(archive_case):
    project, vault, root = archive_case
    project.pop("primary_manuscript_id")
    with pytest.raises(ArchiveError, match="唯一主稿"):
        preview_archive(project, vault, root)
    project["manuscripts"][0]["is_primary"] = True
    assert "不要归档的旧稿" in preview_archive(project, vault, root)["content"]
    project["primary_manuscript_id"] = "m2"
    with pytest.raises(ArchiveError, match="不一致"):
        preview_archive(project, vault, root)


@pytest.mark.parametrize("target", ["../outside.md", "3-输出/../../outside.md", "_Agent/MEMORY.md", "3-输出/test.txt"])
def test_target_paths_cannot_escape_output_directory(archive_case, target):
    project, vault, root = archive_case
    project["settings"]["archive"] = {"target_path": target}
    with pytest.raises(ArchiveError):
        preview_archive(project, vault, root)


def test_symlink_target_escape_is_rejected(archive_case, tmp_path):
    project, vault, root = archive_case
    outside = tmp_path / "outside"
    outside.mkdir()
    link = vault / "3-输出" / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires privileges on this Windows installation")
    project["settings"]["archive"] = {"target_path": "3-输出/escape/file.md"}
    with pytest.raises(ArchiveError):
        preview_archive(project, vault, root)


def test_media_links_use_existing_files_without_copying_video(archive_case):
    project, vault, root = archive_case
    video = root / "final.mp4"
    video.write_bytes(b"fake-test-video")
    cover = vault / "cover.jpg"
    cover.write_bytes(b"fake-test-cover")
    log = root / "internal.log"
    log.write_text("internal access_token=secret-secret", encoding="utf-8")
    project["nodes"] = [{"id": "render", "artifacts": [
        {"name": "最终视频", "kind": "final_video", "path": str(video)},
        {"name": "竖版封面", "kind": "cover_vertical", "path": str(cover)},
        {"kind": "log", "path": str(log)},
    ]}]
    result = apply_archive(project, vault, root)
    assert video.as_uri() in result["content"]
    assert "![[cover.jpg]]" in result["content"]
    assert "access_token" not in result["content"]
    assert not list(vault.rglob("*.mp4"))
    assert video.read_bytes() == b"fake-test-video"


def test_manuscript_can_be_read_from_vault_without_changing_source(archive_case):
    project, vault, root = archive_case
    source = vault / "原稿.md"
    original = "---\ntype: article\nstatus: published\n---\n自己的原始表达。\n"
    source.write_text(original, encoding="utf-8")
    project["manuscripts"][1].pop("content")
    project["manuscripts"][1]["source_path"] = str(source)
    result = apply_archive(project, vault, root)
    assert "自己的原始表达" in result["content"]
    assert "status: published" not in result["content"]
    assert source.read_text(encoding="utf-8") == original


def test_duplicate_numbers_and_credentials_fail_closed(archive_case):
    project, vault, root = archive_case
    project["settings"]["archive"] = {"project_id": 1}
    with pytest.raises(ArchiveConflict, match="编号已存在"):
        preview_archive(project, vault, root)
    project["settings"].pop("archive")
    project["manuscripts"][1]["content"] = "api_key=should-not-go-in-vault"
    with pytest.raises(ArchiveError, match="凭证"):
        apply_archive(project, vault, root)
    assert not list((vault / "3-输出").glob("草稿/*"))


def test_deleted_owned_archive_is_not_silently_recreated(archive_case):
    project, vault, root = archive_case
    result = apply_archive(project, vault, root)
    Path(result["plan"]["note_path"]).unlink()
    with pytest.raises(ArchiveConflict, match="移走或删除"):
        apply_archive(project, vault, root)


def test_flat_frontend_settings_map_to_existing_note_format(archive_case):
    project,vault,root=archive_case
    project["settings"]={"archive_folder":"3-输出/草稿/视频", "publish_platform":"weixin-channels", "publish_title":"前端标题", "description":"前端简介", "tags":["AI","工作流"]}
    result=preview_archive(project,vault,root)
    assert Path(result["plan"]["note_path"]).parent==vault/"3-输出/草稿/视频"
    assert result["plan"]["platforms"]==["视频号"]
    assert "前端标题" in result["content"] and "前端简介" in result["content"]
    project["settings"]["publication_copy"]={"title":"嵌套配置优先"}
    assert "前端标题" not in preview_archive(project,vault,root)["content"]
