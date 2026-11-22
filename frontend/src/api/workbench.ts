export type Session = { csrf_token: string };

export type ProjectInput = {
  name?: string;
  source_bvid?: string | null;
  target_bvid: string;
  source_page?: number | null;
  target_page: number;
};

export type Project = ProjectInput & {
  id: string;
  creation_order: number;
  name: string;
  completed: boolean;
  send_needs_review: boolean;
  send_partial_failure: boolean;
  activity: { stage: TaskKind | "review"; running: boolean; active: boolean };
  send_queue: { position: number; total: number } | null;
  publication_status?: keyof typeof publicationLabels | null;
  preload_publish_at?: number | null;
};

export const publicationLabels = {
  waiting: "正在等待稿件公开",
  checking: "核对计划公开时间",
  blocked: "等待进一步操作",
};

export type TaskState =
  | "queued" | "running" | "backoff" | "pausing" | "paused" | "cancelling" | "cancelled" | "succeeded" | "failed" | "needs_review";
export type TaskKind = "download" | "classify" | "send";

export type SendAccountProgress = {
  state: "ready" | "sending" | "waiting" | "finished";
  next_attempt_at: string | null;
  error: Task["error"];
};

export type Task = {
  id: string;
  project_id: string;
  creation_order: number;
  kind: TaskKind;
  state: TaskState;
  version: number;
  progress: { done: number; total: number | null; failed: number; skipped: number; unknown: number; withdrawn: number };
  checkpoint: {
    automatic_retry_started?: boolean;
    completed_batches?: number; run_id?: string; batch_id?: string; started?: boolean;
    next_send_task_id?: string | null; waiting_after_priority?: boolean; primary_only?: boolean; secondary_upgrade_id?: string | null; analysis_skipped?: boolean;
    share_count?: number;
    publication?: "pending" | "dispatch" | "returned" | null;
    publication_first_pending?: boolean;
    publication_retry_at?: string | null;
    accounts?: Partial<Record<BilibiliAccountRole, SendAccountProgress>>;
  };
  next_attempt_at: string | null;
  error: { code: string; message: string; retryable: boolean; retries_exhausted?: boolean } | null;
};

export type Page<T> = { items: T[]; next_cursor: string | null };

