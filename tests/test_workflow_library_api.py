import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from easel.content_workflow import ContentWorkflowService, node_of
from easel.content_workflow_api import router
from easel.workflow_model import WorkflowModel


def test_actual_skill_learning_roundtrip_diffs_before_writing(tmp_path, monkeypatch):
    monkeypatch.setenv('EASEL_OPENCLAW_STATE_DIR', str(tmp_path / 'empty-profile'))
    vault = tmp_path / 'vault'; vault.mkdir()
    root = tmp_path / 'project'
    skill = root / 'skills' / 'openclaw' / 'text-polisher' / 'SKILL.md'
    skill.parent.mkdir(parents=True)
    before = '---\nname: text-polisher\ndescription: 去 AI 感改写\nlayer: produce\n---\n\n# 原规则\n\n保留事实。\n'
    after = before + '\n## 检查\n\n逐句检查填充短语，修改后核对原文事实。\n'
    skill.write_text(before, encoding='utf-8', newline='\n')
    service = ContentWorkflowService(root, vault=vault)
    p = service.create({'title': 'Skill沉淀'}); pid = p['id']
    node_of(p, 'script')['chat'] = {'status': 'idle', 'messages': [{'skills_used': [
        {'name': 'text-polisher', 'path': 'SKILL.md', 'sha256': 'previous'}]}]}
    service.save(p)
    calls = []
    async def generate(self, prompt, **kwargs):
        calls.append(json.loads(prompt))
        return after
    monkeypatch.setattr(WorkflowModel, 'generate', generate)
    app = FastAPI(); app.include_router(router(service))
    base = f'/api/content-workflows/{pid}/nodes/script'
    with TestClient(app) as client:
        metadata = client.get(base + '/skills').json()
        assert any(row['name'] == 'text-polisher' for row in metadata['library'])
        assert metadata['used_skills'][0]['name'] == 'text-polisher'
        document = client.get(base + '/skills/library', params={'name': 'text-polisher'}).json()
        response = client.post(base + '/learn/preview', json={'scope': 'library', 'skill_name': 'text-polisher',
            'relative_path': 'SKILL.md', 'instruction': '增加逐句检查和事实核对', 'expected_version': document['version']})
        assert response.status_code == 200, response.text
        proposal = response.json()
        assert proposal['scope'] == 'library' and proposal['before'] == before and proposal['after'] == after
        assert '逐句检查' in proposal['diff']
        assert skill.read_text(encoding='utf-8') == before, 'Preview must not edit the original Skill'
        applied = client.post(base + '/learn/apply', json={'scope': 'library', 'proposal_id': proposal['proposal_id']})
        assert applied.status_code == 200, applied.text
        assert skill.read_text(encoding='utf-8') == after
        reloaded = client.get(base + '/skills/library', params={'name': 'text-polisher'}).json()
        assert reloaded['content'] == after
        assert reloaded['version'] != document['version']
        stale = client.post(base + '/learn/preview', json={'scope': 'library', 'skill_name': 'text-polisher',
            'relative_path': 'SKILL.md', 'instruction': '更多核对', 'expected_version': document['version']})
        assert stale.status_code == 409
        assert len(calls) == 1, 'Stale previews must not make a paid model request'
