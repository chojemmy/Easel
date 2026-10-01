"""Original Skill access stays read-only and inside explicitly registered roots."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from easel.workflow_skill_catalog import MAX_FILE_BYTES, WorkflowSkillCatalog, WorkflowSkillCatalogError


@pytest.fixture
def catalog(tmp_path):
    return WorkflowSkillCatalog(tmp_path / "repo", config_path=tmp_path / "state" / "openclaw.json",
                               environment={"EASEL_OPENCLAW_STATE_DIR": str(tmp_path / "state")})


def install(catalog, name="text-polisher", body="## 方法\n使用主动语态。", *, root=None, frontmatter=None):
    directory = (root or catalog.project_root / "skills" / "openclaw") / name
    directory.mkdir(parents=True, exist_ok=True)
    header = frontmatter or f"name: {name}\ndescription: >-\n  文本润色。\n  去 AI 感，保留真实细节。\nlayer: produce"
    (directory / "SKILL.md").write_text(f"---\n{header}\n---\n\n{body}\n", encoding="utf-8")
    return directory


def configure(catalog, values):
    catalog.config_path.parent.mkdir(parents=True, exist_ok=True)
    catalog.config_path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")


def link(target, dest):
    try:
        dest.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as error:
        pytest.skip(f"This host cannot create symbolic links: {type(error).__name__}")


def test_repo_registry_matches_web_names_and_short_alias(catalog):
    path = install(catalog, "skill-hook-generator")
    before = {p: p.read_bytes() for p in path.rglob("*") if p.is_file()}
    items = catalog.list_skills("script")
    assert [item["name"] for item in items] == ["skill-hook-generator"]
    assert items[0]["description"] == "文本润色。 去 AI 感，保留真实细节。"
    assert items[0]["capability"] == "read_only_guidance"
    assert all(not key.startswith("_") for key in items[0])
    assert "主动语态" in catalog.read_skill("hook-generator", node="script")["content"]
    assert before == {p: p.read_bytes() for p in path.rglob("*") if p.is_file()}


def test_missing_registry_is_empty_without_creating_anything(catalog):
    assert catalog.list_skills() == []
    assert not catalog.project_root.exists()
    with pytest.raises(WorkflowSkillCatalogError, match="启用"):
        catalog.read_skill("text-polisher")


def test_method_and_tool_capabilities_are_truthful(catalog):
    install(catalog)
    runtime = install(catalog, "video-production", "运行 `python scripts/build.py` 后检查产物。")
    (runtime / "scripts").mkdir()
    listed = {item["id"]: item for item in catalog.list_skills()}
    assert not listed["text-polisher"]["requires_tools"]
    assert listed["video-production"]["requires_tools"]
    assert "不会执行" in listed["video-production"]["execution_note"]
    assert {item["name"] for item in catalog.list_skills("script")} == {"text-polisher"}
    with pytest.raises(WorkflowSkillCatalogError, match="当前节点"):
        catalog.read_skill("video-production", node="script")


def test_references_and_linked_references_are_read_from_live_source(catalog):
    directory = install(catalog, body="先读 `references/phrases-to-remove.md`，再读 [结构](references/structures.md)。")
    refs = directory / "references"
    refs.mkdir()
    (refs / "phrases-to-remove.md").write_text("# 清单\n不要套话。\n[更多](../guide.md)", encoding="utf-8")
    (refs / "structures.md").write_text("不要公式化结构。", encoding="utf-8")
    (directory / "guide.md").write_text("原文指南。", encoding="utf-8")
    root = catalog.read_skill("text-polisher", node="script")
    assert root["references"] == ["references/phrases-to-remove.md", "references/structures.md"]
    first = catalog.read_skill("text-polisher", root["references"][0], node="script")
    assert first["references"] == ["guide.md"]
    assert catalog.read_skill("text-polisher", "guide.md")["content"] == "原文指南。"
    (refs / "phrases-to-remove.md").write_text("更新后的权威清单。", encoding="utf-8")
    second = catalog.read_skill("text-polisher", root["references"][0])
    assert second["content"] == "更新后的权威清单。" and second["sha256"] != first["sha256"]


@pytest.mark.parametrize("disabled", ["text-polisher", "custom-name", "custom-config-key"])
def test_disabled_skills_cannot_be_listed_or_read_by_an_alias(catalog, disabled):
    install(catalog, frontmatter='name: custom-name\ndescription: 文本润色\nmetadata: {"openclaw":{"skillKey":"custom-config-key"}}')
    configure(catalog, {"skills": {"entries": {disabled: {"enabled": False}}}, "apiKey": "not-exposed"})
    assert catalog.list_skills() == []
    with pytest.raises(WorkflowSkillCatalogError, match="启用"):
        catalog.read_skill("text-polisher")


def test_configuration_toggle_takes_effect_without_rebuilding_catalog(catalog):
    install(catalog, "skill-hook-generator")
    assert catalog.list_skills()
    configure(catalog, {"skills": {"entries": {"hook-generator": {"enabled": False}}}})
    assert catalog.list_skills() == []
    configure(catalog, {"skills": {"entries": {"skill-hook-generator": {"enabled": True}}}})
    assert catalog.read_skill("hook-generator")


def test_configured_workspace_extras_and_explicit_roots_are_discovered(catalog, tmp_path):
    workspace, extra, explicit = tmp_path / "workspace", tmp_path / "extra", tmp_path / "explicit"
    install(catalog, "video-script", root=workspace / "skills")
    install(catalog, "text-polisher", root=extra)
    install(catalog, "copywriting", root=explicit)
    configure(catalog, {"agents": {"defaults": {"workspace": str(workspace)}},
                        "skills": {"load": {"extraDirs": [str(extra)]}}, "token": "private-config-value"})
    custom = WorkflowSkillCatalog(catalog.project_root, config_path=catalog.config_path,
                                  extra_roots=[explicit], environment=catalog.environment)
    names = {item["name"] for item in custom.list_skills("script")}
    assert names == {"video-script", "text-polisher", "copywriting"}
    assert "private-config-value" not in json.dumps(custom.list_skills())


def test_repo_wins_duplicate_names_and_main_workspace_wins_defaults(catalog, tmp_path):
    repo = install(catalog, body="权威仓库。")
    main, other = tmp_path / "main", tmp_path / "other"
    install(catalog, body="重复副本。", root=main / "skills")
    install(catalog, "video-script", root=main / "skills")
    install(catalog, "copywriting", root=other / "skills")
    configure(catalog, {"agents": {"defaults": {"workspace": str(other)}, "list": [{"id": "main", "workspace": str(main)}]}})
    assert "权威仓库" in catalog.read_skill("text-polisher")["content"]
    assert {item["name"] for item in catalog.list_skills()} == {repo.name, "video-script"}


def test_invalid_config_fails_closed_without_leaking_its_content(catalog):
    install(catalog)
    catalog.config_path.parent.mkdir(parents=True)
    catalog.config_path.write_text('{"apiKey":"secret-sensitive" invalid}', encoding="utf-8")
    with pytest.raises(WorkflowSkillCatalogError) as caught:
        catalog.list_skills()
    assert "secret-sensitive" not in str(caught.value)


@pytest.mark.parametrize("path", ["../secret.md", "references/../../secret.md", "/secret.md", "C:\\secret.md", "C:secret.md",
                                "\\\\host\\share\\secret.md", ".env", ".git/config", "_private/note.md", "credentials.json",
                                "references/secret.txt", "config.json", "SKILL.md:stream", "SKILL.md.", "scripts/build.py", "key.pem"])
def test_unsafe_paths_are_rejected(catalog, path):
    install(catalog)
    with pytest.raises(WorkflowSkillCatalogError):
        catalog.read_skill("text-polisher", path)


@pytest.mark.parametrize("name", ["../text-polisher", "a/b", "/tmp", "C:\\private", ".hidden", "_private"])
def test_skill_identifier_cannot_be_a_path(catalog, name):
    with pytest.raises(WorkflowSkillCatalogError):
        catalog.read_skill(name)


def test_symlink_escape_from_reference_or_skill_directory_is_rejected(catalog, tmp_path):
    directory = install(catalog)
    external = tmp_path / "external.md"
    external.write_text("PRIVATE-OUTSIDE", encoding="utf-8")
    link(external, directory / "guide.md")
    with pytest.raises(WorkflowSkillCatalogError):
        catalog.read_skill("text-polisher", "guide.md")
    external_dir = tmp_path / "other-skill"
    external_dir.mkdir()
    (external_dir / "SKILL.md").write_text("PRIVATE-OUTSIDE", encoding="utf-8")
    link(external_dir, directory.parent / "escape")
    assert "escape" not in {item["name"] for item in catalog.list_skills()}


def test_internal_symlink_cannot_disguise_a_private_file(catalog):
    directory = install(catalog)
    secret = directory / ".env"
    secret.write_text("SECRET-VALUE", encoding="utf-8")
    link(secret, directory / "guide.md")
    with pytest.raises(WorkflowSkillCatalogError):
        catalog.read_skill("text-polisher", "guide.md")


def test_references_do_not_advertise_external_or_private_files(catalog, tmp_path):
    directory = install(catalog, body="[web](https://example.com/secret.md) `../secret.md` `.env` `guide.md`")
    (directory / "guide.md").write_text("safe", encoding="utf-8")
    (directory / ".env").write_text("private", encoding="utf-8")
    (directory.parent / "secret.md").write_text("private", encoding="utf-8")
    assert catalog.read_skill("text-polisher")["references"] == ["guide.md"]


def test_bounded_reads_support_continuation_without_data_loss(catalog):
    directory = install(catalog)
    text = "中文内容\n" * 9000
    (directory / "guide.md").write_bytes(text.encode("utf-8"))
    first = catalog.read_skill("text-polisher", "guide.md", max_chars=20000)
    assert first["truncated"] and first["next_offset"] == 20000
    rest, offset = first["content"], first["next_offset"]
    while offset is not None:
        current = catalog.read_skill("text-polisher", "guide.md", offset=offset)
        assert current["sha256"] == first["sha256"]
        rest += current["content"]; offset = current["next_offset"]
    assert rest == text


def test_oversized_non_text_and_bad_read_ranges_fail(catalog):
    directory = install(catalog)
    (directory / "large.md").write_bytes(b"a" * (MAX_FILE_BYTES + 1))
    (directory / "binary.md").write_bytes(b"text\0binary")
    for name in ["large.md", "binary.md"]:
        with pytest.raises(WorkflowSkillCatalogError):
            catalog.read_skill("text-polisher", name)
    for kwargs in [{"offset": -1}, {"offset": 900000}, {"max_chars": 24001}, {"max_chars": True}]:
        with pytest.raises(WorkflowSkillCatalogError):
            catalog.read_skill("text-polisher", **kwargs)


def test_credential_literals_are_redacted_from_metadata_and_body(catalog):
    live_key = "sk-" + "A1b2" * 8
    directory = install(catalog, body=f'api_key = "{live_key}"\npassword: "private-pass"\n使用 $API_KEY 环境变量。',
                        frontmatter=f'name: text-polisher\ndescription: 测试 {live_key}')
    (directory / "guide.md").write_text('token: "private-token"\nAuthorization: Bearer private-bearer\nCookie: sid=private-session; second=private-cookie\n$TOKEN', encoding="utf-8")
    output = json.dumps([catalog.list_skills(), catalog.read_skill("text-polisher"), catalog.read_skill("text-polisher", "guide.md")])
    for secret in [live_key, "private-pass", "private-token", "private-bearer", "private-session", "private-cookie"]:
        assert secret not in output
    assert "[REDACTED]" in output


def test_limits_and_unknown_nodes_are_validated(catalog):
    install(catalog)
    for value in [0, 257, True]:
        with pytest.raises(WorkflowSkillCatalogError):
            catalog.list_skills(limit=value)
    with pytest.raises(WorkflowSkillCatalogError):
        catalog.list_skills("unknown")


def test_resolve_source_rechecks_enabled_state_and_returns_only_safe_source(catalog):
    directory = install(catalog)
    assert catalog.resolve_source("text-polisher", node="script") == (directory / "SKILL.md").resolve()
    with pytest.raises(WorkflowSkillCatalogError):
        catalog.resolve_source("text-polisher", "../private.md", node="script")
    configure(catalog, {"skills": {"entries": {"text-polisher": {"enabled": False}}}})
    with pytest.raises(WorkflowSkillCatalogError):
        catalog.resolve_source("text-polisher", node="script")


def test_native_catalog_provides_current_verified_source_paths_only_when_requested(catalog):
    directory = install(catalog)
    assert "source_path" not in catalog.list_skills("script")[0]
    native = catalog.list_skills("script", include_source=True)
    assert native[0]["source_path"] == str((directory / "SKILL.md").resolve())
    configure(catalog, {"skills": {"entries": {"text-polisher": {"enabled": False}}}})
    assert catalog.list_skills("script", include_source=True) == []
