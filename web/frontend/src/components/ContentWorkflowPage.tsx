import { useCallback, useEffect, useRef, useState } from 'react';
import type { ChangeEvent, ReactNode } from 'react';
import {
  approveWorkflowNode, applyWorkflowLearning, archiveWorkflow, createWorkflow,
  fetchWorkflow, fetchWorkflows, fetchWorkflowSkills, importWorkflowManuscript,
  previewWorkflowArchive, previewWorkflowLearning, runWorkflowNode, searchWorkflowNotes,
  sendWorkflowFeedback, stopWorkflowNode, updateWorkflow, workflowArtifactUrl, workflowSkillExportUrl, workflowAllSkillsExportUrl, reconcileWorkflowPublish,
} from '../lib/contentWorkflowApi';
import type {
  ArchivePreview, LearningProposal, ObsidianNote, WorkflowArtifact, WorkflowDefinition,
  WorkflowKind, WorkflowManuscript, WorkflowNode, WorkflowNodeId, WorkflowProject, WorkflowSkills,
} from '../lib/contentWorkflowApi';
import { uploadFiles } from '../lib/api';
import { IconArchive, IconCheck, IconChevron, IconFile, IconFolder, IconLayers, IconPlus, IconRefresh, IconSearch, IconSkills, IconStop, IconVideo } from './icons';
import './ContentWorkflowPage.css';

const NODE_ORDER: WorkflowNodeId[] = ['brief', 'script', 'source', 'transcript', 'storyboard', 'build', 'review', 'deliver', 'publish', 'archive'];
const NODE_HELP: Record<WorkflowNodeId, string> = {
  brief: '明确这篇内容要讲什么、给谁看，以及你对风格的要求。',
  script: '同一项目可以保留多份稿件和参考资料，选择一份主稿进入后续流程。',
  source: '导入录好的原片。大文件建议直接填写本机完整路径。',
  transcript: '转录录音、校对文字和时间轴，为剪辑与分镜提供依据。',
  storyboard: '把主稿和原片拆成场景，确认画面、素材、字幕与节奏。',
  build: '按已确认的分镜制作画面，执行进展会显示在当前节点。',
  review: '观看预览并核对检查结果，在修改意见中注明时间点。',
  deliver: '确认成品、封面和发布文案，所有交付文件集中保存在这里。',
  publish: '先保存平台草稿，核对回执后再决定公开发布。',
  archive: '预览存入 Obsidian 的正文与文件清单，再确认写入。',
};
const STATUS: Record<string, { label: string; tone: string }> = {
  pending: { label: '未开始', tone: 'muted' }, idle: { label: '未开始', tone: 'muted' },
  running: { label: '执行中', tone: 'blue' }, queued: { label: '排队中', tone: 'blue' },
  awaiting: { label: '待你确认', tone: 'amber' }, awaiting_approval: { label: '待你确认', tone: 'amber' },
  awaiting_review: { label: '待你确认', tone: 'amber' },
  awaiting_input: { label: '待补充', tone: 'amber' }, ready: { label: '待你确认', tone: 'amber' },
  review: { label: '待你确认', tone: 'amber' }, needs_input: { label: '待补充', tone: 'amber' },
  blocked: { label: '需处理', tone: 'amber' }, failed: { label: '执行失败', tone: 'red' },
  error: { label: '执行失败', tone: 'red' }, done: { label: '已完成', tone: 'green' },
  completed: { label: '已完成', tone: 'green' }, approved: { label: '已确认', tone: 'green' },
  stale: { label: '需要更新', tone: 'amber' }, invalidated: { label: '需要更新', tone: 'amber' },
  skipped: { label: '无需执行', tone: 'muted' }, paused: { label: '已暂停', tone: 'muted' },
  cancelled: { label: '已停止', tone: 'muted' }, stopped: { label: '已停止', tone: 'muted' },
  unknown: { label: '结果待确认', tone: 'amber' }, unverified: { label: '结果待确认', tone: 'amber' },
  result_unknown: { label: '结果待确认', tone: 'amber' }, draft_saved: { label: '草稿已保存', tone: 'green' },
  interrupted: { label: '已中断', tone: 'amber' },
};
const finished = (node: WorkflowNode) => ['done', 'completed', 'approved', 'skipped', 'draft_saved'].includes(node.status);
const running = (node: WorkflowNode) => ['running', 'queued'].includes(node.status);
const dateLabel = (date?: string) => date ? new Date(date).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
const sourceLabel = { manual: '手动录入', obsidian: 'Obsidian', generated: '生成稿', file: '本地文件' };
const asText = (value: unknown) => typeof value === 'string' ? value : value == null ? '' : JSON.stringify(value, null, 2);
const skillVersion = (version?: string) => version ? version.replace(/^sha256:/, '').slice(0, 8) : '默认';
function updateWorkflowLocation(id: string | null) {
  const url = new URL(window.location.href);
  if (id) url.searchParams.set('workflow', id);
  else url.searchParams.delete('workflow');
  window.history.replaceState(window.history.state, '', url);
}

function StatusBadge({ status }: { status: string }) {
  const item = STATUS[status] || { label: status || '未开始', tone: 'muted' };
  return <span className={`cw-status cw-${item.tone}`}><i />{item.label}</span>;
}
function Field({ label, children, wide = false }: { label: string; children: ReactNode; wide?: boolean }) {
  return <label className={`cw-field${wide ? ' cw-wide' : ''}`}><span>{label}</span>{children}</label>;
}
function Artifact({ artifact }: { artifact: WorkflowArtifact }) {
  const url = workflowArtifactUrl(artifact);
  const kind = artifact.kind || '';
  const extension = artifact.path?.split('.').pop()?.toLowerCase() || '';
  const video = ['video', 'final_video', 'rendered_video', 'preview_video'].includes(kind) || ['mp4', 'webm', 'mov'].includes(extension);
  const picture = ['image', 'cover', 'cover_vertical', 'cover_horizontal', 'inspection_frame'].includes(kind) || ['png', 'jpg', 'jpeg', 'webp'].includes(extension);
  return <div className="cw-artifact">
    {url && video && <video controls preload="metadata" src={url} />}
    {url && picture && <img src={url} alt={artifact.name} loading="lazy" />}
    <div className="cw-artifact-label"><IconFile size={16} /><div><strong>{artifact.name}</strong><small title={artifact.path}>{artifact.path}</small></div>{url && <a href={url} target="_blank" rel="noreferrer" className="cw-link">打开 <IconChevron size={14} /></a>}</div>
  </div>;
}