export class ApiError extends Error {
  constructor(readonly status: number, message: string, readonly code?: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, init);
  if (!response.ok) {
    const body = await response.json().catch(() => undefined);
    throw new ApiError(response.status, body?.error?.message ?? "请求失败", body?.error?.code);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

function write(method: string, body?: object, csrf?: string, key?: string): RequestInit {
  const headers = new Headers();
  if (body) headers.set("Content-Type", "application/json");
  if (csrf) headers.set("X-CSRF-Token", csrf);
  if (key) headers.set("Idempotency-Key", key);
  return { method, headers, body: body ? JSON.stringify(body) : undefined };
}

function withCursor(path: string, cursor?: string): string {
  return cursor ? `${path}?cursor=${encodeURIComponent(cursor)}` : path;
}

export function openLocalSession(signal?: AbortSignal) {
  const connect = () => request<Session>("/session", {
    method: "POST", headers: { "X-Danmaku-Local": "1" }, signal,
  });
  return navigator.locks ? navigator.locks.request("danmaku-session", { signal }, connect) : connect();
}

export const listProjects = (cursor?: string, signal?: AbortSignal) =>
  request<Page<Project>>(withCursor("/projects", cursor), { signal });
export type PreloadPart = { part: VideoPart; publish_at: number; already_created: boolean };
export const listPreloadParts = () => request<PreloadPart[]>("/preload/parts", { cache: "no-store" });
export const createPreloadProject = (part: VideoPart, csrf: string) =>
  request<Project>("/preload/projects", write("POST", { bvid: part.bvid, page: part.page }, csrf));
export const getOverduePublication = (signal?: AbortSignal) =>
  request<string | null>("/preload/overdue", { signal, cache: "no-store" });
export const decideOverduePublication = (taskId: string, send: boolean, csrf: string) =>
  request<void>("/preload/overdue", write("POST", { task_id: taskId, send }, csrf));
export function createProject(
  project: ProjectInput, csrf: string, source?: DownloadMode, confirmDurationMismatch = false,
): Promise<Project> {
  const params = new URLSearchParams();
  if (source) params.set("source", source);
  if (confirmDurationMismatch) params.set("confirm_duration_mismatch", "true");
  const query = params.toString();
  return request<Project>(`/projects${query ? `?${query}` : ""}`, write("POST", project, csrf));
}
export const renameProject = (projectId: string, name: string, csrf: string) =>
  request<Project>(`/projects/${projectId}`, write("PATCH", { name }, csrf));

export const deleteProject = (projectId: string, csrf: string) =>
  request<void>(`/projects/${projectId}`, write("DELETE", undefined, csrf));

export const setProjectCompletion = (projectId: string, completed: boolean, csrf: string) =>
  request<Project>(`/projects/${projectId}/completion`, write("PATCH", { completed }, csrf));

export function listTasks(projectId: string, cursor?: string, kind?: TaskKind, signal?: AbortSignal): Promise<Page<Task>> {
  const params = new URLSearchParams();
  if (cursor) params.set("cursor", cursor);
  if (kind) params.set("kind", kind);
  const query = params.toString();
  return request<Page<Task>>(`/projects/${projectId}/tasks${query ? `?${query}` : ""}`, { signal });
}
export type DownloadSource = { source_bvid: string; source_page: number } | { source_cid: string };
export const createTask = (
  projectId: string, key: string, csrf: string, mode: DownloadMode = "mock",
  source?: DownloadSource,
) =>
  request<Task>(`/projects/${projectId}/tasks`, write("POST",
    mode === "mock" ? { kind: "download", ...source } : { kind: "download", mode, ...source }, csrf, key));

export const deleteTask = (taskId: string, csrf: string) =>
  request<void>(`/tasks/${taskId}`, write("DELETE", undefined, csrf));

export type ModelProvider = "mock" | "openai_compatible";
export const DEEPSEEK_MODEL = "deepseek-flash";
export const DEEPSEEK_MODEL_LABEL = "DeepSeek V4.1 Flash";
export const DEEPSEEK_ENDPOINT = "https://api.deepseek.com";
export type ReasoningEffort = "low" | "high" | "max";
export type ModelConfig = {
  provider: ModelProvider; base_url: string | null; model: string; version: number;
  thinking_enabled: boolean; reasoning_effort: ReasoningEffort; extra_requirements: string;
};
export type ModelSettings = ModelConfig & { configured: boolean; can_set_reasoning_effort: boolean };
export type ModelInput = Pick<ModelConfig, "provider" | "base_url" | "model">
  & Partial<Pick<ModelConfig, "thinking_enabled" | "reasoning_effort" | "extra_requirements">>
  & { api_key?: string; remove_api_key?: boolean };
export type AnalysisInput = {
  snapshot_id: string; subtitle_version_id?: string | null; japanese_subtitle_version_id?: string | null;
} & ({ skip_analysis?: false; llm_config_version: number; reasoning_effort?: ReasoningEffort | null }
  | { skip_analysis: true; llm_config_version?: never; reasoning_effort?: never });
export type AnalysisRun = {
  name: string;
  id: string; project_id: string; snapshot_id: string; subtitle_version_id: string | null;
  japanese_subtitle_version_id: string | null;
  config: ModelConfig | null; prompt_version: string | null;
  strategy: "full" | "segmented" | "bilingual" | "single_subtitle" | "skipped" | null;
  state: TaskState;
};
export type Classification = {
  id: string; category: "translation" | "other" | null; reason: string | null;
  cue_ids: string[]; diagnostic: string | null;
};

export const getModelSettings = (signal?: AbortSignal) =>
  request<ModelSettings>("/settings/llm", { signal });
export const saveModelSettings = (input: ModelInput, csrf: string) =>
  request<ModelSettings>("/settings/llm", write("PUT", input, csrf));
export type SendTiming = {
  item_min_seconds: number; item_max_seconds: number; rate_limit_seconds: number;
};
export const getSendTiming = (signal?: AbortSignal) =>
  request<SendTiming>("/settings/sending", { signal });
export const saveSendTiming = (input: SendTiming, csrf: string) =>
  request<SendTiming>("/settings/sending", write("PUT", input, csrf));
export const createAnalysis = (projectId: string, key: string, csrf: string, input: AnalysisInput) =>
  request<Task>(`/projects/${projectId}/tasks`, write("POST", { kind: "classify", ...input }, csrf, key));
export const listAnalysisRuns = (projectId: string, cursor?: string, signal?: AbortSignal) =>
  request<Page<AnalysisRun>>(withCursor(`/projects/${projectId}/runs`, cursor), { signal });
export const getAnalysisRun = (projectId: string, runId: string, signal?: AbortSignal) =>
  request<AnalysisRun>(`/projects/${projectId}/runs/${runId}`, { signal });
export const listClassifications = (projectId: string, runId: string, cursor?: string) =>
  request<Page<Classification>>(withCursor(`/projects/${projectId}/runs/${runId}/classifications`, cursor));
export const getTask = (id: string, signal?: AbortSignal) =>
  request<Task>(`/tasks/${id}`, { signal });
export const stopAnalysis = (taskId: string, csrf: string) =>
  request<Task>(`/tasks/${taskId}/stop`, write("POST", undefined, csrf));
export const analysisStopped = (task?: Task) => task?.error?.code === "analysis_stopped";
export async function controlTask(task: Task, action: "pause" | "resume", csrf: string) {
  let current = task;
  for (let retries = 0; ; retries += 1) {
    try {
      return await request<Task>(`/tasks/${task.id}/${action}`,
        write("POST", { expected_version: current.version }, csrf));
    } catch (error) {
      // Starting and settling individual deliveries can advance two versions between SSE updates.
      if (task.kind !== "send" || action !== "pause" || retries >= 2 ||
        !(error instanceof ApiError) || error.code !== "version_conflict") throw error;
      current = await getTask(task.id);
      if (!["queued", "running", "backoff"].includes(current.state)) return current;
    }
  }
}

export const terminal = (task: Task) =>
  task.state === "succeeded" || task.state === "failed" || task.state === "cancelled" || task.state === "needs_review";
export const taskActive = (task: Task) => !terminal(task) && task.state !== "paused";


export type DownloadMode = "mock" | "bilibili";
export type VideoPart = {
  bvid: string; page: number; cid: string; duration_ms: number; title: string;
  video_title?: string | null;
};
export type SourcePart = VideoPart | { cid: string; duration_ms: null };
export type SourceSnapshot = {
  id: string; name: string; mode: DownloadMode; part: SourcePart; range_kind: "current_segments" | "synthetic" | "merged";
  state: "draft" | "complete"; segment_count: number; completed_segments: number;
  covered_until_ms: number; item_count: number;
};
export type SnapshotItem = {
  id: string; time_ms: number; text: string; font_size: number; color_rgb: number | null; mode: number;
};
export type SubtitleLanguage = "japanese" | "chinese";
export type ReviewReady = { run_id: string; revision: number };
export type Workflow = {
  snapshot_id: string | null;
  japanese_subtitle_id: string | null;
  chinese_subtitle_id: string | null;
  analysis_id: string | null;
  reasoning_effort: ReasoningEffort | null;
  review: ReviewReady | null;
};
export type SubtitleVersion = {
  subtitle_version_id: string; filename: string | null; format: "ass" | "srt" | "txt"; encoding: string;
  sha256: string; parser_version: string; cue_count: number;
  warnings: { line: number; code: "non_text" | "empty_text" }[];
};
export type ProjectInputs = {
  parts: { mode: DownloadMode; source: VideoPart | null; target: VideoPart } | null;
  snapshots: SourceSnapshot[]; subtitles: SubtitleVersion[];
  send_in_queue: boolean;
  workflow: Workflow | null;
};

export const getVideoParts = (bvid: string, source: DownloadMode) =>
  request<VideoPart[]>(`/videos/${encodeURIComponent(bvid)}/parts?source=${source}`);
export const getProjectInputs = (id: string, signal?: AbortSignal) =>
  request<ProjectInputs>(`/projects/${id}/inputs`, { signal });
export const listSnapshotItems = (projectId: string, snapshotId: string, cursor?: string, signal?: AbortSignal) =>
  request<Page<SnapshotItem>>(withCursor(`/projects/${projectId}/snapshots/${snapshotId}/items`, cursor), { signal });
export const mergeSnapshots = (projectId: string, snapshotIds: string[], csrf: string, confirmed = false) =>
  request<SourceSnapshot>(`/projects/${projectId}/snapshots/merge`,
    write("POST", { snapshot_ids: snapshotIds, confirm_duration_mismatch: confirmed }, csrf));
export const selectSnapshot = (projectId: string, snapshotId: string, csrf: string) =>
  request<Workflow>(`/projects/${projectId}/workflow/snapshot`, write("PUT", { snapshot_id: snapshotId }, csrf));
export const markReviewReady = (projectId: string, ready: ReviewReady, csrf: string) =>
  request<Workflow>(`/projects/${projectId}/workflow/review`, write("PUT", ready, csrf));
export const clearSubtitleLanguage = (projectId: string, language: SubtitleLanguage, csrf: string) =>
  request<Workflow>(`/projects/${projectId}/workflow/subtitles/${language}`, write("DELETE", undefined, csrf));
export const getProject = (id: string) => request<Project>(`/projects/${id}`);
export const deleteSubtitle = (projectId: string, versionId: string, csrf: string) =>
  request<void>(`/projects/${projectId}/subtitles/${versionId}`, write("DELETE", undefined, csrf));
/** Upload original bytes. TXT containing SRT is returned with SRT format and the original filename. */
export const uploadSubtitle = (
  id: string, file: File, format: "ass" | "srt" | "txt", encoding: string, csrf: string,
  language?: SubtitleLanguage,
) => request<SubtitleVersion>(
  `/projects/${id}/subtitles?${new URLSearchParams({ format, encoding, filename: file.name, ...(language ? { language } : {}) })}`,
  { method: "POST", headers: { "Content-Type": "application/octet-stream", "X-CSRF-Token": csrf },
    body: file },
);

export type ReviewFilters = {
  category?: "translation" | "other";
  selected?: boolean;
  q?: string;
};
export type ReviewCue = {
  id: string; start_ms: number; end_ms: number; plaintext: string; style: string; order: number; line: number;
};
export type ReviewRow = {
  offset_ms: number;
  text_edited: boolean;
  item_id: string; time_ms: number; text: string; color_rgb: number | null; font_size: number; mode: number;
  checkbox_type: 0 | 4;
  classification: Classification | null; nearby_cues: ReviewCue[]; evidence_cues: ReviewCue[];
  selected: boolean; selection_source: "suggestion" | "manual";
};
export type ReviewPage = Page<ReviewRow> & { revision: number; selected_count: number };
export type ReviewResults = Omit<ReviewPage, "next_cursor">;
export type ReviewCommit = { revision: number; changed_count: number; selected_count: number };

export function listReview(
  projectId: string, runId: string, filters: ReviewFilters, cursor?: string,
  limit = 100, signal?: AbortSignal,
) {
  const params = new URLSearchParams({ run_id: runId, limit: String(limit) });
  if (cursor) params.set("cursor", cursor);
  if (filters.category) params.set("category", filters.category);
  if (filters.selected !== undefined) params.set("selected", String(filters.selected));
  if (filters.q) params.set("q", filters.q);
  return request<ReviewPage>(`/projects/${projectId}/review?${params}`, { signal });
}

export async function loadReviewResults(
  projectId: string, runId: string, filters: ReviewFilters, signal: AbortSignal,
): Promise<ReviewResults> {
  const limit = 500;
  const first = await listReview(projectId, runId, filters, undefined, limit, signal);
  const { revision, selected_count } = first;
  const items = first.items.slice();
  let cursor = first.next_cursor;
  while (cursor) {
    const page = await listReview(projectId, runId, filters, cursor, limit, signal);
    if (page.revision !== revision) {
      throw new ApiError(409, "读取期间审核版本已更新，请重试读取。", "revision_conflict");
    }
    items.push(...page.items);
    cursor = page.next_cursor;
  }
  return { items, revision, selected_count };
}

export const updateReview = (
  projectId: string, runId: string, expectedRevision: number, itemIds: string[], selected: boolean, csrf: string,
) => request<ReviewCommit>(`/projects/${projectId}/review`, write("PATCH", {
  run_id: runId, expected_revision: expectedRevision, item_ids: itemIds, selected,
}, csrf));

export const updateReviewOffset = (
  projectId: string, runId: string, expectedRevision: number, itemId: string,
  offsetMs: number, applyToFollowing: boolean, csrf: string,
) => request<ReviewCommit>(`/projects/${projectId}/review/offset`, write("PATCH", {
  run_id: runId, expected_revision: expectedRevision, item_id: itemId,
  offset_ms: offsetMs, apply_to_following: applyToFollowing,
}, csrf));

export const updateReviewText = (
  projectId: string, runId: string, expectedRevision: number, itemId: string, text: string | null, csrf: string,
) => request<ReviewCommit>(`/projects/${projectId}/review/text`, write("PATCH", {
  run_id: runId, expected_revision: expectedRevision, item_id: itemId, text,
}, csrf));

export const resetReview = (projectId: string, runId: string, expectedRevision: number, csrf: string) =>
  request<ReviewCommit>(`/projects/${projectId}/review/reset`, write("POST", {
    run_id: runId, expected_revision: expectedRevision,
  }, csrf));

export type SendMode = "mock" | "live";
export type SendConfirmation = {
  target_cid: string; account_id: string; secondary_account_id?: string | null; secondary_level?: number | null;
  primary_only?: boolean;
};
export type BilibiliSettings = {
  configured: boolean; version: number; account_id: string | null;
};
export type SendingSettings = SendTiming & { dual_account_enabled: boolean };
export type ReviewSettings = { uncheck_small_by_default: boolean };
export type SettingsView = { bilibili: BilibiliSettings; llm: ModelSettings; review: ReviewSettings; sending: SendingSettings };
export type SettingsUpdate = { llm?: ModelInput; review?: ReviewSettings; sending?: Partial<SendingSettings> };

export const getSettings = (signal?: AbortSignal) =>
  request<SettingsView>("/settings", { signal });
export const saveSettings = (input: SettingsUpdate, csrf: string) =>
  request<SettingsView>("/settings", write("PUT", input, csrf));

export const getBilibiliSettings = (signal?: AbortSignal) =>
  request<BilibiliSettings>("/settings/bilibili", { signal });
export type BilibiliAccountRole = "primary" | "secondary";
export type BilibiliProfile = { account_id: string; name: string | null; avatar_url: string | null; level?: number | null };
export type BilibiliLoginStatus = {
  status: "unconfigured" | "valid" | "renewal_required" | "unavailable";
  profile: BilibiliProfile | null;
  auto_refresh: boolean;
};
export type BilibiliQr = { id: string; image: string };
export type BilibiliQrPoll = {
  status: "waiting" | "scanned" | "expired" | "authenticated";
  login: BilibiliLoginStatus | null;
};

const bilibiliLoginPath = (path: string, role: BilibiliAccountRole) =>
  `/bilibili/login/${path}${role === "secondary" ? "?role=secondary" : ""}`;

export function getBilibiliLogin(
  csrf: string, signal?: AbortSignal, role: BilibiliAccountRole = "primary", force = false,
) {
  const path = bilibiliLoginPath("status", role);
  const query = force ? (role === "secondary" ? "&force=true" : "?force=true") : "";
  return request<BilibiliLoginStatus>(path + query, { ...write("POST", undefined, csrf), signal });
}
export const startBilibiliLogin = (csrf: string, signal?: AbortSignal, role: BilibiliAccountRole = "primary") =>
  request<BilibiliQr>(bilibiliLoginPath("qr", role), { ...write("POST", undefined, csrf), signal });
export const pollBilibiliLogin = (id: string, csrf: string, signal?: AbortSignal, role: BilibiliAccountRole = "primary") =>
  request<BilibiliQrPoll>(bilibiliLoginPath(`qr/${encodeURIComponent(id)}/poll`, role),
    { ...write("POST", undefined, csrf), signal });
export const cancelBilibiliLogin = (id: string, csrf: string, role: BilibiliAccountRole = "primary") =>
  request<void>(bilibiliLoginPath(`qr/${encodeURIComponent(id)}`, role), write("DELETE", undefined, csrf));
export const logoutBilibili = (csrf: string, role: BilibiliAccountRole = "primary") =>
  request<void>(bilibiliLoginPath("", role), write("DELETE", undefined, csrf));
export type SendItem = {
  item_id: string; text: string; time_ms: number; font_size: number; color_rgb: number | null; mode: number;
  checkbox_type: 0 | 4;
  account_role?: BilibiliAccountRole | null;
  effect_differences: ("unsupported_mode" | "unsupported_color" | "unsupported_font_size")[];
};
export type SendPreview = Page<SendItem> & {
  revision: number; selected_count: number; outside_count: number; unsupported_count: number;
  source: SourcePart; source_parts?: SourcePart[]; target: VideoPart; cues: ReviewCue[];
  mode: "live"; live_target: boolean; duplicate_count: number; unstable_count: number;
  duration_mismatch_message: string | null;
  secondary_account_id: string | null; secondary_level: number | null;
};
export type DeliveryStatus = "pending" | "in_flight" | "confirmed" | "failed" | "skipped" | "unknown" | "withdrawn";
export type DeliveryFilter = DeliveryStatus | "active";
export type DeliveryResolution = "confirmed" | "skip" | "retry";
export type BilibiliSendDiagnostic = {
  http_status: number; code: number | null; message: string | null;
};
export type DeliveryAttempt = {
  number: number; started_at: string; finished_at: string | null; account_role?: BilibiliAccountRole | null;
  outcome: "in_flight" | "confirmed" | "rejected" | "limited" | "blocked" | "unknown";
  error_code: string | null; bilibili_error: BilibiliSendDiagnostic | null; receipt: string | null;
  resolution: DeliveryResolution | null; resolved_by: string | null; resolved_at: string | null;
};
export type Delivery = {
  id: string; item: SendItem; status: DeliveryStatus; resending: boolean; error_code: string | null;
  bilibili_error: BilibiliSendDiagnostic | null;
  receipt: string | null; delivered_at: string | null; attempts: DeliveryAttempt[];
};
export type DeliveryPage = Page<Delivery> & {
  batch: {
    id: string; run_id: string; review_revision: number; mode: SendMode; target: VideoPart; account_id: string | null;
    secondary_account_id?: string | null;
  };
  task_version: number;
  total_count: number;
  tracked_items?: Delivery[];
};

export function getSendPreview(projectId: string, runId: string, cursor?: string, signal?: AbortSignal) {
  const query = new URLSearchParams({ run_id: runId, limit: "500" });
  if (cursor) query.set("cursor", cursor);
  return request<SendPreview>(`/projects/${projectId}/send-preview?${query}`, { signal });
}

export const createSend = (
  projectId: string, runId: string, revision: number, key: string, csrf: string, confirmation: SendConfirmation,
) =>
  request<Task>(`/projects/${projectId}/tasks`, write("POST", {
    kind: "send", run_id: runId, expected_review_revision: revision, confirmation,
  }, csrf, key));

export function listDeliveries(
  taskId: string, status: DeliveryFilter | "", page: number, limit: number, signal?: AbortSignal,
  track: readonly string[] = [], accountRole?: BilibiliAccountRole,
) {
  const query = new URLSearchParams({ page: String(page), limit: String(limit) });
  if (status) query.set("status", status);
  if (accountRole) query.set("account_role", accountRole);
  for (const id of track) query.append("track", id);
  return request<DeliveryPage>(`/tasks/${taskId}/deliveries?${query}`, { signal });
}

export const retryFailedDeliveries = (task: Task, csrf: string) =>
  request<Task>(`/tasks/${task.id}/deliveries/retry-failed`, write("POST", { expected_version: task.version }, csrf));

export const continueDeliveryQueue = (task: Task, csrf: string) =>
  request<Task>(`/tasks/${task.id}/deliveries/continue-queue`, write("POST", { expected_version: task.version }, csrf));

export const changeDelivery = (
  taskId: string, deliveryId: string, version: number, action: "withdraw" | "resend", csrf: string,
) => request<Task>(`/tasks/${taskId}/deliveries/${deliveryId}/${action}`, write("POST", { expected_version: version }, csrf));

export const getPendingSendDecision = (signal?: AbortSignal) =>
  request<Task | null>("/send-queue/pending-decision", { signal });

export const getActiveSendTaskId = (signal?: AbortSignal) =>
  request<string | null>("/send-queue/active", { signal });

export const getInterruptedSendCount = (signal?: AbortSignal) =>
  request<number>("/send-queue/interrupted", { signal });

export interface SendRate {
  task_id: string;
  items_per_hour: number;
}

export const getSendRate = (signal?: AbortSignal) =>
  request<SendRate | null>("/send-queue/rate", { signal });

export const getSendConnection = (signal?: AbortSignal) =>
  request<boolean | null>("/send-queue/connection", { signal, cache: "no-store" });

type ConfirmedSendHead = { task_id: string; expected_version: number };

export const getPausedSendHead = () =>
  request<(ConfirmedSendHead & { project_name: string }) | null>("/send-queue/paused-head");

export const resumeInterruptedSends = (csrf: string, confirmedHead?: ConfirmedSendHead) =>
  request<number>("/send-queue/resume-interrupted", write("POST", confirmedHead, csrf));

export const getDualUpgradeCount = () => request<number>("/send-queue/dual-upgrade");

export const upgradeSendQueue = (secondaryAccountId: string, csrf: string) =>
  request<number>("/send-queue/dual-upgrade", write("POST", { secondary_account_id: secondaryAccountId }, csrf));

export const prioritizeSendTask = (task: Task, csrf: string, confirmedPreloadTaskId?: string) =>
  request<Task>(`/tasks/${task.id}/send-priority`, write("POST", {
    expected_version: task.version, confirmed_preload_task_id: confirmedPreloadTaskId,
  }, csrf));

export const usePrimarySendAccount = (task: Task, csrf: string) =>
  request<Task>(`/tasks/${task.id}/send-primary-only`, write("POST", undefined, csrf));

export const cancelSendTask = (task: Task, csrf: string) =>
  request<Task>(`/tasks/${task.id}/send-cancel`, write("POST", { expected_version: task.version }, csrf));

export const awaitingSendDecision = (task: Task) => task.kind === "send" && task.state === "paused" &&
  task.error?.code === "send_failed_review";

export const resolveDelivery = (
  taskId: string, deliveryId: string, version: number, outcome: DeliveryResolution, csrf: string,
) => request<Delivery>(`/tasks/${taskId}/deliveries/${deliveryId}/resolve`, write("POST", {
  expected_version: version, outcome,
}, csrf));
