import { useEffect, useMemo, useRef, useState } from 'react';
import {
  applyWorkflowLearning, fetchWorkflowLibrarySkill, fetchWorkflowSkills, previewWorkflowLearning,
} from '../lib/contentWorkflowApi';
import type {
  LearningProposal, WorkflowLearningScope, WorkflowLibrarySource, WorkflowNodeId, WorkflowSkills, WorkflowSkillUse,
} from '../lib/contentWorkflowApi';
import type { WorkflowDocument } from './WorkflowDocumentPreview';

interface Props {
  projectId: string; nodeId: WorkflowNodeId; nodeTitle: string; running: boolean;
  initialInstruction?: string; preferredSkill?: WorkflowSkillUse;
  onClose: () => void; onRead: (document: WorkflowDocument) => void;
  onApplied: (scope: WorkflowLearningScope, name: string) => Promise<void>;
}

export default function WorkflowLearningDialog({ projectId, nodeId, nodeTitle, running, initialInstruction = '', preferredSkill, onClose, onRead, onApplied }: Props) {
  const [data, setData] = useState<WorkflowSkills | null>(null);
  const [scope, setScope] = useState<WorkflowLearningScope>('library');
  const [skillName, setSkillName] = useState('');
  const [relativePath, setRelativePath] = useState('SKILL.md');
  const [references, setReferences] = useState<string[]>([]);
  const [source, setSource] = useState<WorkflowLibrarySource | null>(null);
  const [instruction, setInstruction] = useState(initialInstruction);
  const [proposal, setProposal] = useState<LearningProposal | null>(null);
  const [busy, setBusy] = useState('loading');
  const [sourceLoading, setSourceLoading] = useState(false);
  const [error, setError] = useState('');
  const sending = useRef(false);
  const dialog = useRef<HTMLElement>(null);
  const library = data?.library || [];
  const version = scope === 'library' ? source?.version || source?.sha256 : data?.version;
  const targetPath = scope === 'library' ? source?.target_path : data?.target_path;
  const content = scope === 'library' ? source?.content : data?.content || data?.body;
  const used = useMemo(() => data?.used_skills || [], [data?.used_skills]);
  const usedHere = used.some((item) => item.name === skillName && item.path === relativePath);

  useEffect(() => {
    let live = true;
    void fetchWorkflowSkills(projectId, nodeId).then((value) => {
      if (!live) return;
      setData(value);
      const available = value.library || [];
      const valid = (item?: WorkflowSkillUse) => item && available.some((skill) => skill.name === item.name);
      const preferred = valid(preferredSkill) ? preferredSkill : (value.used_skills || []).slice().reverse().find((item) => valid(item));
      if (preferred) { setSkillName(preferred.name); setRelativePath(preferred.path || 'SKILL.md'); }
      else if (available.length) { setSkillName(available[0].name); setRelativePath('SKILL.md'); }
      else setScope('node');
    }).catch((reason) => { if (live) setError(reason instanceof Error ? reason.message : '读取 Skill 目录失败'); }).finally(() => { if (live) setBusy(''); });
    return () => { live = false; };
  }, [projectId, nodeId, preferredSkill]);

  useEffect(() => {
    if (scope !== 'library' || !skillName) { setSource(null); setSourceLoading(false); return; }
    let live = true;
    setSourceLoading(true); setSource(null); setError('');
    void Promise.all([
      fetchWorkflowLibrarySkill(projectId, nodeId, skillName, relativePath),
      relativePath === 'SKILL.md' ? Promise.resolve(null) : fetchWorkflowLibrarySkill(projectId, nodeId, skillName),
    ]).then(([value, main]) => {
      if (!live) return;
      setSource(value);
      setReferences(Array.from(new Set(['SKILL.md', relativePath, ...(main?.references || []), ...(value.references || []), ...used.filter((item) => item.name === skillName).map((item) => item.path)])));
    }).catch((reason) => { if (live) setError(reason instanceof Error ? reason.message : '读取真实源文件失败'); }).finally(() => { if (live) setSourceLoading(false); });
    return () => { live = false; };
  }, [projectId, nodeId, scope, skillName, relativePath, used]);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.focus();
    return () => { if (previous?.isConnected) previous.focus(); };
  }, []);

  async function operate(label: string, action: () => Promise<void>) {
    if (sending.current) return;
    sending.current = true; setBusy(label); setError('');
    try { await action(); } catch (reason) { setError(reason instanceof Error ? reason.message : '操作失败，请重新预览后再试。'); }
    finally { sending.current = false; setBusy(''); }
  }
  const fileChoices = Array.from(new Set(['SKILL.md', relativePath, ...references, ...used.filter((item) => item.name === skillName).map((item) => item.path)]));
  return <div className="cw-modal-backdrop"><section className="cw-modal cw-learning-modal" role="dialog" aria-modal="true" aria-labelledby="cw-learning-title" ref={dialog} tabIndex={-1}>
    <div className="cw-section-heading"><div><span className="cw-eyebrow">{nodeTitle}</span><h2 id="cw-learning-title">沉淀经验到 Skill</h2></div><button className="cw-close" aria-label="关闭经验沉淀" disabled={!!busy} onClick={onClose}>×</button></div>
    <p>选择要改进的真实来源，让助手整理成可复用的方法。先看前后差异，再确认写入。</p>
    {error && <div className="cw-alert cw-alert-error" role="alert">{error}</div>}
    <fieldset className="cw-node-fields" disabled={!!busy}>
      <label className="cw-field"><span>沉淀目标</span><select aria-label="沉淀目标" value={scope} onChange={(event) => { setScope(event.target.value as WorkflowLearningScope); setProposal(null); setError(''); }}><option value="library" disabled={!library.length}>Easel 原有 Skill（真实源文件）</option><option value="node">本节点补充标准</option></select></label>
      {scope === 'library' ? <div className="cw-learning-target-grid"><label className="cw-field"><span>原有 Skill</span><select aria-label="原有 Skill" value={skillName} onChange={(event) => { setSkillName(event.target.value); setRelativePath('SKILL.md'); setReferences([]); setProposal(null); }}>{library.map((item) => <option key={item.id || item.name} value={item.name}>{item.name}{used.some((value) => value.name === item.name) ? ' · 实际使用过' : ''}</option>)}</select></label><label className="cw-field"><span>正文或引用文件</span><select aria-label="正文或引用文件" value={relativePath} onChange={(event) => { setRelativePath(event.target.value); setProposal(null); }}>{fileChoices.map((path) => <option value={path} key={path}>{path}</option>)}</select></label></div> : <p className="cw-hint">本节点补充标准用于当前工作流的额外要求。要改进原有 Skill，请选择上面的真实源文件。</p>}
      {scope === 'library' && <p className="cw-hint">{usedHere ? '这份文件已在本节点实际读取。' : '这是本节点适用库中的可选来源。'}{library.find((item) => item.name === skillName)?.description}</p>}
      <div className="cw-learning-source"><span>{sourceLoading || busy === 'loading' ? '正在读取真实来源…' : version ? `当前版本 ${version.replace(/^sha256:/, '').slice(0, 8)}` : '尚未读到可修改的来源'}</span>{targetPath && <code>{targetPath}</code>}{content !== undefined && <button type="button" className="cw-link cw-read-button" onClick={() => onRead({ title: scope === 'library' ? `${skillName} · ${relativePath}` : `${nodeTitle} · 节点补充标准`, path: targetPath, content, format: 'markdown' })}>阅读应用前正文</button>}{source?.truncated && <p className="cw-hint">当前正文仅为节选。差异预览必须由后端基于完整源文件生成。</p>}</div>
      <label className="cw-field"><span>这次经验要怎样改进方法</span><textarea rows={4} aria-label="这次经验要怎样改进方法" value={instruction} onChange={(event) => { setInstruction(event.target.value); setProposal(null); }} placeholder="例如：去 AI 感改写时，先检查公式化开头，保留真实的现场细节。把这条方法整合进现有检查步骤。" /></label>
    </fieldset>
    <button className="btn" disabled={!!busy || sourceLoading || !instruction.trim() || !version} onClick={() => void operate('preview', async () => { setProposal(await previewWorkflowLearning(projectId, nodeId, instruction.trim(), version, { scope, ...(scope === 'library' ? { skill_name: skillName, relative_path: relativePath } : {}) })); })}>{busy === 'preview' ? '正在整理经验并生成差异…' : '预览方法改进差异'}</button>
    {proposal && <><div className="cw-callout"><strong>{proposal.scope === 'library' || scope === 'library' ? '确认后将直接更新这份原有 Skill 文件，后续调用会使用新方法。' : '确认后将更新本节点的补充标准。'}</strong><br /><code>{proposal.target_path}</code></div><div className="cw-diff-columns"><section><h3>应用前 · {proposal.base_version.replace(/^sha256:/, '').slice(0, 8)}</h3><pre>{proposal.before || '（空文件）'}</pre></section><section><h3>应用后</h3><pre>{proposal.after}</pre></section></div><details className="cw-unified-diff"><summary>逐行差异</summary><pre>{Array.isArray(proposal.diff) ? proposal.diff.join('\n') : proposal.diff}</pre></details><button className="btn btn-primary" disabled={!!busy || running} onClick={() => void operate('apply', async () => { const targetScope = proposal.scope || scope; await applyWorkflowLearning(projectId, nodeId, proposal.proposal_id, targetScope); await onApplied(targetScope, proposal.skill_name || skillName); })}>{busy === 'apply' ? '正在应用…' : scope === 'library' ? '确认改进这份原有 Skill' : '确认更新节点补充标准'}</button></>}
  </section></div>;
}
