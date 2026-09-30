from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from easel.workflow_library_learning import WorkflowLibraryLearning, WorkflowLibraryConflict
from easel.workflow_skill_catalog import WorkflowSkillCatalog


@pytest.fixture
def library(tmp_path):
    root=tmp_path/"project"
    skill=root/"skills/openclaw/text-polisher"
    (skill/"references").mkdir(parents=True)
    original="---\nname: text-polisher\ndescription: 中文润色\n---\n\n# 写作规则\n\n保留用户观点。\n\n读取 `references/zh-ai-markers.md`。\n"
    (skill/"SKILL.md").write_bytes(original.encode("utf-8"))
    (skill/"references/zh-ai-markers.md").write_bytes("# 中文指南\n\n避免空话。\n".encode("utf-8"))
    config=tmp_path/"isolated-config.json"
    catalog=WorkflowSkillCatalog(root,config_path=config,environment={"EASEL_OPENCLAW_STATE_DIR":str(tmp_path/"empty-state")})
    return WorkflowLibraryLearning(root,catalog),skill,config


def test_propose_is_reviewable_and_does_not_write_real_skill_or_copy_it(library):
    store,skill,_=library
    original=(skill/"SKILL.md").read_bytes()
    proposal=store.propose("script","text-polisher","SKILL.md","开头直接说明具体观点。",
                           context={"run_id":"run_123","manuscripts":[{"content":"私人稿件不应沉淀"}],"token":"private-context-value"})
    assert proposal["scope"]=="library" and proposal["id"]==proposal["proposal_id"]
    assert proposal["target_path"]==str((skill/"SKILL.md").resolve())
    assert "+开头直接说明具体观点。" in proposal["diff"]
    assert proposal["context"]=={"run_id":"run_123"}
    assert (skill/"SKILL.md").read_bytes()==original
    assert not list(skill.glob("*.lock"))
    assert not list(store.root.rglob("easel-node-*"))
    persisted="".join(p.read_text(encoding="utf-8") for p in store.proposal_root.glob("*.json"))
    assert "私人稿件" not in persisted and "private-context-value" not in persisted
    restarted=WorkflowLibraryLearning(store.root,store.catalog)
    assert restarted.get_proposal("script",proposal["id"])==proposal


@pytest.mark.parametrize("relative",["SKILL.md","references/zh-ai-markers.md"])
def test_approved_change_updates_original_future_reads_and_history_idempotently(library,relative):
    store,skill,_=library
    before=(skill/relative).read_text(encoding="utf-8")
    after=before.replace("保留用户观点。","保留用户观点，引用只来自材料。") if relative=="SKILL.md" else "# 中文指南\n\n用具体动作替换空话。\n"
    proposal=store.propose("script","text-polisher",relative,"使用具体动作和可核查事实。",after_content=after)
    result=store.apply("script",proposal["id"])
    assert result["content"]==after and result["scope"]=="library"
    assert (skill/relative).read_text(encoding="utf-8")==after
    assert store.catalog.read_skill("text-polisher",relative,node="script")["content"]==after
    assert store.apply("script",proposal["id"])==result
    history=store.history("script","text-polisher",relative)
    assert {item["content"] for item in history}=={before,after}
    assert store.get_proposal("script",proposal["id"])["status"]=="applied"


def test_manual_change_and_stale_proposal_cannot_overwrite_source(library):
    store,skill,_=library
    first=store.propose("script","text-polisher","SKILL.md","一次只解释一个观点。")
    stale=store.propose("script","text-polisher","SKILL.md","全部段落加小标题。")
    store.apply("script",first["id"])
    with pytest.raises(WorkflowLibraryConflict):
        store.apply("script",stale["id"])
    (skill/"SKILL.md").write_text(first["after"]+"\n用户手动修订。\n",encoding="utf-8")
    with pytest.raises(WorkflowLibraryConflict):
        store.apply("script",first["id"])
    assert "用户手动修订" in (skill/"SKILL.md").read_text(encoding="utf-8")


def test_crlf_only_external_edit_invalidates_original_bytes_version(library):
    store,skill,_=library
    proposal=store.propose("script","text-polisher","SKILL.md","短句表达。")
    source=skill/"SKILL.md"
    source.write_bytes(source.read_bytes().replace(b"\n",b"\r\n"))
    with pytest.raises(WorkflowLibraryConflict):
        store.apply("script",proposal["id"])


def test_disabled_skill_is_rechecked_at_approval(library):
    store,skill,config=library
    proposal=store.propose("script","text-polisher","SKILL.md","结论写具体。")
    config.write_text(json.dumps({"skills":{"entries":{"text-polisher":{"enabled":False}}}}),encoding="utf-8")
    with pytest.raises(ValueError,match="未找到"):
        store.apply("script",proposal["id"])
    assert (skill/"SKILL.md").read_text(encoding="utf-8")==proposal["before"]


