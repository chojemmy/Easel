"""Regression coverage for prose/action separation and real Skill selection."""
import asyncio
import json

import pytest

from easel import workflow_chat as chat
from easel.content_workflow import ContentWorkflowService, node_of


def envelope(reply, **action):
    return reply + '\n<easel_action>' + json.dumps(action, ensure_ascii=False) + '</easel_action>'


@pytest.fixture
def service(tmp_path):
    vault = tmp_path / 'vault'
    vault.mkdir()
    return ContentWorkflowService(tmp_path / 'project', vault=vault)


async def exchange(service, project, message='请使用技能库把原稿改自然'):
    await service.chat(project['id'], 'script', {'message': message,
        'content_version': project['content_version'], 'client_message_id': 'tools-case'})
    await service.tasks[project['id']]
    return service.get(project['id'])


def test_long_prose_quotes_code_and_newlines_do_not_need_json_escaping():
    body = '# 口播稿\n他说"我有自己的判断"。\n```text\nC:\\work\\clip\n```\n' * 200
    result = chat.parse_reply(envelope(body, action='draft', manuscript_title='新稿'))
    assert result['reply'] == body.strip()
    assert result['action'] == 'draft'


def test_action_tag_and_payload_never_flash_during_streaming():
    body = '可以先从这个现场故事讲起。'
    wire = envelope(body, action='update', updates={'title': 'hidden'})
    for end in range(1, len(wire) + 1):
        visible = chat.public_reply(wire[:end])
        assert '<easel' not in visible and 'hidden' not in visible
        assert visible.strip() == body[:len(visible.strip())]
    assert chat.public_reply(wire) == body


def test_partial_unicode_surrogate_is_not_a_callback_error():
    assert chat.partial_reply('{"reply":"正文\\uD83D\\uZZZZ') == '正文'


def test_tool_only_action_is_valid_but_empty_manuscript_is_not():
    result = chat.parse_reply(envelope('', action='read_skill', skills=[{'name': 'text-polisher'}]))
    assert result['action'] == 'read_skill'
    with pytest.raises(ValueError, match='没有返回可用答复'):
        chat.parse_reply(envelope('', action='draft'))


def test_extra_action_metadata_never_becomes_a_project_mutation():
    result = chat.parse_reply(envelope('先读取技能。', action='read_skill',
        skills=[{'name': 'text-polisher'}], offset=0, approved=True, status='completed'))
    assert result['skills'][0]['offset'] == 0
    assert 'approved' not in result and 'status' not in result
    with pytest.raises(ValueError, match='超出当前节点'):
        chat.parse_reply(envelope('越权', action='publish_direct', approved=True))


def test_bad_format_repair_keeps_completed_prose_without_overwriting_drafts(service, monkeypatch):
    body = 'Easel 的文本润色技能可以处理自然口吻；这一轮先解释。'
    calls = []
    async def generate(self, prompt, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            kwargs['on_text'](body)
            return body  # The model forgot the action tag entirely.
        assert kwargs['task'] == 'short_json'
        assert body in prompt
        return '{"action":"chat"}'
    monkeypatch.setattr(chat.WorkflowModel, 'generate', generate)
    p = service.create({'title': '格式恢复', 'manuscripts': [{'id': 'old', 'content': '保留原稿'}]})
    final = asyncio.run(exchange(service, p))
    state = node_of(final, 'script')['chat']
    assert state['status'] == 'idle'
    assert state['messages'][-1]['content'] == body
    assert final['manuscripts'] == p['manuscripts']
    assert len(calls) == 2
    assert any('修复操作格式' in a['text'] for a in node_of(final, 'script')['activity'])


def test_selected_skill_and_reference_are_read_then_used_in_draft(service, monkeypatch):
    reads, prompts = [], []
    class Catalog:
        def __init__(self, *args): pass
        def list_skills(self, **kwargs):
            return [{'id': 'text-polisher', 'name': 'text-polisher', 'description': '去AI感'}]
        def read_skill(self, name, path, **kwargs):
            reads.append((name, path))
            return {'name': name, 'path': path, 'sha256': 'audited-source-hash',
                'content': '砍填充短语；长短句交替；不编造事实。',
                'references': ['references/zh-ai-markers.md'] if path == 'SKILL.md' else []}
    monkeypatch.setattr(chat, 'WorkflowSkillCatalog', Catalog)
    async def generate(self, prompt, **kwargs):
        context = json.loads(prompt); prompts.append(context)
        if len(prompts) == 1:
            assert context['available_skills'][0]['name'] == 'text-polisher'
            return envelope('我先读取原有润色技能。', action='read_skill', skills=[{'name': 'text-polisher'}])
        if len(prompts) == 2:
            assert context['tool_results'][0]['result'][0]['sha256'] == 'audited-source-hash'
            return envelope('继续读取中文改写清单。', action='read_skill',
                skills=[{'name': 'text-polisher', 'path': 'references/zh-ai-markers.md'}])
        assert len(context['tool_results']) == 2
        return envelope('昨天我把稿子交给AI，读完又改了半天。', action='draft')
    monkeypatch.setattr(chat.WorkflowModel, 'generate', generate)
    p = service.create({'title': '润色', 'manuscripts': [{'id': 'old', 'content': '原稿'}]})
    final = asyncio.run(exchange(service, p))
    assert reads == [('text-polisher', 'SKILL.md'), ('text-polisher', 'references/zh-ai-markers.md')]
    assert final['manuscripts'][0]['content'] == '原稿'
    assert final['manuscripts'][-1]['content'].startswith('昨天我')
    state = node_of(final, 'script')['chat']
    assert state['status'] == 'idle'
    assert len(state['messages'][-1]['skills_used']) == 2
    events = node_of(final, 'script')['activity']
    assert sum(a['kind'] == 'tool' and 'text-polisher' in a['text'] and a.get('run_id', '').startswith('chat-') for a in events) == 2


def test_tools_cannot_apply_updates_in_same_round(service, monkeypatch):
    async def generate(self, *args, **kwargs):
        return envelope('读取技能', action='read_skill', skills=[{'name': 'text-polisher'}],
            updates={'title': '不应写入'})
    monkeypatch.setattr(chat.WorkflowModel, 'generate', generate)
    p = service.create({'title': '不能旁路'})
    final = asyncio.run(exchange(service, p))
    assert final['title'] == p['title']
    assert node_of(final, 'script')['chat']['status'] == 'failed'


def test_transient_retry_status_is_observable(service, monkeypatch):
    async def generate(self, *args, **kwargs):
        kwargs['on_status']('模型服务暂时繁忙（HTTP 529），2 秒后重试。')
        return envelope('现在可以继续。', action='chat')
    monkeypatch.setattr(chat.WorkflowModel, 'generate', generate)
    p = service.create({'title': '重试可见'})
    final = asyncio.run(exchange(service, p))
    assert node_of(final, 'script')['chat']['status'] == 'idle'
    assert any('HTTP 529' in item['text'] for item in node_of(final, 'script')['activity'])
