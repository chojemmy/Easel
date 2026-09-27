import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from easel.minimax_quota import QuotaService, Settings, eligible, normalize, router


def test_interval_default_validation_and_persistence(tmp_path):
    assert Settings().check_interval_minutes == 15
    for value in (0, 1441):
        with pytest.raises(ValueError):
            Settings(check_interval_minutes=value)
    service = QuotaService(tmp_path)
    service.save(Settings(check_interval_minutes=30))
    assert QuotaService(tmp_path).settings().check_interval_minutes == 30


def quota(now=1000, pct=80, weekly=50, left=1800):
    return normalize({'base_resp': {'status_code': 0}, 'model_remains': [{
        'model_name': 'general', 'end_time': (now + left) * 1000,
        'current_interval_remaining_percent': pct, 'current_weekly_remaining_percent': weekly,
    }]}, now)


def test_threshold_freshness_and_weekly_quota():
    settings = Settings(enabled=True)
    assert eligible(settings, quota(), 1000)
    assert not eligible(settings, quota(pct=15), 1000)
    assert not eligible(settings, quota(left=3601), 1000)
    assert not eligible(settings, quota(left=-1), 1000)
    assert not eligible(settings, quota(weekly=0), 1000)
    assert not eligible(settings, quota(), 1121)
    assert not eligible(Settings(), quota(), 1000)


def test_unknown_count_is_not_assumed_remaining():
    data = quota(pct=None)
    assert data['rows'][0]['remaining_percent'] is None
    assert not eligible(Settings(enabled=True), data, 1000)
    with pytest.raises(ValueError):
        normalize({'base_resp': {'status_code': 2153}})


def test_durable_single_claim_and_fifo(tmp_path):
    service = QuotaService(tmp_path)
    service.save(Settings(enabled=True))
    first = service.add('先完成这个任务')
    service.add('第二个任务')
    row = quota()['rows'][0]
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = list(pool.map(lambda _: service.claim(row), range(4)))
    job = next(j for j in jobs if j)
    assert sum(j is not None for j in jobs) == 1
    assert job['task_id'] == first
    service.finish(job, 'completed', 'done')
    restarted = QuotaService(tmp_path)
    assert restarted.claim(row) is None
    assert restarted.snapshot()['tasks'][0]['state'] == 'queued'


def test_empty_queue_opt_in_and_disable(tmp_path):
    service = QuotaService(tmp_path)
    row = quota()['rows'][0]
    service.save(Settings(enabled=True))
    assert service.claim(row) is None
    service.save(Settings(enabled=True, trending_enabled=True, trending_prompt='BIM 热点'))
    job = service.claim(row)
    assert 'BIM 热点' in job['prompt'] and job['task_id'] is None
    service.finish(job, 'completed', 'done')
    service.save(Settings(enabled=False))
    service.add('不得启动')
    assert service.claim({**row, 'reset_at': 9999}) is None


def test_tick_dispatches_and_blocks_duplicate(tmp_path):
    service = QuotaService(tmp_path)
    service.save(Settings(enabled=True))
    service.add('本地测试')
    service.cache = quota(time.time())
    seen = []

    async def execute(job, row):
        seen.append(job)
        service.finish(job, 'completed', '模拟 OpenClaw 成功')

    service.execute = execute
    asyncio.run(service.tick())
    asyncio.run(service.tick())
    assert len(seen) == 1


def test_secret_redaction(tmp_path, monkeypatch):
    monkeypatch.setenv('MINIMAX_API_KEY', 'test-secret-123456')
    service = QuotaService(tmp_path)
    service.save(Settings(enabled=True))
    service.add('test')
    job = service.claim(quota()['rows'][0])
    service.finish(job, 'failed', 'failure test-secret-123456')
    assert 'test-secret-123456' not in service.snapshot()['runs'][0]['result']


def test_routes_settings_queue_cancel_and_origin(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    service = QuotaService(tmp_path)
    app = FastAPI()
    app.include_router(router(service))
    with TestClient(app) as client:
        assert client.get('/api/minimax/automation').json()['settings']['check_interval_minutes'] == 15
        assert client.put('/api/minimax/automation', json={'check_interval_minutes': 0}).status_code == 422
        assert client.put('/api/minimax/automation', json={'check_interval_minutes': 30}).status_code == 200
        assert service.settings().check_interval_minutes == 30
        assert client.post('/api/minimax/tasks', json={'prompt': '   '}).status_code == 400
        assert client.post('/api/minimax/tasks', json={'prompt': 'test'},
                           headers={'Origin': 'https://unrelated.example'}).status_code == 403
        task = client.post('/api/minimax/tasks', json={'prompt': 'test'}).json()['id']
        assert client.delete('/api/minimax/tasks/' + task).status_code == 200
        assert service.snapshot()['tasks'][0]['state'] == 'cancelled'
