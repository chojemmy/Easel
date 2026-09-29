/** The content workflow has its own durable projects; chat sessions are not jobs. */
export type WorkflowKind = 'video' | 'article';
export type WorkflowNodeId = 'brief' | 'script' | 'source' | 'transcript' | 'storyboard' | 'build' | 'review' | 'deliver' | 'publish' | 'archive';
export interface WorkflowArtifact { name: string; path: string; url?: string; kind?: string; }
export interface WorkflowRun { id?: string; status?: string; message?: string; started_at?: string; finished_at?: string; log?: string; }
export interface WorkflowFeedback { text: string; created_at?: string; at?: string; }
export interface WorkflowNode {
  id: WorkflowNodeId; title: string; status: string; message?: string; version: number;
  approved_version?: number; artifacts: WorkflowArtifact[]; runs: WorkflowRun[];
  feedback: WorkflowFeedback[]; skill_version?: string; current_skill_version?: string; skill_updated?: boolean; skill_error?: string;
  publication_uncertain?: boolean; progress?: number; updated_at?: string;
}
export interface WorkflowManuscript {
  id: string; title: string; content: string; source_kind: 'manual' | 'obsidian' | 'generated' | 'file';
  source_path?: string; version: number;
}
export interface WorkflowBrief { topic?: string; audience?: string; platform?: string; duration?: string | number; style?: string; requirements?: string; }
export interface WorkflowProject {
  id: string; title: string; kind: WorkflowKind; created_at: string; updated_at: string; content_version: number;
  brief: WorkflowBrief; manuscripts: WorkflowManuscript[]; primary_manuscript_id?: string;
  media: { source_path?: string; transcript_path?: string; [key: string]: unknown };
  nodes: WorkflowNode[]; settings: Record<string, unknown>; archive?: Record<string, unknown>;
}
export interface WorkflowDefinition { id: WorkflowNodeId; title: string; description?: string; skills?: string[]; }
export interface WorkflowIndex { projects: WorkflowProject[]; nodes: WorkflowDefinition[]; defaults: { vault?: string; output_dir?: string; [key: string]: unknown }; }
export interface WorkflowSkill { name?: string; path?: string; content?: string; body?: string; version?: string; }
export interface WorkflowSkills { version?: string; content?: string; body?: string; target_path?: string; skills?: (WorkflowSkill | string)[]; [key: string]: unknown; }
export interface LearningProposal { proposal_id: string; before: string; after: string; diff: string | string[]; base_version: string; target_path?: string; after_version?: string; [key: string]: unknown; }
export interface ArchivePreview { status?: string; target_path?: string; path?: string; content?: string; markdown?: string; hash?: string; expected_hash?: string; targets?: { path: string; action: string; before_hash?: string; after_hash?: string }[]; plan?: { note_path?: string; primary_manuscript_id?: string; status?: string }; files?: unknown[]; [key: string]: unknown; }
export interface ObsidianNote { title: string; path: string; excerpt?: string; }

const base = window.location.pathname.replace(/\/index\.html$/, '').replace(/\/$/, '');
const root = '/api/content-workflows';
const projectUrl = (id: string) => `${root}/${encodeURIComponent(id)}`;
const nodeUrl = (id: string, node: WorkflowNodeId) => `${projectUrl(id)}/nodes/${node}`;

async function request<T>(url: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(`${base}${url}`, {
    method, cache: 'no-store', headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail || data.message || data.error;
    throw new Error(typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `请求失败（${response.status}）`);
  }
  return data as T;
}

const unpack = (data: WorkflowProject | { project: WorkflowProject }) => 'project' in data ? data.project : data;
export const fetchWorkflows = () => request<WorkflowIndex>(root);
export const fetchWorkflow = async (id: string) => unpack(await request<WorkflowProject | { project: WorkflowProject }>(projectUrl(id)));
export const createWorkflow = async (data: Partial<WorkflowProject>) => unpack(await request<WorkflowProject | { project: WorkflowProject }>(root, 'POST', data));
export const updateWorkflow = async (id: string, data: Partial<WorkflowProject>) => unpack(await request<WorkflowProject | { project: WorkflowProject }>(projectUrl(id), 'PATCH', data));
export const runWorkflowNode = async (id: string, node: WorkflowNodeId, data: Record<string, unknown> = {}) => unpack(await request<WorkflowProject | { project: WorkflowProject }>(`${nodeUrl(id, node)}/run`, 'POST', data));
export const approveWorkflowNode = async (id: string, node: WorkflowNodeId, version: number) => unpack(await request<WorkflowProject | { project: WorkflowProject }>(`${nodeUrl(id, node)}/approve`, 'POST', { version }));
export const sendWorkflowFeedback = async (id: string, node: WorkflowNodeId, text: string, target_node?: WorkflowNodeId) => unpack(await request<WorkflowProject | { project: WorkflowProject }>(`${nodeUrl(id, node)}/feedback`, 'POST', { text, target_node }));
export const stopWorkflowNode = async (id: string, node: WorkflowNodeId) => unpack(await request<WorkflowProject | { project: WorkflowProject }>(`${nodeUrl(id, node)}/stop`, 'POST', {}));
export const reconcileWorkflowPublish = async (id: string, note: string) => unpack(await request<WorkflowProject | { project: WorkflowProject }>(`${nodeUrl(id, 'publish')}/reconcile`, 'POST', { outcome: 'not_submitted', confirm: true, note }));
export const fetchWorkflowSkills = (id: string, node: WorkflowNodeId) => request<WorkflowSkills>(`${nodeUrl(id, node)}/skills`);
export const previewWorkflowLearning = (id: string, node: WorkflowNodeId, instruction: string, expected_version?: string) => request<LearningProposal>(`${nodeUrl(id, node)}/learn/preview`, 'POST', { instruction, scope: 'node', expected_version });
export const applyWorkflowLearning = (id: string, node: WorkflowNodeId, proposal_id: string) => request<Record<string, unknown>>(`${nodeUrl(id, node)}/learn/apply`, 'POST', { proposal_id });
export const workflowSkillExportUrl = (id: string, node: WorkflowNodeId) => `${base}${nodeUrl(id, node)}/skills/export`;
export const workflowAllSkillsExportUrl = (id: string) => `${base}${projectUrl(id)}/skills/export`;
export const previewWorkflowArchive = (id: string) => request<ArchivePreview>(`${projectUrl(id)}/archive/preview`);
export const archiveWorkflow = async (id: string, expected_hash?: string) => unpack(await request<WorkflowProject | { project: WorkflowProject }>(`${projectUrl(id)}/archive`, 'POST', { confirm: true, expected_hash }));
export const importWorkflowManuscript = async (id: string, source_path: string) => unpack(await request<WorkflowProject | { project: WorkflowProject }>(`${projectUrl(id)}/manuscripts/import`, 'POST', { source_kind: 'obsidian', source_path }));
export const searchWorkflowNotes = (query: string) => request<{ notes: ObsidianNote[] }>(`${root}/obsidian/search?q=${encodeURIComponent(query)}`);

export function workflowArtifactUrl(artifact: WorkflowArtifact): string | undefined {
  // Only server-supplied, same-origin URLs are opened in the application.
  if (!artifact.url) return undefined;
  try {
    const target = artifact.url.startsWith('/api/') ? `${base}${artifact.url}` : artifact.url;
    const url = new URL(target, window.location.origin);
    return url.origin === window.location.origin && ['http:', 'https:'].includes(url.protocol) ? url.href : undefined;
  } catch { return undefined; }
}
