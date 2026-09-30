import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { WorkflowActivity, WorkflowNode, WorkflowNodeId } from '../lib/contentWorkflowApi';
import SafeMarkdown from './SafeMarkdown';
import { IconSkills, IconStop } from './icons';
import './WorkflowNodeAssistant.css';

const PROMPTS: Record<WorkflowNodeId, string> = {
  brief: '告诉助手这次想表达什么，也可以请它根据现有材料补齐创作要求。',
  script: '例如：参考已有材料写一版完整口播稿，用自然口语，保留具体案例。',
  source: '描述原片、录制要求或素材路径，让助手帮你核对本步需要补充什么。',
  transcript: '说明字幕、错字或时间点上的问题，让助手在本节点处理。',
  storyboard: '告诉助手画面节奏、分段和素材要求，逐步完善分镜。',
  build: '描述想要的模板、画面、字幕或颜色，助手会在现有制作能力内调整。',
  review: '说明要检查的内容，或给出画面与字幕的具体时间点。',
  deliver: '完善标题、封面和成片要求，先查看产物再确认交付。',
  publish: '先让助手准备发布文案和本地发布包；平台提交仍受工作流确认约束。',
  archive: '说明希望怎样整理存档。实际写入仍通过本节点的预览与确认完成。',
};
const activityLabel = { status: '进展', generation: '正在生成', tool: '工具', result: '结果', error: '需处理' };
const time = (value?: string) => value ? new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '';

function currentActivities(items: WorkflowActivity[]) {
  const unique = new Map<string, WorkflowActivity>();
  items.forEach((item) => unique.set(item.id, item));
  const values = Array.from(unique.values());
  const currentRun = values.filter((item) => item.run_id).at(-1)?.run_id;
  const generation = values.filter((item) => item.kind === 'generation' && (!currentRun || item.run_id === currentRun)).at(-1);
  return { events: values.filter((item) => item.kind !== 'generation').slice(-12), generation };
}

interface Props {
  projectId: string; node: WorkflowNode; disabled: boolean; submitting: boolean; executing: boolean;
  budget: string; onBudget: (value: string) => void;
  onSend: (message: string, clientMessageId: string) => Promise<boolean>;
  onStop: () => Promise<void>;
  onRead: (title: string, content: string) => void;
}

