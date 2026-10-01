#!/usr/bin/env python3
"""faster-whisper 词级转录 → JSON（字幕 / 场景数据原料）。

用法:
  python transcribe.py --src video.mp4 --out transcript.json
  python transcribe.py --src video.mp4 --out t.json --model <模型目录> --device cuda

说明:
  - 口播建议 vad_filter=False：软语音被当静音切掉是常见事故源。
  - 模型本地路径或名称均可；可用 tools/fetch_whisper_model.py 拉取 large-v3。
  - HF_ENDPOINT 默认走国内镜像（hf-mirror.com），境外环境可删。
依赖: faster-whisper（见 deps/DEPS.md）
"""
import argparse
import json
import os
import time
from pathlib import Path

os.environ.setdefault('HF_ENDPOINT', 'https://hf-mirror.com')
os.environ.setdefault('HF_HUB_DISABLE_XET', '1')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', required=True, help='音/视频源')
    ap.add_argument('--out', required=True, help='输出 JSON')
    ap.add_argument('--model', default='large-v3', help='模型名或本地目录')
    ap.add_argument('--lang', default='zh')
    ap.add_argument('--device', default='cuda')
    ap.add_argument('--compute', default='float16')
    ap.add_argument('--reference-file', help='已选口播稿，只作为术语/识别上下文，不能代替录音')
    ap.add_argument('--local-files-only', action='store_true', help='只用已存在的本地模型，禁止下载')
    ap.add_argument('--progress-file', help='可选：本次任务的真实加载/转录进度 JSON')
    args = ap.parse_args()

    def atomic_json(path, value):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, path)

    def progress(phase, **details):
        if args.progress_file:
            atomic_json(args.progress_file, {'phase': phase, 'pid': os.getpid(), **details})

    from faster_whisper import WhisperModel

    t0 = time.time()
    print('loading model...', flush=True)
    progress('loading_model')
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute,
                         local_files_only=args.local_files_only)
    print(f'model loaded {time.time() - t0:.1f}s', flush=True)

    t1 = time.time()
    reference = Path(args.reference_file).read_text(encoding='utf-8-sig').strip() if args.reference_file else ''
    segments, info = model.transcribe(
        args.src, language=args.lang, word_timestamps=True, vad_filter=False, beam_size=5,
        initial_prompt=reference[:2000] or None)
    progress('transcribing', audio_seconds=0, duration=info.duration)
    result = {'language': info.language, 'duration': info.duration, 'segments': [],
              'source': str(Path(args.src).resolve()), 'asr_model': str(args.model),
              'reference_used': bool(reference), 'source_kind': 'local-asr'}
    for s in segments:
        result['segments'].append({
            'start': round(s.start, 3),
            'end': round(s.end, 3),
            'text': s.text.strip(),
            'words': [{'word': w.word, 'start': round(w.start, 3), 'end': round(w.end, 3)} for w in (s.words or [])],
        })
        progress('transcribing', audio_seconds=round(s.end, 3), duration=info.duration,
                 segments=len(result['segments']))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    atomic_json(args.out, result)
    progress('completed', audio_seconds=info.duration, segments=len(result['segments']))
    print(f'DONE {len(result["segments"])} segments in {time.time() - t1:.1f}s → {args.out}', flush=True)


if __name__ == '__main__':
    main()
