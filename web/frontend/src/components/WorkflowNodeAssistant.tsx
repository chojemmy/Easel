import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { WorkflowActivity, WorkflowNode, WorkflowNodeId, WorkflowSkillUse } from '../lib/contentWorkflowApi';
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
const phaseLabel: Record<string, string> = { preparing: '准备材料与工具', transcribing: '转录音频', correcting: '校对字幕', validating: '检查并保存产物' };
const executionLabel: Record<string, string> = { starting: '正在启动任务', running: '任务执行中', queued: '任务排队中', completed: '任务已完成', awaiting_review: '已生成，待你确认', blocked: '任务已停止 · 需处理', failed: '任务执行失败', stopped: '任务已停止', interrupted: '任务已中断' };
const elapsed = (start: string | undefined, end: string | undefined, tick: number) => {
  if (!start) return '';
  const seconds = Math.max(0, Math.floor(((end ? Date.parse(end) : tick) - Date.parse(start)) / 1000));
  return Number.isFinite(seconds) ? seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds} 秒` : '';
};

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
  onReadSkill: (skill: WorkflowSkillUse) => void;
  onLearnSkill: (skill: WorkflowSkillUse) => void;
}

export default function WorkflowNodeAssistant({ projectId, node, disabled, submitting, executing, budget, onBudget, onSend, onStop, onRead, onReadSkill, onLearnSkill }: Props) {
  const context = `${projectId}:${node.id}`;
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [showLatest, setShowLatest] = useState(false);
  const thread = useRef<HTMLDivElement>(null);
  const activityThread = useRef<HTMLOListElement>(null);
  const followActivity = useRef(true);
  const [tick, setTick] = useState(Date.now());
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
  const lastEvent = node.activity?.at(-1);
  const lastRun = node.runs.at(-1);
  const nodeRunning = ['running', 'queued'].includes(node.status);
  const attention = ['blocked', 'failed', 'interrupted', 'stopped'].includes(node.status);
  const stateLabel = nodeRunning ? phaseLabel[node.phase || ''] || '节点任务执行中' : chatRunning ? '助手正在处理' : attention ? executionLabel[node.status] : node.chat?.status === 'failed' ? '上次对话未完成' : node.chat?.status === 'stopped' ? '已停止' : '随时补充要求';
  const waitSeconds = lastEvent?.at ? Math.max(0, Math.floor((tick - Date.parse(lastEvent.at)) / 1000)) : 0;
  const changeText = (value: string) => setDrafts((previous) => ({ ...previous, [context]: value }));

  useLayoutEffect(() => {
    if (previousContext.current !== context) { previousContext.current = context; followBottom.current = true; }
    const panel = thread.current;
    if (!panel) return;
    if (followBottom.current) { panel.scrollTop = panel.scrollHeight; setShowLatest(false); }
    else setShowLatest(true);
  }, [context, messages.length, lastMessage?.id, lastMessage?.content, lastMessage?.status, lastMessage?.skills_used?.length, lastMessage?.execution?.status]);
  useEffect(() => {
    if (!executing) return;
    const timer = window.setInterval(() => setTick(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [executing]);
  useLayoutEffect(() => {
    if (followActivity.current && activityThread.current) activityThread.current.scrollTop = activityThread.current.scrollHeight;
  }, [context, lastEvent?.id, lastEvent?.at]);
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
    <header className="cw-assistant-heading"><div><span className="cw-assistant-icon"><IconSkills size={20} /></span><div><h3>节点助手</h3><p>围绕“{node.title}”补充要求、生成内容和处理问题</p></div></div><span className={`cw-assistant-state ${executing ? 'is-live' : attention ? 'needs-attention' : ''}`} role="status">{stateLabel}</span></header>
    <div className="cw-chat-thread" ref={thread} role="log" aria-label={`${node.title}对话`} onScroll={() => { const panel = thread.current; if (panel) { followBottom.current = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 55; if (followBottom.current) setShowLatest(false); } }}>
      {messages.length ? messages.map((message) => <article key={message.id} className={`cw-chat-message cw-chat-${message.role}`}><div className="cw-chat-meta"><strong>{message.role === 'user' ? '你' : '节点助手'}</strong><span>{time(message.created_at)}</span>{message.status === 'streaming' && <span className="cw-chat-streaming">正在生成</span>}{message.status === 'failed' && <span>未完成</span>}{message.status === 'stopped' && <span>已停止</span>}{message.role === 'assistant' && message.content && <button className="cw-chat-read" onClick={() => onRead(`${node.title} · 助手答复`, message.content)}>展开阅读</button>}</div>{message.role === 'assistant' ? <SafeMarkdown content={message.content || (message.status === 'streaming' ? '正在准备回复…' : '此次没有生成正文。')} /> : <p className="cw-chat-user-text">{message.content}</p>}{message.execution && <div className={`cw-chat-execution cw-execution-${message.execution.status}`} role="status"><strong>{message.execution.status === 'running' ? phaseLabel[message.execution.phase || ''] || '任务执行中' : executionLabel[message.execution.status || ''] || message.execution.status}</strong><span>{message.execution.message}</span>{message.execution.started_at && <small>{message.execution.finished_at ? '耗时' : '已运行'} {elapsed(message.execution.started_at, message.execution.finished_at, tick)}</small>}</div>}{message.role === 'assistant' && !!message.skills_used?.length && <div className="cw-chat-skill-trace"><strong>本轮已读取的原有 Skill</strong>{Array.from(new Map(message.skills_used.map((item) => [`${item.name}:${item.path}`, item])).values()).map((item) => <div key={`${item.name}:${item.path}`}><button disabled={submitting} onClick={() => onReadSkill(item)}><span>{item.name}</span><small>{item.path}</small></button><button className="cw-trace-learn" disabled={submitting} onClick={() => onLearnSkill(item)}>沉淀经验</button></div>)}</div>}</article>) : <div className="cw-chat-empty"><strong>在这里和助手一起完成本节点</strong><p>{PROMPTS[node.id]}</p><small>对话和处理结果会保留在当前项目。</small></div>}
    </div>
    {showLatest && <button className="cw-chat-latest" onClick={() => { followBottom.current = true; if (thread.current) thread.current.scrollTop = thread.current.scrollHeight; setShowLatest(false); }}>查看最新回复 ↓</button>}
    <div className="cw-activity" aria-label="执行动态"><div className="cw-activity-heading"><strong>执行动态</strong><span>{executing ? '实时更新中' : '本节点最近记录'}</span>{executing && <button className="cw-chat-stop" disabled={submitting} onClick={() => void onStop()}><IconStop size={12} />停止</button>}</div>
      {(nodeRunning || attention || chatRunning) && <div className={`cw-execution-summary ${attention ? 'needs-attention' : ''}`} role="status"><strong>{stateLabel}{nodeRunning && lastRun?.started_at ? ` · 已运行 ${elapsed(lastRun.started_at, undefined, tick)}` : ''}</strong><p>{chatRunning && !nodeRunning ? lastEvent?.kind === 'generation' ? '正在生成答复…' : lastEvent?.text || '正在准备回复…' : node.message}</p>{executing && waitSeconds >= 15 && <small>上次进展在 {waitSeconds} 秒前，正在等待当前工具或模型返回。</small>}</div>}
      {events.length ? <ol ref={activityThread} onScroll={() => { const panel = activityThread.current; if (panel) followActivity.current = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 35; }}>{events.map((item) => <li className={`cw-activity-${item.kind}`} key={item.id}><span className="cw-activity-kind">{activityLabel[item.kind] || '进展'}</span><p>{item.text}</p><time>{time(item.at)}</time></li>)}</ol> : <p className="cw-activity-empty">{executing ? node.message || '任务已提交，正在等待处理结果…' : '执行本节点后，进展会显示在这里。'}</p>}
      {generation && <details className="cw-generation" open={executing || undefined}><summary>{executing ? '正在生成' : '最近生成的内容'}<span>{generation.text.length.toLocaleString()} 字符</span></summary><div><SafeMarkdown content={generation.text} /></div></details>}
    </div>
    <div className="cw-chat-composer"><label htmlFor={`cw-chat-${node.id}`}>发给节点助手</label><textarea id={`cw-chat-${node.id}`} ref={textarea} rows={4} value={text} maxLength={30000} disabled={submitting} placeholder={PROMPTS[node.id]} onChange={(event) => changeText(event.target.value)} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && !composing.current && event.keyCode !== 229 && !disabled) { event.preventDefault(); void send(); } }} />
      <div className="cw-chat-budget"><label>生成预算<select aria-label="生成预算" value={budget || 'large'} disabled={disabled} onChange={(event) => onBudget(event.target.value)}><option value="standard">标准</option><option value="large">高额度（默认）</option><option value="maximum">最大额度</option></select></label><span>控制本节点生成长度与等待时间，实际消耗按模型返回计费</span></div>
      <div className="cw-chat-compose-actions"><small>{executing ? '可先写新要求，完成或停止后再发送' : disabled ? '请等待当前任务完成后继续发送' : 'Enter 发送 · Shift + Enter 换行'}</small><button className="btn btn-primary" disabled={disabled || !text.trim()} onClick={() => void send()}>{submitting ? '提交中…' : chatRunning ? '正在处理…' : '发送给助手'}</button></div>
    </div>
  </section>;
}