def test_registered_alias_moving_to_another_source_requires_new_review(library):
    store,skill,_=library
    proposal=store.propose("script","text-polisher","SKILL.md","开头只写一个结论。")
    relocated=skill.with_name("renamed-polisher")
    skill.rename(relocated)
    assert store.catalog.resolve_source("text-polisher",node="script")==relocated/"SKILL.md"
    with pytest.raises(WorkflowLibraryConflict,match="路径已改变"):
        store.apply("script",proposal["id"])
    assert (relocated/"SKILL.md").read_bytes().decode("utf-8")==proposal["before"]


@pytest.mark.parametrize("field,value",[("target_path","C:/unrelated/secret.md"),("relative_path","../other.md"),
                                      ("skill_name","other-skill"),("after","unreviewed replacement")])
def test_tampered_proposal_cannot_change_target_or_content(library,field,value):
    store,skill,_=library
    proposal=store.propose("script","text-polisher","SKILL.md","用简洁具体的表达。")
    proposal[field]=value
    (store.proposal_root/f"{proposal['id']}.json").write_text(json.dumps(proposal),encoding="utf-8")
    with pytest.raises(WorkflowLibraryConflict):
        store.apply("script",proposal["id"])
    assert "用简洁具体的表达" not in (skill/"SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("relative",["../other.md","references/../../other.md","/absolute.md","references/config.json","scripts/run.py"])
def test_only_registered_markdown_sources_can_be_proposed(library,relative):
    store,_,_=library
    with pytest.raises(ValueError):
        store.propose("script","text-polisher",relative,"规则。")
    assert not store.proposal_root.exists()


@pytest.mark.parametrize("replacement",["", "# 改名的新 Skill\n", "---\nname: other-skill\ndescription: 中文润色\n---\n正文。"])
def test_skill_frontmatter_and_name_must_survive_rewrite(library,replacement):
    store,_,_=library
    with pytest.raises(ValueError):
        store.propose("script","text-polisher","SKILL.md","简洁表达。",after_content=replacement)
    assert not store.proposal_root.exists()


@pytest.mark.parametrize("instruction",["api_key=sk-private-secret-value123456", "Bearer abcdef0123456789"])
def test_credentials_never_enter_proposals(library,instruction):
    store,_,_=library
    with pytest.raises(ValueError,match="凭证"):
        store.propose("script","text-polisher","SKILL.md",instruction)
    assert not store.proposal_root.exists()


def test_existing_source_with_credentials_is_not_snapshotted(library):
    store,skill,_=library
    source=skill/"SKILL.md"
    source.write_bytes(source.read_bytes()+b"\napi_key=sk-private-existing-value123456\n")
    with pytest.raises(ValueError,match="凭证"):
        store.propose("script","text-polisher","SKILL.md","摘要只保留方法。")
    assert not store.proposal_root.exists()


def test_known_environment_credential_in_rewrite_is_rejected(library,monkeypatch):
    store,skill,_=library
    monkeypatch.setenv("TEST_LIBRARY_API_KEY","a-test-only-credential-value")
    after=(skill/"SKILL.md").read_text(encoding="utf-8")+"\n未标注键名的值：a-test-only-credential-value\n"
    with pytest.raises(ValueError,match="凭证"):
        store.propose("script","text-polisher","SKILL.md","沉淀表达方式。",after_content=after)
    assert not store.proposal_root.exists()


def test_full_context_draft_cannot_be_embedded_in_shared_skill(library):
    store,skill,_=library
    draft="仅属于这个项目的未公开个人经历。"*30
    after=(skill/"SKILL.md").read_text(encoding="utf-8")+draft
    with pytest.raises(ValueError,match="完整稿件"):
        store.propose("script","text-polisher","SKILL.md","总结通用写法。",after_content=after,context={"manuscripts":[{"content":draft}]})
    assert not store.proposal_root.exists()


def test_different_projects_cannot_both_apply_from_same_shared_base(library,tmp_path):
    store,skill,_=library
    second=WorkflowLibraryLearning(tmp_path/"other-project",store.catalog)
    proposals=[store.propose("script","text-polisher","SKILL.md","第一项目意见。"),
               second.propose("script","text-polisher","SKILL.md","第二项目意见。")]
    def apply(pair):
        owner,proposal=pair
        try:
            return owner.apply("script",proposal["id"])
        except WorkflowLibraryConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(apply,zip([store,second],proposals)))
    assert sum(result is not None for result in results)==1
    live=(skill/"SKILL.md").read_text(encoding="utf-8")
    assert ("第一项目意见" in live) != ("第二项目意见" in live)


def test_wrong_node_and_proposal_id_do_not_apply(library):
    store,skill,_=library
    proposal=store.propose("script","text-polisher","SKILL.md","自然口语。")
    with pytest.raises(ValueError):
        store.apply("build",proposal["id"])
    with pytest.raises(ValueError):
        store.apply("script","../escape")
    assert (skill/"SKILL.md").read_text(encoding="utf-8")==proposal["before"]