export default function ContentWorkflowPage() {
  const [projects, setProjects] = useState<WorkflowProject[]>([]);
  const [definitions, setDefinitions] = useState<WorkflowDefinition[]>([]);
  const [defaults, setDefaults] = useState<{ vault?: string; output_dir?: string }>({});
  const [project, setProject] = useState<WorkflowProject | null>(null);
  const [draft, setDraft] = useState<WorkflowProject | null>(null);
  const [nodeId, setNodeId] = useState<WorkflowNodeId>('brief');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [dirty, setDirty] = useState(false);
  const dirtyRef = useRef(false);
  const selectedRef = useRef<string | null>(null);
  const [filter, setFilter] = useState('');
  const [createOpen, setCreateOpen] = useState(false);
  const [newTitle, setNewTitle] = useState('');
  const [newKind, setNewKind] = useState<WorkflowKind>('video');
  const [manuscriptId, setManuscriptId] = useState('');
  const [importOpen, setImportOpen] = useState(false);
  const [importPath, setImportPath] = useState('');
  const [noteQuery, setNoteQuery] = useState('');
  const [notes, setNotes] = useState<ObsidianNote[]>([]);
  const [feedback, setFeedback] = useState('');
  const [feedbackTarget, setFeedbackTarget] = useState<WorkflowNodeId>('brief');
  const [skills, setSkills] = useState<WorkflowSkills | null>(null);
  const [skillsOpen, setSkillsOpen] = useState(false);
  const [learnOpen, setLearnOpen] = useState(false);
  const [instruction, setInstruction] = useState('');
  const [proposal, setProposal] = useState<LearningProposal | null>(null);
  const [archivePreview, setArchivePreview] = useState<ArchivePreview | null>(null);
  const [publishConfirm, setPublishConfirm] = useState(false);
  const [reconcileOpen, setReconcileOpen] = useState(false);
  const [reconcileNote, setReconcileNote] = useState('');
  const manuscriptFile = useRef<HTMLInputElement>(null);
  const mediaFile = useRef<HTMLInputElement>(null);

  const markDirty = () => { dirtyRef.current = true; setDirty(true); setArchivePreview(null); };
  const acceptProject = useCallback((value: WorkflowProject, forceDraft = true) => {
    setProjects((previous) => [value, ...previous.filter((item) => item.id !== value.id)]);
    if (selectedRef.current !== value.id) return;
    setProject(value);
    if (forceDraft || !dirtyRef.current) { setDraft(value); setDirty(false); dirtyRef.current = false; }
  }, []);
  const loadIndex = useCallback(async () => {
    const data = await fetchWorkflows();
    setProjects(data.projects || []); setDefinitions(data.nodes || []); setDefaults(data.defaults || {});
    return data;
  }, []);
  useEffect(() => {
    let active = true;
    void loadIndex().then((data) => {
      if (!active) return;
      const id = new URLSearchParams(window.location.search).get('workflow');
      if (id === null) return;
      const value = data.projects.find((item) => item.id === id);
      if (!value) { setError('未找到链接中的内容项目，请从项目列表重新选择。'); return; }
      selectedRef.current = value.id;
      acceptProject(value);
      setNodeId(value.nodes.find((item) => !finished(item))?.id || 'archive');
      setManuscriptId(value.primary_manuscript_id || value.manuscripts[0]?.id || '');
    }).catch((e: Error) => { if (active) setError(e.message); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [loadIndex, acceptProject]);
  useEffect(() => {
    if (!project?.id) return;
    let live = true;
    const id = project.id;
    const tick = async () => {
      if (document.hidden) return;
      try { const value = await fetchWorkflow(id); if (live && selectedRef.current === id) acceptProject(value, false); }
      catch (e) { if (live) setError(e instanceof Error ? e.message : '刷新失败'); }
    };
    const timer = window.setInterval(() => void tick(), 2000);
    return () => { live = false; window.clearInterval(timer); };
  }, [project?.id, acceptProject]);
  useEffect(() => { setFeedback(''); setFeedbackTarget(nodeId === 'review' || nodeId === 'deliver' ? 'build' : nodeId); setSkills(null); setSkillsOpen(false); setLearnOpen(false); setProposal(null); setInstruction(''); setArchivePreview(null); }, [project?.id, nodeId]);
  useEffect(() => {
    const onEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || busy) return;
      setCreateOpen(false); setLearnOpen(false); setPublishConfirm(false); setReconcileOpen(false);
    };
    window.addEventListener('keydown', onEscape);
    return () => window.removeEventListener('keydown', onEscape);
  }, [busy]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);

  async function perform(label: string, fn: () => Promise<void>) {
    if (busy) return;
    setBusy(label); setError(''); setNotice('');
    try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : '操作失败，请查看节点状态后重试。'); }
    finally { setBusy(''); }
  }
  async function openProject(id: string) {
    if (dirtyRef.current && !window.confirm('当前修改尚未保存。放弃修改并切换项目？')) return;
    await perform('open', async () => {
      const value = await fetchWorkflow(id); selectedRef.current = id; acceptProject(value); updateWorkflowLocation(id);
      setNodeId(value.nodes.find((item) => !finished(item))?.id || 'archive');
      setManuscriptId(value.primary_manuscript_id || value.manuscripts[0]?.id || '');
    });
  }
  function backToProjects() {
    if (dirtyRef.current && !window.confirm('当前修改尚未保存。放弃修改并返回项目列表？')) return;
    selectedRef.current = null; setProject(null); setDraft(null); setDirty(false); dirtyRef.current = false;
    updateWorkflowLocation(null);
    void loadIndex().catch((e: Error) => setError(e.message));
  }
  async function saveDraft(): Promise<WorkflowProject> {
    if (!draft) throw new Error('请选择项目');
    if (!dirtyRef.current) return project || draft;
    const value = await updateWorkflow(draft.id, {
      title: draft.title, brief: draft.brief, manuscripts: draft.manuscripts, content_version: draft.content_version,
      primary_manuscript_id: draft.primary_manuscript_id, media: draft.media, settings: draft.settings,
    });
    acceptProject(value); setNotice('修改已保存'); return value;
  }
  function editProject(change: Partial<WorkflowProject>) { if (draft) { markDirty(); setDraft({ ...draft, ...change }); } }
  function editSetting(key: string, value: string) { if (draft) editProject({ settings: { ...draft.settings, [key]: value } }); }
  function addManuscript(content = '', title = '新稿件', source: WorkflowManuscript['source_kind'] = 'manual', path?: string) {
    if (!draft) return;
    const id = `manuscript-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    editProject({ manuscripts: [...draft.manuscripts, { id, title, content, source_kind: source, source_path: path, version: 1 }], primary_manuscript_id: draft.primary_manuscript_id || id });
    setManuscriptId(id);
  }
  function editManuscript(change: Partial<WorkflowManuscript>) {
    if (!draft) return;
    editProject({ manuscripts: draft.manuscripts.map((item) => item.id === manuscriptId ? { ...item, ...change } : item) });
  }
  async function readManuscriptFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]; event.target.value = '';
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) { setError('文字稿文件超过 5 MB，请先选取需要的内容。'); return; }
    await perform('file', async () => addManuscript(await file.text(), file.name.replace(/\.(md|txt)$/i, ''), 'file', file.name));
  }
  async function executeNode(extra: Record<string, unknown> = {}) {
    await perform('run', async () => {
      const saved = await saveDraft();
      const result = await runWorkflowNode(saved.id, nodeId, { action: 'run', ...extra });
      acceptProject(result); setNotice('任务已提交，进展会自动更新。');
    });
  }
  async function loadSkills() {
    if (!project) return;
    await perform('skills', async () => { setSkills(await fetchWorkflowSkills(project.id, nodeId)); setSkillsOpen(true); });
  }

  const node = project?.nodes.find((item) => item.id === nodeId);
  const activeManuscript = draft?.manuscripts.find((item) => item.id === manuscriptId) || draft?.manuscripts.find((item) => item.id === draft.primary_manuscript_id) || draft?.manuscripts[0];
  const isRunning = !!node && running(node);
  const anyRunning = !!project?.nodes.some(running);
  const orderedNodes = project ? NODE_ORDER.map((id) => project.nodes.find((item) => item.id === id)).filter((item): item is WorkflowNode => !!item) : [];
  const doneCount = orderedNodes.filter((item) => finished(item) && item.status !== 'skipped').length;
  const requiredCount = orderedNodes.filter((item) => item.status !== 'skipped').length;
  const attention = orderedNodes.find((item) => ['failed', 'error', 'blocked', 'unknown', 'unverified', 'result_unknown', 'stale', 'invalidated'].includes(item.status)) || orderedNodes.find((item) => running(item)) || orderedNodes.find((item) => !finished(item));
  const settings = draft?.settings || {};
  const archiveResult = project?.archive;
  const publishPlatform = asText(settings.publish_platform) || 'weixin-channels';
  const canSavePlatformDraft = project?.kind === 'video' && publishPlatform === 'weixin-channels';
  const canPublishPublicly = project?.kind === 'video' && ['weixin-channels', 'kuaishou'].includes(publishPlatform);

  function renderManuscripts() {
    if (!draft) return null;
    return <section className="cw-section">
      <div className="cw-section-heading"><div><h3>稿件与来源</h3><p>主稿用于制作；参考稿和历史版本保留在当前项目。</p></div><span className="cw-counter">{draft.manuscripts.length} 份稿件</span></div>
      <div className="cw-toolbar"><button className="btn" disabled={!!busy} onClick={() => addManuscript()}><IconPlus size={15} />手动添加</button><button className="btn" disabled={!!busy} onClick={() => { setImportOpen(!importOpen); setNotes([]); }}><IconFolder size={15} />从 Obsidian 导入</button><button className="btn" disabled={!!busy} onClick={() => manuscriptFile.current?.click()}><IconFile size={15} />导入文字文件</button><input ref={manuscriptFile} type="file" accept=".md,.txt" className="cw-hidden" onChange={(e) => void readManuscriptFile(e)} /></div>
      {importOpen && <div className="cw-import-panel"><label className="cw-field"><span>搜索笔记</span><div className="cw-input-row"><input aria-label="搜索笔记" value={noteQuery} onChange={(e) => setNoteQuery(e.target.value)} placeholder="输入标题或关键词" onKeyDown={(e) => { if (e.key === 'Enter' && noteQuery.trim()) void perform('search', async () => setNotes((await searchWorkflowNotes(noteQuery)).notes || [])); }} /><button className="btn" disabled={!!busy || !noteQuery.trim()} onClick={() => void perform('search', async () => setNotes((await searchWorkflowNotes(noteQuery)).notes || []))}><IconSearch size={16} />搜索</button></div></label>
        {notes.length > 0 && <div className="cw-note-results">{notes.map((note) => <button key={note.path} onClick={() => setImportPath(note.path)} className={importPath === note.path ? 'selected' : ''}><strong>{note.title}</strong><small>{note.path}</small>{note.excerpt && <p>{note.excerpt}</p>}</button>)}</div>}
        <Field label="笔记完整路径"><div className="cw-input-row"><input aria-label="笔记完整路径" value={importPath} onChange={(e) => setImportPath(e.target.value)} placeholder={defaults.vault ? `${defaults.vault} / 笔记.md` : 'Obsidian 笔记路径'} /><button className="btn btn-primary" disabled={!!busy || !importPath.trim()} onClick={() => void perform('import', async () => { const saved = await saveDraft(); const value = await importWorkflowManuscript(saved.id, importPath.trim()); acceptProject(value); setManuscriptId(value.manuscripts.at(-1)?.id || ''); setImportOpen(false); setNotice('笔记已作为稿件导入'); })}>导入</button></div></Field><p className="cw-hint">读取笔记内容到当前项目，不改动原笔记。</p></div>}
      {draft.manuscripts.length > 0 ? <><div className="cw-manuscript-tabs" role="tablist" aria-label="稿件">{draft.manuscripts.map((item) => <button key={item.id} role="tab" aria-selected={activeManuscript?.id === item.id} className={activeManuscript?.id === item.id ? 'active' : ''} onClick={() => setManuscriptId(item.id)}>{item.title || '未命名稿件'}{draft.primary_manuscript_id === item.id && <b>主稿</b>}</button>)}</div>
        {activeManuscript && <div className="cw-manuscript-editor"><div className="cw-input-row"><input aria-label="稿件标题" value={activeManuscript.title} onChange={(e) => { setManuscriptId(activeManuscript.id); editProject({ manuscripts: draft.manuscripts.map((item) => item.id === activeManuscript.id ? { ...item, title: e.target.value } : item) }); }} /><button className="btn" disabled={draft.primary_manuscript_id === activeManuscript.id} onClick={() => editProject({ primary_manuscript_id: activeManuscript.id })}>{draft.primary_manuscript_id === activeManuscript.id ? <><IconCheck size={14} />当前主稿</> : '设为主稿'}</button></div><div className="cw-meta-line"><span>{sourceLabel[activeManuscript.source_kind]}</span><span>v{activeManuscript.version}</span><span>{activeManuscript.content.length} 字符</span>{activeManuscript.source_path && <span title={activeManuscript.source_path}>{activeManuscript.source_path}</span>}</div><textarea className="cw-script-editor" aria-label="稿件正文" value={activeManuscript.content} onChange={(e) => { if (manuscriptId === activeManuscript.id) editManuscript({ content: e.target.value }); else { setManuscriptId(activeManuscript.id); editProject({ manuscripts: draft.manuscripts.map((item) => item.id === activeManuscript.id ? { ...item, content: e.target.value } : item) }); } }} placeholder="写下口播稿、文章或参考内容…" /></div>}</> : <div className="cw-empty-inline"><IconFile size={28} /><p>还没有稿件。添加已有内容，或保存要求后点击“依据材料生成新稿”。</p></div>}
    </section>;
  }

  function renderNodeBody() {
    if (!draft || !node) return null;
    if (nodeId === 'brief') return <><div className="cw-form-grid"><Field label="项目名称"><input value={draft.title} onChange={(e) => editProject({ title: e.target.value })} /></Field><Field label="主题"><input value={draft.brief.topic || ''} onChange={(e) => editProject({ brief: { ...draft.brief, topic: e.target.value } })} placeholder="这篇内容最想讲清楚什么" /></Field><Field label="目标受众"><input value={draft.brief.audience || ''} onChange={(e) => editProject({ brief: { ...draft.brief, audience: e.target.value } })} placeholder="希望影响哪一类读者或观众" /></Field><Field label="目标平台"><input value={draft.brief.platform || ''} onChange={(e) => editProject({ brief: { ...draft.brief, platform: e.target.value } })} placeholder="例如：微信视频号、公众号" /></Field><Field label={draft.kind === 'video' ? '目标时长' : '目标篇幅'}><input value={draft.brief.duration || ''} onChange={(e) => editProject({ brief: { ...draft.brief, duration: e.target.value } })} placeholder={draft.kind === 'video' ? '例如：3 分钟' : '例如：1500 字'} /></Field><Field label="内容风格"><input value={draft.brief.style || ''} onChange={(e) => editProject({ brief: { ...draft.brief, style: e.target.value } })} placeholder="例如：自然口语、观点明确、有具体例子" /></Field><Field label="具体要求" wide><textarea rows={5} value={draft.brief.requirements || ''} onChange={(e) => editProject({ brief: { ...draft.brief, requirements: e.target.value } })} placeholder="必须表达的观点、要引用的材料、希望避免的说法…" /></Field></div>{renderManuscripts()}</>;
    if (nodeId === 'script') return renderManuscripts();
    if (nodeId === 'source') return draft.kind === 'article' ? <div className="cw-callout">文章项目直接沿用已确认的主稿；节点是否需要执行，以当前流程状态为准。</div> : <div className="cw-section"><Field label="原片完整路径"><input value={draft.media.source_path || ''} onChange={(e) => editProject({ media: { ...draft.media, source_path: e.target.value } })} placeholder="例如：D:\视频素材\这次录制.mp4" /></Field><p className="cw-hint">使用本机路径无需通过浏览器上传。也可以上传小文件：</p><button className="btn" disabled={!!busy} onClick={() => mediaFile.current?.click()}><IconPlus size={15} />选择视频文件</button><input ref={mediaFile} className="cw-hidden" type="file" accept="video/*" onChange={(e) => { const file = e.target.files?.[0]; e.target.value = ''; if (file) void perform('upload', async () => { const result = await uploadFiles([file], `workflow_${draft.id}`); if (!result[0]) throw new Error('没有收到上传结果'); editProject({ media: { ...draft.media, source_path: result[0].path } }); setNotice('原片已上传，请保存并检查素材。'); }); }} /><Field label="现成字幕路径（可选）"><input value={draft.media.transcript_path || ''} onChange={(e) => editProject({ media: { ...draft.media, transcript_path: e.target.value } })} placeholder="已有 .srt / .vtt / .json 时可复用" /></Field></div>;
    if (nodeId === 'transcript') return <div className="cw-section"><Field label="已有或校对后的字幕路径"><input value={draft.media.transcript_path || ''} onChange={(e) => editProject({ media: { ...draft.media, transcript_path: e.target.value } })} placeholder="不填则按现有转录策略生成" /></Field><div className="cw-callout">转录与剪辑结果会显示在本步产物中。发现错字或口误时，在下方提交修改意见，附上对应时间点。</div></div>;
    if (nodeId === 'storyboard') return <div className="cw-section"><Field label="已有分镜设计表路径（可选）"><input value={asText(settings.design_table_path)} onChange={(e) => editSetting('design_table_path', e.target.value)} placeholder="可留空，由当前主稿和素材生成" /></Field><div className="cw-callout">先检查每场台词、画面、素材和字幕，再确认分镜。对某一场的要求可以写在本节点修改意见里。</div></div>;
    if (nodeId === 'build') return <div className="cw-form-grid"><Field label="制作模板"><select aria-label="制作模板" value={asText(settings.template) || 'documentary'} onChange={(e) => editSetting('template', e.target.value)}><option value="documentary">纪录片 · documentary</option><option value="editorial">杂志 · editorial</option></select></Field><Field label="画面比例"><select aria-label="画面比例" value={asText(settings.output_ratio) || '9:16'} onChange={(e) => editSetting('output_ratio', e.target.value)}><option value="9:16">9:16 竖屏</option><option value="16:9">16:9 横屏</option><option value="1:1">1:1 方形</option></select></Field><Field label="视觉风格"><input value={asText(settings.visual_style)} onChange={(e) => editSetting('visual_style', e.target.value)} placeholder="沿用分镜风格，或补充具体要求" /></Field><Field label="字幕要求" wide><textarea rows={3} value={asText(settings.subtitle_style)} onChange={(e) => editSetting('subtitle_style', e.target.value)} placeholder="字幕位置、字号、关键词强调方式…" /></Field></div>;
    if (nodeId === 'review') return <div className="cw-callout">打开下方预览和检查报告。确认内容、字幕、画面与声音后，再批准当前版本。需要调整时，提交时间点和具体修改意见。</div>;
    if (nodeId === 'deliver' || nodeId === 'publish') return <><div className="cw-form-grid"><Field label={nodeId === 'deliver' ? '待导入成片路径（可选）' : '发布成片路径'}><input value={nodeId === 'publish' ? asText(draft.media.final_path) : asText(settings.final_path)} readOnly={nodeId === 'publish'} onChange={(e) => editSetting('final_path', e.target.value)} placeholder={nodeId === 'deliver' ? '通过“导入并验证已有成片”采用此文件' : '默认采用本项目交付的成片'} /></Field><Field label="封面路径"><input value={asText(draft.media.cover_path)} readOnly placeholder="完成成片后自动生成，见本步产物" /></Field><Field label="发布平台"><select aria-label="发布平台" value={asText(settings.publish_platform) || 'weixin-channels'} onChange={(e) => editSetting('publish_platform', e.target.value)}><option value="weixin-channels">微信视频号</option><option value="douyin">抖音（本地发布包）</option><option value="xiaohongshu">小红书（本地发布包）</option><option value="bilibili">B站（本地发布包）</option><option value="kuaishou">快手</option><option value="wechat-oa">公众号（本地发布包）</option></select></Field><Field label="发布标题"><input value={asText(settings.publish_title)} onChange={(e) => editSetting('publish_title', e.target.value)} placeholder={draft.title} /></Field><Field label="正文 / 描述" wide><textarea rows={5} value={asText(settings.publish_description)} onChange={(e) => editSetting('publish_description', e.target.value)} /></Field><Field label="话题标签" wide><input value={asText(settings.publish_tags)} onChange={(e) => editSetting('publish_tags', e.target.value)} placeholder="多个话题用逗号分隔" /></Field></div>{nodeId === 'publish' && <div className="cw-callout">{draft.kind === 'article' ? '文章目前可准备本地发布包并归档；平台草稿与公开发布适配尚未开放。' : '先准备本地发布包核对文案与素材。目前平台草稿支持视频号，公开发布支持视频号和快手；其他平台可先导出本地发布包。结果待确认时请先核对平台。'}</div>}</>;
    if (nodeId === 'archive') return <div className="cw-section">
      {archiveResult && <div className="cw-archive-preview cw-saved-archive">
        <div className="cw-section-heading"><h3>已保存的存档结果</h3></div>
        {archiveResult.plan?.note_path && <div className="cw-archive-target"><span>文件</span><code>{archiveResult.plan.note_path}</code></div>}
        {archiveResult.at && <p className="cw-hint" title={archiveResult.at}>存档时间：{dateLabel(archiveResult.at)}</p>}
        {(archiveResult.plan?.publication_status || asText(archiveResult.publication_status)) && <p className="cw-hint">发布状态：{asText(archiveResult.plan?.publication_status || archiveResult.publication_status)}</p>}
        {archiveResult.content && <details className="cw-skill-content"><summary>查看已存档正文</summary><pre className="cw-document">{archiveResult.content}</pre></details>}
      </div>}
      <Field label="Obsidian 存档目录"><input value={asText(settings.archive_folder)} onChange={(e) => editSetting('archive_folder', e.target.value)} placeholder={defaults.vault ? `默认在 ${defaults.vault} 内创建项目存档` : '使用已配置的 Obsidian 存档目录'} /></Field><p className="cw-hint">存档包含主稿、发布文案、成品链接和发布状态。先预览本次将写入的实际文件。</p><button className="btn" disabled={!!busy || anyRunning} onClick={() => void perform('archive-preview', async () => { const saved = await saveDraft(); setArchivePreview(await previewWorkflowArchive(saved.id)); })}><IconArchive size={16} />预览 Obsidian 存档</button>{archivePreview && <div className="cw-archive-preview"><div className="cw-section-heading"><h3>本次存档清单</h3><StatusBadge status={archivePreview.status === 'conflict' ? 'blocked' : 'ready'} /></div>{archivePreview.targets?.map((target) => <div className="cw-archive-target" key={target.path}><span>{target.action}</span><code>{target.path}</code></div>)}{archivePreview.plan?.note_path && !archivePreview.targets?.length && <code>{archivePreview.plan.note_path}</code>}<pre className="cw-document">{archivePreview.content || archivePreview.markdown || '没有可展示的正文'}</pre><button className="btn btn-primary" disabled={!!busy || archivePreview.status === 'conflict' || !archivePreview.hash} onClick={() => void perform('archive', async () => { acceptProject(await archiveWorkflow(draft.id, archivePreview.hash)); setArchivePreview(null); setNotice('已写入 Obsidian，详情见本步产物。'); })}>确认写入上述文件</button>{archivePreview.status === 'conflict' && <p className="cw-hint">目标文件存在冲突，请调整目录或处理冲突后重新预览。</p>}</div>}</div>;
    return null;
  }

  return <div className="cw-page">
    <header className="cw-page-header"><div><div className="cw-eyebrow"><IconLayers size={16} />CONTENT WORKFLOW</div><h1>内容工作流</h1><p>一篇内容，一个项目。从资料与稿件，一路看到成品、发布和存档。</p></div><div className="cw-toolbar"><button className="btn cw-icon-button" title="刷新项目" aria-label="刷新项目" disabled={!!busy} onClick={() => void perform('refresh', async () => { await loadIndex(); if (project) acceptProject(await fetchWorkflow(project.id), false); })}><IconRefresh size={17} /></button><button className="btn btn-primary" onClick={() => setCreateOpen(true)}><IconPlus size={16} />新建内容项目</button></div></header>
    {error && <div className="cw-alert cw-alert-error" role="alert"><span>{error}</span>{dirty && project && <button className="btn" onClick={() => { if (window.confirm('放弃尚未保存的本地修改，重新载入服务器上的最新内容？')) void perform('reload', async () => acceptProject(await fetchWorkflow(project.id))); }}>载入最新版本</button>}<button onClick={() => setError('')} aria-label="关闭错误提示">×</button></div>}
    {notice && <div className="cw-alert cw-alert-success" role="status"><span>{notice}</span><button onClick={() => setNotice('')} aria-label="关闭提示">×</button></div>}
    {error && (createOpen || learnOpen || publishConfirm || reconcileOpen) && <div className="cw-alert cw-alert-error cw-modal-error" role="alert"><span>{error}</span><button onClick={() => setError('')} aria-label="关闭错误提示">×</button></div>}
    {loading ? <div className="cw-empty"><IconRefresh size={28} /><p>正在读取内容项目…</p></div> : !project || !draft ? <>
      <div className="cw-list-toolbar"><h2>我的项目 <span>{projects.length}</span></h2><div className="cw-search"><IconSearch size={17} /><input aria-label="搜索项目" placeholder="搜索项目名称或主题" value={filter} onChange={(e) => setFilter(e.target.value)} /></div></div>
      {projects.length === 0 ? <div className="cw-empty cw-first-project"><span className="cw-empty-icon"><IconLayers size={32} /></span><h2>从第一篇内容开始</h2><p>可以从 Obsidian 笔记、已有稿件或一个新主题开始。<br />每一步的结果、确认与修改都会留在同一个项目里。</p><button className="btn btn-primary" onClick={() => setCreateOpen(true)}><IconPlus size={16} />创建项目</button></div> : <div className="cw-project-grid">{projects.filter((item) => `${item.title} ${item.brief?.topic || ''}`.toLowerCase().includes(filter.toLowerCase())).map((item) => { const nodes = item.nodes || []; const focus = nodes.find((step) => ['failed', 'error', 'blocked'].includes(step.status)) || nodes.find((step) => !finished(step)); return <button className="cw-project-card" key={item.id} onClick={() => void openProject(item.id)} disabled={!!busy}><div className="cw-card-top"><span className={`cw-kind cw-kind-${item.kind}`}>{item.kind === 'article' ? <IconFile size={21} /> : <IconVideo size={21} />}</span><span>{item.kind === 'article' ? '文章' : '视频'}</span><StatusBadge status={focus?.status || 'done'} /></div><h3>{item.title}</h3><p>{item.brief?.topic || '尚未填写创作主题'}</p><div className="cw-mini-progress">{nodes.map((step) => <i key={step.id} className={finished(step) && step.status !== 'skipped' ? 'done' : running(step) ? 'running' : ''} title={`${step.title}：${STATUS[step.status]?.label || step.status}`} />)}</div><div className="cw-card-bottom"><span>{focus ? `${focus.title} · ${STATUS[focus.status]?.label || focus.status}` : '流程已完成'}</span><small>{dateLabel(item.updated_at)}</small></div></button>; })}</div>}
    </> : <>
      <div className="cw-project-header"><button className="cw-back" onClick={backToProjects}>‹ 全部项目</button><div className="cw-project-title"><h2>{project.title}</h2><span>{project.kind === 'article' ? '文章项目' : '视频项目'}</span></div><a className="cw-link cw-export-all" href={workflowAllSkillsExportUrl(project.id)} download>导出全部节点 Skill</a><div className="cw-project-progress"><strong>{doneCount}<small> / {requiredCount}</small></strong><span>适用节点已完成</span></div></div>
      {attention && <button className={`cw-attention cw-attention-${STATUS[attention.status]?.tone || 'muted'}`} onClick={() => setNodeId(attention.id)}><span><StatusBadge status={attention.status} /><strong>{attention.title}</strong><span>{attention.message || NODE_HELP[attention.id]}</span></span><span className="cw-attention-go">查看节点 <IconChevron size={16} /></span></button>}
      <section className="cw-flow-panel" aria-label="项目流程图"><div className="cw-flow-caption"><span>完整流程</span><small>点击任意节点，查看产物和执行详情</small></div><div className="cw-flow-viewport"><div className="cw-flow-graph">{orderedNodes.map((step, index) => { const column = index < 5 ? index + 1 : 10 - index; const row = index < 5 ? 1 : 2; return <button key={step.id} className={`cw-flow-node ${step.id === nodeId ? 'selected' : ''} ${index >= 5 ? 'cw-reverse' : ''} ${index === 4 ? 'cw-turn' : ''} ${index === orderedNodes.length - 1 ? 'cw-last' : ''}`} style={{ gridColumn: column, gridRow: row }} aria-current={step.id === nodeId ? 'step' : undefined} onClick={() => setNodeId(step.id)}><div className="cw-node-top"><span className={`cw-node-number ${finished(step) && step.status !== 'skipped' ? 'complete' : ''}`}>{finished(step) && step.status !== 'skipped' ? <IconCheck size={14} /> : String(index + 1).padStart(2, '0')}</span><StatusBadge status={step.status} /></div><strong>{step.title || definitions.find((item) => item.id === step.id)?.title || step.id}</strong><p>{step.message || NODE_HELP[step.id]}</p><div className="cw-node-bottom"><span>{step.id === 'archive' && archiveResult ? '1 份存档' : `${step.artifacts?.length || 0} 个产物`}</span><span>v{step.version ?? 0}</span></div></button>; })}</div></div></section>
      {node && <div className="cw-detail-layout"><section className="cw-node-detail"><div className="cw-detail-heading"><div><span className="cw-eyebrow">STEP {String(NODE_ORDER.indexOf(nodeId) + 1).padStart(2, '0')}</span><h2>{node.title}</h2><p>{NODE_HELP[nodeId]}</p></div><StatusBadge status={node.status} /></div>
        {node.message && <div className={`cw-node-message cw-${STATUS[node.status]?.tone || 'muted'}`}>{node.message}</div>}
        {typeof node.progress === 'number' && <div className="cw-real-progress"><progress max={100} value={Math.max(0, Math.min(100, node.progress))} /><span>{Math.round(node.progress)}%</span></div>}
        <fieldset className="cw-node-fields" disabled={anyRunning || !!busy}>{renderNodeBody()}</fieldset>{nodeId === 'publish' && node.publication_uncertain && <div className="cw-callout"><strong>上次提交结果待确认</strong><p>请先到平台草稿箱或作品列表核对。只有确认没有提交成功，才恢复本次重试。</p><button className="btn" disabled={!!busy || anyRunning} onClick={() => { setReconcileNote(''); setReconcileOpen(true); }}>已在平台核实未提交</button></div>}
        <div className="cw-node-actions"><div>{dirty && <span className="cw-unsaved">有未保存的修改</span>}<button className="btn" disabled={!!busy || !dirty || anyRunning} onClick={() => void perform('save', async () => { await saveDraft(); })}>保存修改</button></div><div>
          {isRunning ? <button className="btn" disabled={!!busy} onClick={() => void perform('stop', async () => acceptProject(await stopWorkflowNode(project.id, nodeId)))}><IconStop size={14} />停止此任务</button> : nodeId !== 'archive' && <>
            {nodeId === 'publish' ? <><button className="btn" disabled={!!busy || anyRunning} onClick={() => void executeNode({ action: 'run' })}>准备本地发布包</button><button className="btn" disabled={!!busy || anyRunning || !canPublishPublicly || (node.publication_uncertain || ['unknown', 'unverified', 'result_unknown'].includes(node.status))} onClick={() => setPublishConfirm(true)}>公开发布…</button><button className="btn btn-primary" disabled={!!busy || anyRunning || !canSavePlatformDraft || (node.publication_uncertain || ['unknown', 'unverified', 'result_unknown'].includes(node.status))} onClick={() => void executeNode({ action: 'draft' })}>保存平台草稿</button></> : <><button className="btn" disabled={!!busy || anyRunning || node.status !== 'awaiting_review' || dirty} onClick={() => void perform('approve', async () => { const saved = await saveDraft(); const current = saved.nodes.find((item) => item.id === nodeId); acceptProject(await approveWorkflowNode(saved.id, nodeId, current?.version || node.version)); setNotice('当前版本已确认'); })}><IconCheck size={15} />确认当前版本</button>{nodeId === 'deliver' && <button className="btn" disabled={!!busy || anyRunning || !asText(settings.final_path).trim()} onClick={() => void executeNode({ action: 'import' })}>导入并验证已有成片</button>}{nodeId === 'script' && <button className="btn" disabled={!!busy || anyRunning} onClick={() => void executeNode({ action: 'run', mode: 'generate' })}>依据材料生成新稿</button>}<button className="btn btn-primary" disabled={!!busy || anyRunning || node.status === 'skipped' || (nodeId === 'script' && !activeManuscript?.content.trim())} onClick={() => void executeNode({ action: ['failed', 'error'].includes(node.status) ? 'retry' : 'run' })}>{busy === 'run' ? '提交中…' : nodeId === 'script' ? '采用当前主稿' : nodeId === 'source' ? '检查并导入素材' : nodeId === 'brief' ? '整理创作简报' : nodeId === 'deliver' ? '渲染成片' : finished(node) ? '重新执行本步' : '执行本步'}</button></>}
          </>}
        </div></div>
        <section className="cw-section"><div className="cw-section-heading"><h3>本步产物</h3><span className="cw-counter">{nodeId === 'archive' && archiveResult ? '1 份存档' : node.artifacts?.length || 0}</span></div>{node.artifacts?.length ? <div className="cw-artifacts">{node.artifacts.map((item, index) => <Artifact artifact={item} key={`${item.path}-${index}`} />)}</div> : <p className="cw-hint">{nodeId === 'archive' && archiveResult ? '存档文件路径与正文见上方已保存的存档结果。' : '这一步还没有产物。运行完成后会在这里展示。'}</p>}</section>
        <section className="cw-section"><div className="cw-section-heading"><h3>修改意见</h3><span className="cw-hint">仅针对当前项目，可回到上游节点修改</span></div><Field label="由哪个节点处理这条修改"><select aria-label="由哪个节点处理这条修改" value={feedbackTarget} onChange={(e) => setFeedbackTarget(e.target.value as WorkflowNodeId)} disabled={anyRunning || !!busy}>{Array.from(new Set<WorkflowNodeId>([nodeId, 'script', 'transcript', 'storyboard', 'build', 'review', 'publish'])).filter((id) => NODE_ORDER.indexOf(id) <= NODE_ORDER.indexOf(nodeId) && project.nodes.find((item) => item.id === id)?.status !== 'skipped').map((id) => <option key={id} value={id}>{project.nodes.find((item) => item.id === id)?.title || id}</option>)}</select></Field><textarea style={{ marginTop: 10 }} rows={3} aria-label="节点修改意见" value={feedback} onChange={(e) => setFeedback(e.target.value)} placeholder="例如：00:32 的字幕太小，请加大字号；保留原来的画面节奏。" /><div className="cw-feedback-action"><button className="btn" disabled={!!busy || !feedback.trim() || anyRunning} onClick={() => void perform('feedback', async () => { await saveDraft(); acceptProject(await sendWorkflowFeedback(project.id, nodeId, feedback.trim(), feedbackTarget)); setFeedback(''); setNotice(`已记录到“${project.nodes.find((item) => item.id === feedbackTarget)?.title || feedbackTarget}”，请回该节点重新执行。`); })}>记录修改意见</button></div>{node.feedback?.length > 0 && <div className="cw-feedback-list">{node.feedback.slice().reverse().map((item, index) => <div key={`${item.created_at}-${index}`}><p>{item.text}</p><small>{dateLabel(item.created_at || item.at)}</small></div>)}</div>}</section>
      </section><aside className="cw-detail-aside"><section className="cw-side-card"><div className="cw-section-heading"><h3><IconSkills size={17} />节点经验</h3><span className="cw-version">{skillVersion(node.current_skill_version || skills?.version || node.skill_version)}</span></div><p>把反复修改得出的要求，沉淀为这个节点下次可用的标准。</p>{node.skill_error && <div className="cw-alert cw-alert-error" role="alert"><span>个人标准无法读取：{node.skill_error}。已有稿件和产物保留，修复标准后可继续。</span></div>}{node.skill_updated && <div className="cw-callout">标准已更新。当前产物使用 {skillVersion(node.skill_version)}，下次执行将使用新标准。</div>}<button className="btn" disabled={!!busy} onClick={() => void loadSkills()}>查看当前标准</button><button className="btn btn-primary" disabled={!!busy} onClick={() => { setLearnOpen(true); setProposal(null); }}>沉淀这次经验</button><a className="cw-link" href={workflowSkillExportUrl(project.id, nodeId)} download>导出节点 Skill 与版本</a>{skillsOpen && <div className="cw-skill-content"><h4>当前标准</h4>{skills?.skills?.map((item, index) => typeof item === 'string' ? <p key={index}>{item}</p> : <details key={item.name || index}><summary>{item.name || item.path || 'Skill'}</summary><pre>{item.content || item.body || item.path}</pre></details>)}{(skills?.content || skills?.body) && <pre>{skills.content || skills.body}</pre>}{!skills?.content && !skills?.body && !skills?.skills?.length && <pre>{JSON.stringify(skills, null, 2)}</pre>}</div>}</section>
        <section className="cw-side-card"><h3>执行记录</h3><div className="cw-metadata"><span>节点版本</span><strong>v{node.version ?? 0}</strong><span>已确认版本</span><strong>{nodeId === 'archive' && node.status === 'completed' && archiveResult ? '已确认存档' : node.approved_version ? `v${node.approved_version}` : '尚未确认'}</strong><span>最近更新</span><strong>{dateLabel(node.updated_at || project.updated_at)}</strong></div>{node.runs?.length ? <div className="cw-runs">{node.runs.slice().reverse().map((run, index) => <details key={run.id || index}><summary><StatusBadge status={run.status || 'pending'} /><small>{dateLabel(run.started_at)}</small></summary>{run.message && <p>{run.message}</p>}{run.log && <pre>{run.log}</pre>}{run.finished_at && <small>结束于 {dateLabel(run.finished_at)}</small>}</details>)}</div> : <p className="cw-hint">{nodeId === 'archive' && archiveResult ? '已完成直接归档，记录见本步存档结果' : '尚未执行此节点'}</p>}</section>
        <section className="cw-side-card cw-note-card"><IconFolder size={19} /><h3>所有内容留在项目里</h3><p>切换页面不会终止后台任务。重新打开项目可以继续查看节点状态和已有产物。</p></section>
      </aside></div>}
    </>}
    {reconcileOpen && project && <div className="cw-modal-backdrop"><section className="cw-modal" role="dialog" aria-modal="true" aria-labelledby="cw-reconcile-title"><div className="cw-section-heading"><h2 id="cw-reconcile-title">登记人工核实结果</h2><button className="cw-close" aria-label="关闭" disabled={!!busy} onClick={() => setReconcileOpen(false)}>×</button></div><p>仅在已核对平台、确认草稿或作品没有提交成功时使用。此操作恢复重试，不会标记发布成功，也不会立即重发。</p><Field label="核实记录（至少 5 字）"><textarea rows={4} value={reconcileNote} onChange={(e) => setReconcileNote(e.target.value)} placeholder="写明核对的平台、草稿箱或作品列表，以及核实结果。" /></Field><button className="btn btn-primary cw-full" disabled={!!busy || reconcileNote.trim().length < 5} onClick={() => void perform('reconcile', async () => { acceptProject(await reconcileWorkflowPublish(project.id, reconcileNote.trim()), false); setReconcileOpen(false); setNotice('已登记未提交结果，现可重新保存草稿或发布。'); })}>确认未提交，恢复重试</button></section></div>}
    {createOpen && <div className="cw-modal-backdrop" role="presentation" onClick={(e) => { if (e.target === e.currentTarget && !busy) setCreateOpen(false); }}><section className="cw-modal" role="dialog" aria-modal="true" aria-labelledby="cw-create-title"><div className="cw-section-heading"><h2 id="cw-create-title">新建内容项目</h2><button className="cw-close" aria-label="关闭" disabled={!!busy} onClick={() => setCreateOpen(false)}>×</button></div><p>一篇新的内容，创建一个独立项目。稿件版本与参考资料可以放在同一个项目中。</p><div className="cw-kind-picker"><button className={newKind === 'video' ? 'selected' : ''} onClick={() => setNewKind('video')}><IconVideo size={25} /><strong>视频</strong><span>口播稿 → 原片 → 成片</span></button><button className={newKind === 'article' ? 'selected' : ''} onClick={() => setNewKind('article')}><IconFile size={25} /><strong>文章</strong><span>资料 → 写作 → 发布</span></button></div><Field label="项目名称"><input autoFocus value={newTitle} onChange={(e) => setNewTitle(e.target.value)} placeholder="为这篇内容起个名字" /></Field><button className="btn btn-primary cw-full" disabled={!!busy || !newTitle.trim()} onClick={() => void perform('create', async () => { if (dirtyRef.current) await saveDraft(); const value = await createWorkflow({ title: newTitle.trim(), kind: newKind }); selectedRef.current = value.id; acceptProject(value); updateWorkflowLocation(value.id); setNodeId('brief'); setManuscriptId(''); setCreateOpen(false); setNewTitle(''); })}>{busy === 'create' ? '创建中…' : '创建并进入工作流'}</button></section></div>}
    {learnOpen && project && node && <div className="cw-modal-backdrop"><section className="cw-modal cw-learning-modal" role="dialog" aria-modal="true" aria-labelledby="cw-learning-title"><div className="cw-section-heading"><div><span className="cw-eyebrow">{node.title}</span><h2 id="cw-learning-title">沉淀节点经验</h2></div><button className="cw-close" aria-label="关闭" disabled={!!busy} onClick={() => setLearnOpen(false)}>×</button></div><p>先查看标准的前后差异，确认后再应用到这个节点。</p><Field label="以后这个节点应该怎样做"><textarea rows={4} value={instruction} onChange={(e) => { setInstruction(e.target.value); setProposal(null); }} placeholder="写成可复用的具体要求，例如：口播稿先用一个真实场景开场；字幕每行不超过18字，避免遮挡人物。" /></Field><button className="btn" disabled={!!busy || !instruction.trim()} onClick={() => void perform('learn-preview', async () => { const current = await fetchWorkflowSkills(project.id, nodeId); setSkills(current); setProposal(await previewWorkflowLearning(project.id, nodeId, instruction.trim(), current.version || node.skill_version)); })}>{busy === 'learn-preview' ? '生成差异中…' : '预览标准差异'}</button>{proposal && <><div className="cw-callout">确认后更新节点标准，供后续项目使用。<br /><code>{proposal.target_path}</code></div><div className="cw-diff-columns"><section><h3>应用前 · {skillVersion(proposal.base_version)}</h3><pre>{proposal.before || '当前还没有补充标准'}</pre></section><section><h3>应用后</h3><pre>{proposal.after}</pre></section></div><details className="cw-unified-diff"><summary>逐行差异</summary><pre>{Array.isArray(proposal.diff) ? proposal.diff.join('\n') : proposal.diff}</pre></details><button className="btn btn-primary" disabled={!!busy || anyRunning} onClick={() => void perform('learn-apply', async () => { await applyWorkflowLearning(project.id, nodeId, proposal.proposal_id); acceptProject(await fetchWorkflow(project.id), false); setSkills(await fetchWorkflowSkills(project.id, nodeId)); setProposal(null); setLearnOpen(false); setSkillsOpen(true); setNotice('节点标准已更新，已保留版本记录，可随时导出。'); })}>确认应用这份差异</button></>}</section></div>}
    {publishConfirm && project && <div className="cw-modal-backdrop"><section className="cw-modal" role="dialog" aria-modal="true" aria-labelledby="cw-publish-title"><div className="cw-section-heading"><h2 id="cw-publish-title">确认公开发布</h2><button className="cw-close" aria-label="关闭" onClick={() => setPublishConfirm(false)}>×</button></div><p>本次会将内容公开发布到所选平台。</p><dl className="cw-publish-summary"><dt>平台</dt><dd>{asText(settings.publish_platform) || 'weixin-channels'}</dd><dt>标题</dt><dd>{asText(settings.publish_title) || project.title}</dd><dt>成片</dt><dd>{asText(settings.final_path) || '采用当前项目成片'}</dd></dl><div className="cw-toolbar"><button className="btn" onClick={() => setPublishConfirm(false)}>返回检查</button><button className="btn btn-primary" disabled={!!busy} onClick={() => { setPublishConfirm(false); void executeNode({ action: 'publish', confirm: true }); }}>确认公开发布</button></div></section></div>}
  </div>;
}
