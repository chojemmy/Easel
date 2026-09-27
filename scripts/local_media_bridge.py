"""Local MiniMax protocol adapter and private Chinese embedding service.

Keys are supplied by the DPAPI launcher. No credentials are saved in this file.
Only fixed upstream hosts are supported; this is not a general forwarding proxy.
"""
from __future__ import annotations

import base64
import hmac
import os
import re
import threading
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI(docs_url=None, redoc_url=None)
MINIMAX = 'https://api.minimaxi.com'
MODEL = 'BAAI/bge-small-zh-v1.5'
_embedding = None
_embedding_lock = threading.Lock()


@app.middleware('http')
async def authenticate(request: Request, call_next):
    if request.url.path != '/healthz':
        expected = os.environ.get('EASEL_MEDIA_TOKEN', '')
        received = request.headers.get('authorization', '').removeprefix('Bearer ')
        if not expected or not hmac.compare_digest(received, expected):
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    return await call_next(request)


@app.get('/healthz')
def health():
    return {'ok': True, 'embedding': MODEL, 'media': 'MiniMax'}


def upstream(path: str, payload: dict | None = None, params: dict | None = None):
    key = os.environ['MINIMAX_API_KEY']
    try:
        with httpx.Client(timeout=180, trust_env=True) as client:
            r = client.request('POST' if payload is not None else 'GET', MINIMAX + path,
                               headers={'Authorization': 'Bearer ' + key}, json=payload, params=params)
        x = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, 'MiniMax request failed: ' + type(exc).__name__) from exc
    status = x.get('base_resp', {})
    if r.status_code >= 400 or status.get('status_code', 0) != 0:
        message = str(status.get('status_msg') or x.get('error') or 'Upstream request failed')
        raise HTTPException(502, message.replace(key, '[redacted]')[:400])
    return x


def ratio(size: str) -> str:
    if not size or size == 'auto':
        return '1:1'
    choices = ('1:1', '16:9', '4:3', '3:2', '2:3', '3:4', '9:16', '21:9')
    if size in choices:
        return size
    if re.fullmatch(r'\d+x\d+', size):
        w, h = map(int, size.split('x'))
        if h > 0:
            return min(choices, key=lambda v: abs(int(v.split(':')[0]) / int(v.split(':')[1]) - w / h))
    raise HTTPException(400, 'Unsupported image size')


def generate_image(data: dict):
    if data.get('model', 'image-01') != 'image-01':
        raise HTTPException(400, 'This MiniMax adapter supports image-01')
    prompt = str(data.get('prompt', '')).strip()
    if not prompt:
        raise HTTPException(400, 'prompt is required')
    payload = {'model': 'image-01', 'prompt': prompt, 'n': int(data.get('n', 1)),
               'aspect_ratio': ratio(str(data.get('size', '1:1'))), 'response_format': 'base64'}
    if not 1 <= payload['n'] <= 9:
        raise HTTPException(400, 'n must be between 1 and 9')
    x = upstream('/v1/image_generation', payload)
    result = [{'b64_json': value} for value in x.get('data', {}).get('image_base64', [])]
    result += [{'url': value} for value in x.get('data', {}).get('image_urls', [])]
    if not result:
        raise HTTPException(502, 'MiniMax did not return an image')
    return {'data': result}


@app.post('/v1/images/generations')
def image_generation(data: dict):
    return generate_image(data)


@app.post('/v1/images/edits')
@app.post('/v1/images/variations')
def image_edit():
    # MiniMax character-reference generation is not a general image-edit API.
    raise HTTPException(422, 'MiniMax image-01 supports text-to-image here. General image editing needs a compatible image-edit provider.')


@app.post('/v1/videos')
def video_generation(data: dict):
    duration = int(data.get('seconds') or 6)
    if duration not in (6, 10):
        raise HTTPException(400, 'MiniMax Hailuo 2.3 supports 6 or 10 seconds; choose --duration 6 or 10')
    if data.get('model') not in (None, 'MiniMax-Hailuo-2.3'):
        raise HTTPException(400, 'This Token Plan adapter uses MiniMax-Hailuo-2.3')
    payload = {'model': 'MiniMax-Hailuo-2.3', 'prompt': str(data.get('prompt', '')),
               'duration': duration, 'resolution': '768P'}
    if data.get('image'):
        payload['first_frame_image'] = data['image']
    elif data.get('size') not in (None, '', '16:9', 'auto'):
        raise HTTPException(400, 'MiniMax text-to-video uses landscape output; use a portrait first frame for portrait video')
    x = upstream('/v1/video_generation', payload)
    return {'id': x['task_id'], 'status': 'queued'}


@app.get('/v1/videos/{task_id}')
def video_status(task_id: str):
    if not task_id.isdigit():
        raise HTTPException(400, 'Invalid MiniMax task id')
    x = upstream('/v1/query/video_generation', params={'task_id': task_id})
    status = str(x.get('status', '')).lower()
    if status == 'success':
        f = upstream('/v1/files/retrieve', params={'file_id': x['file_id']})
        return {'id': task_id, 'status': 'completed', 'url': f['file']['download_url']}
    if status == 'fail':
        return {'id': task_id, 'status': 'failed', 'error': 'MiniMax video generation failed'}
    return {'id': task_id, 'status': 'in_progress'}


@app.post('/v1/embeddings')
def embeddings(data: dict):
    global _embedding
    if data.get('model', MODEL) != MODEL:
        raise HTTPException(400, 'Unknown embedding model')
    texts = data.get('input')
    if isinstance(texts, str):
        texts = [texts]
    if not isinstance(texts, list) or not texts or not all(isinstance(t, str) for t in texts):
        raise HTTPException(400, 'input must contain strings')
    if len(texts) > 128:
        raise HTTPException(400, 'Maximum batch size is 128')
    with _embedding_lock:
        if _embedding is None:
            from fastembed import TextEmbedding
            _embedding = TextEmbedding(MODEL, cache_dir=str(Path(os.environ['LOCALAPPDATA']) / 'Easel/models'),
                                       threads=4, local_files_only=True)
        vectors = list(_embedding.query_embed(texts) if data.get('input_type') == 'query' else _embedding.embed(texts))
    encoded = data.get('encoding_format') == 'base64'
    return {'object': 'list', 'model': MODEL,
            'data': [{'object': 'embedding', 'index': i,
                      'embedding': base64.b64encode(v.astype('<f4').tobytes()).decode() if encoded else v.tolist()}
                     for i, v in enumerate(vectors)],
            'usage': {'prompt_tokens': 0, 'total_tokens': 0}}


@app.get('/v1/models')
def models():
    return {'object': 'list', 'data': [{'id': name, 'object': 'model'}
                                     for name in ('image-01', 'MiniMax-Hailuo-2.3', MODEL)]}
