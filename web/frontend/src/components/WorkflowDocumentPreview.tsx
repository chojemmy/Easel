import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import SafeMarkdown from './SafeMarkdown';
import './WorkflowDocumentPreview.css';

export interface WorkflowDocument { title: string; content?: string; url?: string; path?: string; format?: 'markdown' | 'json' | 'text'; }
const MAX_BYTES = 1024 * 1024;

async function readDocument(url: string, signal: AbortSignal) {
  const target = new URL(url, window.location.origin);
  if (target.origin !== window.location.origin || !['http:', 'https:'].includes(target.protocol)) throw new Error('只能阅读本工作台提供的文件。');
  const response = await fetch(target.href, { signal, cache: 'no-store', redirect: 'error' });
  if (!response.ok) throw new Error(`文件读取失败（${response.status}）`);
  if (Number(response.headers.get('content-length') || 0) > MAX_BYTES) throw new Error('文件超过 1 MB，请下载后阅读。');
  if (!response.body) throw new Error('文件没有可读取的正文。');
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_BYTES) { await reader.cancel(); throw new Error('文件超过 1 MB，请下载后阅读。'); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size);
  let offset = 0;
  chunks.forEach((chunk) => { bytes.set(chunk, offset); offset += chunk.byteLength; });
  const text = new TextDecoder().decode(bytes);
  if (text.includes('\0')) throw new Error('这不是可阅读的文字文件，请下载后用对应应用打开。');
  return text;
}

export default function WorkflowDocumentPreview({ document: item, onClose }: { document: WorkflowDocument; onClose: () => void }) {
  const [content, setContent] = useState(item.content ?? '');
  const [loading, setLoading] = useState(item.content === undefined);
  const [error, setError] = useState('');
  const dialog = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  useEffect(() => { closeRef.current = onClose; }, [onClose]);
  useEffect(() => {
    const previous = window.document.activeElement as HTMLElement | null;
    const panel = dialog.current;
    panel?.focus();
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); closeRef.current(); }
      if (event.key !== 'Tab' || !panel) return;
      const targets = Array.from(panel.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], [tabindex="0"]'));
      const first = targets[0]; const last = targets.at(-1);
      if (!first) { event.preventDefault(); panel.focus(); }
      else if (event.shiftKey && (window.document.activeElement === first || window.document.activeElement === panel)) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && window.document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener('keydown', keyboard, true);
    return () => { window.removeEventListener('keydown', keyboard, true); if (previous?.isConnected) previous.focus(); };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    setError(''); setContent(item.content ?? ''); setLoading(item.content === undefined);
    if (item.content !== undefined) {
      if (new TextEncoder().encode(item.content).byteLength > MAX_BYTES) setError('正文超过 1 MB，请在原文件中阅读。');
      return () => controller.abort();
    }
    if (!item.url) { setLoading(false); setError('此文件暂未提供阅读地址。'); return; }
    void readDocument(item.url, controller.signal).then((text) => { if (live) setContent(text); }).catch((reason) => { if (live && !controller.signal.aborted) setError(reason instanceof Error ? reason.message : '读取失败'); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; controller.abort(); };
  }, [item.content, item.url]);
  const extension = (item.path || item.title).split('.').pop()?.toLowerCase();
  const format = item.format || (extension === 'json' ? 'json' : ['md', 'markdown'].includes(extension || '') ? 'markdown' : 'text');
  let display = content;
  if (format === 'json') { try { display = JSON.stringify(JSON.parse(content), null, 2); } catch { /* Invalid JSON remains readable as text. */ } }
  return createPortal(<div className="cw-reader-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="cw-reader" role="dialog" aria-modal="true" aria-labelledby="cw-reader-title" tabIndex={-1} ref={dialog}>
      <header className="cw-reader-header"><div><span>文档阅读</span><h2 id="cw-reader-title">{item.title}</h2>{item.path && <p title={item.path}>{item.path}</p>}</div><button className="cw-reader-close" onClick={onClose} aria-label="关闭文档阅读">×</button></header>
      <div className="cw-reader-body" tabIndex={0}>{loading ? <p role="status">正在读取正文…</p> : error ? <p className="cw-reader-error" role="alert">{error}</p> : format === 'markdown' ? <SafeMarkdown content={content} /> : <pre className="cw-reader-code">{display || '（空文件）'}</pre>}</div>
      <footer className="cw-reader-footer"><span>{format === 'markdown' ? 'Markdown · 外部图片不自动加载' : format === 'json' ? 'JSON · 格式化阅读' : '纯文本'}{!loading && !error ? ` · ${content.length.toLocaleString()} 字符` : ''}</span>{item.url && <a href={item.url} download>下载原文件</a>}</footer>
    </section>
  </div>, window.document.body);
}