export default function WorkflowNodeAssistant({ projectId, node, disabled, submitting, executing, budget, onBudget, onSend, onStop, onRead }: Props) {
  const context = `${projectId}:${node.id}`;
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [showLatest, setShowLatest] = useState(false);
  const thread = useRef<HTMLDivElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const followBottom = useRef(true);
  const composing = useRef(false);
  const sending = useRef(false);
  const pendingIds = useRef<Record<string, { text: string; id: string }>>({});
  const restored = useRef(new Set<string>());
  const previousContext = useRef(context);
  const text = drafts[context] || '';
  const messages = useMemo(() => node.chat?.messages || [], [node.chat?.messages]);
  const lastMessage = messages.at(-1);
  const chatRunning = node.chat?.status === 'running';
  const { events, generation } = currentActivities(node.activity || []);
  const changeText = (value: string) => setDrafts((previous) => ({ ...previous, [context]: value }));

  useLayoutEffect(() => {
    if (previousContext.current !== context) { previousContext.current = context; followBottom.current = true; }
    const panel = thread.current;
    if (!panel) return;
    if (followBottom.current) { panel.scrollTop = panel.scrollHeight; setShowLatest(false); }
    else setShowLatest(true);
  }, [context, messages.length, lastMessage?.id, lastMessage?.content, lastMessage?.status]);
  useEffect(() => {
    if (node.chat?.status !== 'failed' || !lastMessage || restored.current.has(`${context}:${lastMessage.id}`)) return;
    const user = messages.filter((message) => message.role === 'user').at(-1);
    if (!user) return;
    restored.current.add(`${context}:${lastMessage.id}`);
    setDrafts((previous) => previous[context] ? previous : { ...previous, [context]: user.content });
  }, [context, node.chat?.status, lastMessage, messages]);

  async function send() {
    const message = text.trim();
    if (!message || disabled || sending.current) return;
    sending.current = true; followBottom.current = true;
    const existing = pendingIds.current[context];
    const id = existing?.text === message ? existing.id : `chat-${crypto.randomUUID()}`;
    pendingIds.current[context] = { text: message, id };
    try {
      if (await onSend(message, id)) {
        delete pendingIds.current[context];
        setDrafts((previous) => previous[context]?.trim() === message ? { ...previous, [context]: '' } : previous);
      }
    } finally { sending.current = false; }
  }

  return <section className="cw-assistant" aria-label="节点助手">
    <header className="cw-assistant-heading"><div><span className="cw-assistant-icon"><IconSkills size={20} /></span><div><h3>节点助手</h3><p>围绕“{node.title}”补充要求、生成内容和处理问题</p></div></div><span className={`cw-assistant-state ${chatRunning ? 'is-live' : ''}`} role="status">{chatRunning ? '正在处理' : node.chat?.status === 'failed' ? '上次处理未完成' : node.chat?.status === 'stopped' ? '已停止' : '随时补充要求'}</span></header>
    <div className="cw-chat-thread" ref={thread} role="log" aria-label={`${node.title}对话`} onScroll={() => { const panel = thread.current; if (panel) { followBottom.current = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 55; if (followBottom.current) setShowLatest(false); } }}>
      {messages.length ? messages.map((message) => <article key={message.id} className={`cw-chat-message cw-chat-${message.role}`}><div className="cw-chat-meta"><strong>{message.role === 'user' ? '你' : '节点助手'}</strong><span>{time(message.created_at)}</span>{message.status === 'streaming' && <span className="cw-chat-streaming">正在生成</span>}{message.status === 'failed' && <span>未完成</span>}{message.status === 'stopped' && <span>已停止</span>}{message.role === 'assistant' && message.content && <button className="cw-chat-read" onClick={() => onRead(`${node.title} · 助手答复`, message.content)}>展开阅读</button>}</div>{message.role === 'assistant' ? <SafeMarkdown content={message.content || (message.status === 'streaming' ? '正在准备回复…' : '此次没有生成正文。')} /> : <p className="cw-chat-user-text">{message.content}</p>}</article>) : <div className="cw-chat-empty"><strong>在这里和助手一起完成本节点</strong><p>{PROMPTS[node.id]}</p><small>对话和处理结果会保留在当前项目。</small></div>}
    </div>
    {showLatest && <button className="cw-chat-latest" onClick={() => { followBottom.current = true; if (thread.current) thread.current.scrollTop = thread.current.scrollHeight; setShowLatest(false); }}>查看最新回复 ↓</button>}
    <div className="cw-activity" aria-label="执行动态"><div className="cw-activity-heading"><strong>执行动态</strong><span>{executing ? '实时更新中' : '本节点最近记录'}</span>{executing && <button className="cw-chat-stop" disabled={submitting} onClick={() => void onStop()}><IconStop size={12} />停止</button>}</div>
      {events.length ? <ol>{events.map((item) => <li className={`cw-activity-${item.kind}`} key={item.id}><span className="cw-activity-kind">{activityLabel[item.kind] || '进展'}</span><p>{item.text}</p><time>{time(item.at)}</time></li>)}</ol> : <p className="cw-activity-empty">{executing ? node.message || '任务已提交，正在等待处理结果…' : '执行本节点后，进展会显示在这里。'}</p>}
      {generation && <details className="cw-generation" open={executing || undefined}><summary>{executing ? '正在生成' : '最近生成的内容'}<span>{generation.text.length.toLocaleString()} 字符</span></summary><div><SafeMarkdown content={generation.text} /></div></details>}
    </div>
    <div className="cw-chat-composer"><label htmlFor={`cw-chat-${node.id}`}>发给节点助手</label><textarea id={`cw-chat-${node.id}`} ref={textarea} rows={4} value={text} maxLength={30000} disabled={submitting} placeholder={PROMPTS[node.id]} onChange={(event) => changeText(event.target.value)} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && !composing.current && event.keyCode !== 229 && !disabled) { event.preventDefault(); void send(); } }} />
      <div className="cw-chat-budget"><label>生成预算<select aria-label="生成预算" value={budget || 'large'} disabled={disabled} onChange={(event) => onBudget(event.target.value)}><option value="standard">标准</option><option value="large">高额度（默认）</option><option value="maximum">最大额度</option></select></label><span>控制本节点生成长度与等待时间，实际消耗按模型返回计费</span></div>
      <div className="cw-chat-compose-actions"><small>{executing ? '可先写新要求，完成或停止后再发送' : disabled ? '请等待当前任务完成后继续发送' : 'Enter 发送 · Shift + Enter 换行'}</small><button className="btn btn-primary" disabled={disabled || !text.trim()} onClick={() => void send()}>{submitting ? '提交中…' : chatRunning ? '正在处理…' : '发送给助手'}</button></div>
    </div>
  </section>;
}
