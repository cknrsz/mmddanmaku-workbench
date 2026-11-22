import { useEffect, useRef, useState } from "react";
import {
  AnalysisInput, ApiError, controlTask, createAnalysis, createTask, DownloadSource, getProjectInputs, listTasks, markReviewReady,
  Project, ProjectInputs, ReviewReady, SendRate, SourceSnapshot, stopAnalysis, Task, taskActive, TaskKind, terminal, Workflow,
} from "../api/workbench";
import { watchTask } from "../api/taskEvents";
import { AnalysisPanel } from "./AnalysisPanel";
import { InputPanel } from "./InputPanel";
import { ProjectName } from "./ProjectName";
import { ReviewPanel } from "./ReviewPanel";
import { SendPreview } from "./SendPreview";
import { SendProgress } from "./SendProgress";

const stages = ["① 弹幕拉取", "② 弹幕智能分析", "③ 人工审核", "④ 弹幕发送"];
const taskKinds: TaskKind[] = ["download", "classify", "send"];

function sendComplete(task: Task) {
  const { done, total, failed, unknown } = task.progress;
  return task.state !== "cancelled" && done === total && failed === 0 && unknown === 0;
}

function sendingInProject(task: Task) {
  const { done, total, unknown } = task.progress;
  return !terminal(task) && task.checkpoint.started === true && !task.checkpoint.waiting_after_priority &&
    !sendComplete(task) && !(unknown > 0 && done + unknown === total);
}

async function submitTask(storageKey: string, submit: (key: string) => Promise<Task>) {
  const key = sessionStorage.getItem(storageKey) ?? crypto.randomUUID();
  sessionStorage.setItem(storageKey, key);
  try {
    const task = await submit(key);
    sessionStorage.removeItem(storageKey);
    return task;
  } catch (error) {
    if (error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 401) {
      sessionStorage.removeItem(storageKey);
    }
    throw error;
  }
}

export function ProjectWorkflow({ project, csrf, modelVersion, reviewRefresh, bilibiliVersion, openDelivery, onActivity, sendRate, onSendControl, onUnauthorized }: {
  project: Project; csrf: string; modelVersion: number; reviewRefresh: number; bilibiliVersion: number;
  openDelivery?: Task; onActivity: (id: string, activity?: Project["activity"]) => void; onUnauthorized: () => void;
  sendRate: SendRate | null;
  onSendControl: () => void;
}) {
  const id = project.id;
  const stageStorageKey = `danmaku:project-stage:${id}`;
  const [inputs, setInputs] = useState<ProjectInputs>();
  const [tasks, setTasks] = useState<Record<string, Task>>({});
  const [stage, setStage] = useState<number | undefined>(() => {
    const saved = sessionStorage.getItem(stageStorageKey);
    const index = stages.findIndex((_, index) => String(index) === saved);
    return index === -1 ? undefined : index;
  });
  const [sendPane, setSendPane] = useState<string>();
  const [reviewRun, setReviewRun] = useState<string>();
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const inputGeneration = useRef(0);

  function acceptTasks(next: Task[]) {
    setTasks((current) => {
      const updated = { ...current };
      for (const task of next) {
        if (task.version > (updated[task.id]?.version ?? 0)) updated[task.id] = task;
      }
      return updated;
    });
  }

  function updateInputs(next: (current: ProjectInputs) => ProjectInputs) {
    inputGeneration.current += 1;
    setInputs((current) => current ? next(current) : current);
  }

  function updateWorkflow(workflow: Workflow) {
    updateInputs((current) => ({ ...current, workflow }));
  }

  function markSendInQueue() {
    updateInputs((current) => ({ ...current, send_in_queue: true }));
    setRefresh((value) => value + 1);
  }

  async function readInputs(signal?: AbortSignal) {
    const generation = ++inputGeneration.current;
    const next = await getProjectInputs(id, signal);
    if (!signal?.aborted && generation === inputGeneration.current) setInputs(next);
  }

  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    async function load() {
      try {
        const pages = await Promise.all(taskKinds.map((kind) => listTasks(id, undefined, kind, controller.signal)));
        if (controller.signal.aborted) return;
        acceptTasks(pages.flatMap((page) => page.items));
        await readInputs(controller.signal);
        if (!controller.signal.aborted) setError("");
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) onUnauthorized();
        else setError(error instanceof Error ? error.message : "读取项目失败");
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(load, 3000);
      }
    }
    void load();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [id, refresh, bilibiliVersion, onUnauthorized]);

  useEffect(() => {
    if (!openDelivery) return;
    acceptTasks([openDelivery]);
    setSendPane(openDelivery.id);
    setStage(3);
  }, [openDelivery]);

  const snapshots = inputs?.snapshots.filter((snapshot) => snapshot.state === "complete") ?? [];
  const ordered = Object.values(tasks).sort((left, right) => right.creation_order - left.creation_order);
  const downloads = ordered.filter((task) => task.kind === "download" &&
    !snapshots.some((snapshot) => snapshot.id === task.id && snapshot.range_kind === "merged"));
  const analyses = ordered.filter((task) => task.kind === "classify");
  const sends = ordered.filter((task) => task.kind === "send");
  const activeDownload = downloads.find(taskActive);
  const activeAnalysis = analyses.find(taskActive);
  const activeSend = sends.filter(taskActive).at(-1);
  const sendPlan = activeSend?.checkpoint;
  const previewPlanKey = `${activeSend?.id ?? ""}:${sendPlan?.secondary_upgrade_id ?? ""}:${sendPlan?.primary_only ?? false}:${!!sendPlan?.accounts?.secondary}:${sendPlan?.share_count ?? 0}`;
  const sending = sends.find(sendingInProject);
  const activeTasks = [activeSend, activeAnalysis, activeDownload];
  const runningTask = sending ?? [activeAnalysis, activeDownload].find(
    (task) => task?.state === "running" || task?.state === "pausing",
  );
  const stageRunning = !!runningTask;
  const stageActive = activeTasks.some(Boolean);
  const currentAnalysis = inputs?.workflow?.analysis_id ? tasks[inputs.workflow.analysis_id] : undefined;
  const review = inputs?.workflow?.review;
  const successfulAnalysis = currentAnalysis?.state === "succeeded" ? currentAnalysis
    : analyses.find((task) => task.state === "succeeded");
  const latestDeliveryStep = ordered.find((task) => task.kind === "send" ||
    (task.kind === "classify" && task.state === "succeeded"));
  const readyToSend = !!review || latestDeliveryStep?.kind === "send";
  const hasDeliveryStep = readyToSend || !!latestDeliveryStep;
  const waitingStage = inputs && hasDeliveryStep && !ordered.some((task) => task.state === "paused")
    ? (readyToSend ? "send" : "review") : undefined;
  const activityStage = (runningTask ?? activeTasks.find(Boolean))?.kind ?? waitingStage;

  useEffect(() => {
    onActivity(id, activityStage ? { stage: activityStage, running: stageRunning, active: stageActive } : undefined);
    return () => onActivity(id);
  }, [id, activityStage, stageRunning, stageActive, onActivity]);

  useEffect(() => {
    const running = [...downloads.filter(taskActive), ...(activeAnalysis ? [activeAnalysis] : [])];
    const listeners = running.map((task) => watchTask(task, (next) => {
      acceptTasks([next]);
      if (next.state === "succeeded") {
        void readInputs().catch((error: unknown) => {
          if (error instanceof ApiError && error.status === 401) onUnauthorized();
        });
      }
    }, () => {}, onUnauthorized));
    return () => listeners.forEach((stop) => stop());
  }, [downloads.filter(taskActive).map((task) => task.id).sort().join(","), activeAnalysis?.id, onUnauthorized]);

  async function download(source: DownloadSource) {
    const identifier = "source_cid" in source ? `cid:${source.source_cid}` : `${source.source_bvid}:${source.source_page}`;
    const key = `danmaku:download:${id}:${identifier}`;
    const task = await submitTask(key, (key) => createTask(id, key, csrf, "bilibili", source));
    acceptTasks([task]);
    if (task.state === "succeeded") void readInputs().catch(() => setRefresh((value) => value + 1));
    return task;
  }

  function addMerged(snapshot: SourceSnapshot) {
    updateInputs((current) => ({
      ...current, snapshots: [snapshot, ...current.snapshots.filter((item) => item.id !== snapshot.id)],
    }));
  }

  async function analyse(input: AnalysisInput) {
    const task = await submitTask(`danmaku:classify:${id}:${JSON.stringify(input)}`,
      (key) => createAnalysis(id, key, csrf, input));
    acceptTasks([task]);
    updateWorkflow({
      snapshot_id: input.snapshot_id, chinese_subtitle_id: input.subtitle_version_id ?? null,
      japanese_subtitle_id: input.japanese_subtitle_version_id ?? null, analysis_id: task.id, review: null,
      reasoning_effort: input.reasoning_effort === undefined ? inputs?.workflow?.reasoning_effort ?? null : input.reasoning_effort,
    });
    setReviewRun(input.skip_analysis ? task.checkpoint.run_id ?? task.id : undefined);
    if (input.skip_analysis) setStage(2);
  }

  async function resume(task: Task) {
    acceptTasks([await controlTask(task, "resume", csrf)]);
  }

  async function openPreview(ready: ReviewReady) {
    updateWorkflow(await markReviewReady(id, ready, csrf));
    setReviewRun(ready.run_id);
    setSendPane("preview");
    setStage(3);
  }

  const available = [true, snapshots.length > 0, !!successfulAnalysis, !!review || sends.length > 0];
  const sentAfterAnalysis = !!sends[0] && (!currentAnalysis || sends[0].creation_order > currentAnalysis.creation_order);
  const done = [available[1], currentAnalysis?.state === "succeeded" || !!review, !!review || sentAfterAnalysis,
    sentAfterAnalysis && sendComplete(sends[0])];
  const current = done.findIndex((complete) => !complete);
  const initialStage = sentAfterAnalysis || review ? 3 : currentAnalysis?.state === "succeeded" ? 2 : snapshots.length ? 1 : 0;
  const visible = stage !== undefined && available[stage] ? stage : initialStage;
  const returnedSendId = sends[0]?.checkpoint.publication === "returned" ? sends[0].id : undefined;
  const showPreview = !!review && (sendPane === "preview" || (!sendPane && (!sentAfterAnalysis || !!returnedSendId)));
  const selectedSend = showPreview ? undefined : tasks[sendPane ?? ""] ?? sends[0];

  const inputsLoaded = inputs !== undefined;
  const watchedSend = sending ?? activeSend;
  const backgroundSend = visible === 3 && inputsLoaded && selectedSend?.id === watchedSend?.id ? undefined : watchedSend;
  useEffect(() => {
    if (!backgroundSend) return;
    return watchTask(backgroundSend, (task) => acceptTasks([task]), () => {}, onUnauthorized);
  }, [backgroundSend?.id, onUnauthorized]);

  useEffect(() => {
    if (returnedSendId && review) setSendPane((current) => current === returnedSendId ? "preview" : current);
  }, [returnedSendId, review?.run_id]);

  useEffect(() => {
    if (!inputsLoaded) return;
    if (stage === undefined) setStage(visible);
    sessionStorage.setItem(stageStorageKey, String(visible));
  }, [inputsLoaded, stage, stageStorageKey, visible]);

  return <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
    <header className="shrink-0">
      <h2 className="text-lg font-semibold sm:text-xl"><ProjectName name={project.name} lines={1} /></h2>
      <p className="mt-0.5 text-sm text-ink-muted">目标视频 · {project.target_bvid} P{project.target_page}</p>
    </header>
    <nav aria-label="项目工作区" className="grid shrink-0 grid-cols-2 gap-2 sm:grid-cols-4">
      {stages.map((label, index) => <button key={label} type="button" aria-pressed={visible === index}
        disabled={!available[index]} className={`workflow-tab ${done[index] ? "workflow-done" :
          current === index ? "workflow-current" : "workflow-available"}`}
        onClick={() => setStage(index)}>
        <span>{label}</span>
        {available[index] && <span className="block text-xs font-normal">
          {index === 1 && currentAnalysis?.checkpoint.analysis_skipped ? "已跳过"
            : index === 3 && sentAfterAnalysis &&
              (sends[0].state === "needs_review" || sends[0].error?.code === "send_partial") && sends[0].progress.failed > 0
              ? <span className="text-warning"><span aria-hidden="true">? </span>部分弹幕发送失败，待核对</span>
            : index === 3 && sends[0]?.state === "needs_review" ? "需人工核对"
            : done[index] ? "已完成" : current === index ? <>正在进行{index === 0 ? " · 输入想要迁移其弹幕的视频。" : index === 1 ? " · 智能识别弹幕翻译内容" : ""}</> : "可查看"}
        </span>}
      </button>)}
    </nav>
    {error && <p role="alert" className="text-sm text-danger">{error}{" "}
      <button className="secondary" onClick={() => setRefresh((value) => value + 1)}>重试读取</button>
    </p>}
    {!inputs && !error && <p data-backend-notice role="status" className="text-sm text-ink-muted">正在读取项目…</p>}
    {inputs && <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        <div hidden={visible !== 0}>
          <InputPanel project={project} csrf={csrf} snapshots={snapshots} task={activeDownload ?? downloads[0]} downloads={downloads}
            targetPart={inputs.parts?.target}
            onDownload={download} onMerged={addMerged} onResume={resume} onUnauthorized={onUnauthorized} />
        </div>
        {available[1] && <div hidden={visible !== 1}>
          <AnalysisPanel projectId={id} csrf={csrf} inputs={inputs} snapshots={snapshots}
            task={activeAnalysis ?? currentAnalysis} modelVersion={modelVersion} onSubmit={analyse} onResume={resume}
            onStop={async (task) => acceptTasks([await stopAnalysis(task.id, csrf)])}
            onInputsChanged={readInputs} onWorkflow={updateWorkflow} onUnauthorized={onUnauthorized} />
        </div>}
        {visible === 2 && <ReviewPanel key={reviewRefresh} projectId={id} preferredRunId={reviewRun ?? review?.run_id ?? successfulAnalysis?.checkpoint.run_id}
          csrf={csrf} onUnauthorized={onUnauthorized} onPreview={openPreview} />}
      {visible === 3 && <div className={`flex h-full flex-col gap-3 ${selectedSend ? "min-h-[22rem]" : ""}`}>
          {sends.length > 0 && <label className="flex shrink-0 items-center gap-2 text-sm">发送记录
            <select className="min-w-0 rounded-md border border-line-strong bg-panel px-2 py-1"
              value={selectedSend?.id ?? "preview"} onChange={(event) => setSendPane(event.currentTarget.value)}>
              {review && <option value="preview">预览待发送弹幕</option>}
              {sends.map((task, index) => <option key={task.id} value={task.id}>
                {index === 0 ? "最近一次发送" : `倒数第 ${index + 1} 次发送`}
              </option>)}
            </select>
          </label>}
          {selectedSend ? <SendProgress key={selectedSend.id} task={selectedSend} csrf={csrf} sendRate={sendRate}
            publicationStatus={project.publication_status} publishAt={project.preload_publish_at}
            onTask={(task) => acceptTasks([task])} onControl={onSendControl} onUnauthorized={onUnauthorized} />
            : review && <SendPreview key={`${review.run_id}:${review.revision}:${bilibiliVersion}:${inputs.send_in_queue}:${previewPlanKey}`}
              projectId={id} runId={review.run_id} revision={review.revision} csrf={csrf}
              sendInQueue={inputs.send_in_queue}
              publishAt={project.preload_publish_at}
              onSendConflict={markSendInQueue}
              onCreated={(task) => {
                acceptTasks([task]);
                setSendPane(task.id);
                if (!terminal(task)) markSendInQueue();
              }}
              onUnauthorized={onUnauthorized}
              onStale={() => {
                setReviewRun(review.run_id);
                updateWorkflow({ ...inputs.workflow!, review: null });
                setStage(2);
              }} />}
        </div>}
    </div>}
  </div>;
}
