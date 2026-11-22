import { useEffect, useState } from "react";
import {
  AnalysisInput, analysisStopped, ApiError, clearSubtitleLanguage, getModelSettings, ModelSettings, ProjectInputs, ReasoningEffort, selectSnapshot, SourceSnapshot,
  SubtitleLanguage, Task, taskActive, uploadSubtitle, Workflow,
} from "../api/workbench";
import { useQuestion } from "../components/QuestionDialog";
import { Spinner } from "./InputPanel";
import { AnalysisTerminal } from "./AnalysisTerminal";

const subtitleFields = [
  { language: "japanese", label: "日语字幕" },
  { language: "chinese", label: "中文字幕" },
] as const;

export function AnalysisPanel({
  projectId, csrf, inputs, snapshots, task, modelVersion, onSubmit, onResume, onStop, onInputsChanged, onWorkflow, onUnauthorized,
}: {
  projectId: string; csrf: string; inputs: ProjectInputs; snapshots: SourceSnapshot[]; task?: Task;
  modelVersion: number; onSubmit: (input: AnalysisInput) => Promise<void>; onResume: (task: Task) => Promise<void>;
  onStop: (task: Task) => Promise<void>;
  onInputsChanged: () => Promise<void>; onWorkflow: (workflow: Workflow) => void; onUnauthorized: () => void;
}) {
  const { confirm } = useQuestion();
  const [settings, setSettings] = useState<ModelSettings>();
  const [refresh, setRefresh] = useState(0);
  const [encoding, setEncoding] = useState("utf-8");
  const [effortDraft, setEffortDraft] = useState<ReasoningEffort | null>();
  const [action, setAction] = useState<"analyse" | "stop" | "skip" | "upload" | "select">();
  const [error, setError] = useState("");
  const [dismissedFailure, setDismissedFailure] = useState<string>();
  const [settingsError, setSettingsError] = useState("");
  const workflow = inputs.workflow;
  const snapshot = snapshots.find((entry) => entry.id === workflow?.snapshot_id) ?? snapshots[0];
  const subtitles = {
    japanese: inputs.subtitles.find((entry) => entry.subtitle_version_id === workflow?.japanese_subtitle_id),
    chinese: inputs.subtitles.find((entry) => entry.subtitle_version_id === workflow?.chinese_subtitle_id),
  };
  const active = !!task && taskActive(task);
  const succeeded = task?.id === workflow?.analysis_id && task?.state === "succeeded";
  const skipped = succeeded && task.checkpoint.analysis_skipped;
  const complete = succeeded && !skipped;
  const running = action === "analyse" || active;
  const disabled = !!action || active;
  const chinese = subtitles.chinese?.cue_count ? subtitles.chinese.subtitle_version_id : null;
  const japanese = subtitles.japanese?.cue_count ? subtitles.japanese.subtitle_version_id : null;
  const duplicate = !!chinese && chinese === japanese;
  const current = settings && settings.version >= modelVersion ? settings : undefined;
  const effort = effortDraft === undefined ? workflow?.reasoning_effort ?? null : effortDraft;
  const failureKey = task?.error ? `${task.id}:${task.version}` : undefined;
  const taskError = !running && !analysisStopped(task) && failureKey !== dismissedFailure ? task?.error : undefined;

  function report(error: unknown) {
    if (error instanceof ApiError && error.status === 401) onUnauthorized();
    else setError(error instanceof Error ? error.message : "请求失败");
  }

  useEffect(() => {
    const controller = new AbortController();
    setSettingsError("");
    getModelSettings(controller.signal).then((next) => {
      if (!controller.signal.aborted) setSettings(next);
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      else setSettingsError(error instanceof Error ? error.message : "读取模型设置失败");
    });
    return () => controller.abort();
  }, [modelVersion, refresh, onUnauthorized]);

  async function upload(language: SubtitleLanguage, file: File) {
    setError("");
    const format = file.name.split(".").pop()?.toLowerCase();
    if (format !== "ass" && format !== "srt" && format !== "txt") {
      setError("请选择 ASS、SRT 或内含 ASS、SRT 字幕的 TXT 文件");
      return;
    }
    setAction("upload");
    try {
      await uploadSubtitle(projectId, file, format, encoding, csrf, language);
      await onInputsChanged();
    } catch (error) {
      report(error);
    } finally {
      setAction(undefined);
    }
  }

  async function choose(id: string) {
    setError("");
    setAction("select");
    try {
      onWorkflow(await selectSnapshot(projectId, id, csrf));
    } catch (error) {
      report(error);
    } finally {
      setAction(undefined);
    }
  }

  async function clear(language: SubtitleLanguage) {
    setError("");
    setAction("upload");
    try {
      onWorkflow(await clearSubtitleLanguage(projectId, language, csrf));
    } catch (error) {
      report(error);
    } finally {
      setAction(undefined);
    }
  }

  async function analyse(resume?: Task) {
    if (disabled) return;
    setError("");
    setDismissedFailure(failureKey);
    setAction("analyse");
    try {
      if (resume) await onResume(resume);
      else if (snapshot && current?.configured && (chinese || japanese) && !duplicate && !complete) {
        await onSubmit({
          snapshot_id: snapshot.id, subtitle_version_id: chinese,
          japanese_subtitle_version_id: japanese, llm_config_version: current.version,
          ...(current.can_set_reasoning_effort ? { reasoning_effort: effort } : {}),
        });
        setEffortDraft(undefined);
      }
    } catch (error) {
      report(error);
    } finally {
      setAction(undefined);
    }
  }

  async function stop() {
    if (!task || !active || action) return;
    setError("");
    setAction("stop");
    try {
      await onStop(task);
    } catch (error) {
      report(error);
    } finally {
      setAction(undefined);
    }
  }

  async function skip() {
    if (disabled || !snapshot || complete || skipped) return;
    if (!await confirm({
      title: "跳过弹幕智能分析？",
      message: "进入人工审核时，所有弹幕都会默认勾选，你可以逐条取消。",
      confirmLabel: "跳过分析",
    })) return;
    setError("");
    setDismissedFailure(failureKey);
    setAction("skip");
    try {
      await onSubmit({
        skip_analysis: true, snapshot_id: snapshot.id,
        subtitle_version_id: subtitles.chinese?.subtitle_version_id ?? null,
        japanese_subtitle_version_id: subtitles.japanese?.subtitle_version_id ?? null,
      });
    } catch (error) {
      report(error);
    } finally {
      setAction(undefined);
    }
  }

  const terminalTask = (action === "analyse" && !active) || skipped ? undefined : task;

  return <><section aria-label="弹幕智能分析" className="workflow-panel">
    <form className="grid items-end gap-3 md:grid-cols-[minmax(9rem,1.3fr)_minmax(7rem,1fr)_minmax(7rem,1fr)]"
      onSubmit={(event) => { event.preventDefault(); void analyse(); }}>
      {snapshots.length > 1 ? <label className="field">弹幕快照
        <select value={snapshot?.id ?? ""} disabled={disabled} onChange={(event) => void choose(event.currentTarget.value)}>
          {snapshots.map((entry) => <option key={entry.id} value={entry.id}>{entry.name}</option>)}
        </select>
      </label> : <div className="field">
        <span>弹幕快照</span>
        <p className="truncate rounded-md bg-panel-muted px-3 py-2.5" title={snapshot?.name}>{snapshot?.name}</p>
      </div>}
      {subtitleFields.map(({ language, label }) => {
        const subtitle = subtitles[language];
        const value = subtitle?.filename ?? subtitle?.format.toUpperCase();
        return <div key={language} className="field">
          <span>{label}</span>
          <div className="flex min-w-0 gap-1">
            <label className="relative min-w-0 flex-1">
              <input className="peer sr-only" type="file" aria-label={label} accept=".ass,.srt,.txt"
                disabled={disabled} onChange={(event) => {
                  const file = event.currentTarget.files?.[0];
                  event.currentTarget.value = "";
                  if (file) void upload(language, file);
                }} />
              <span className="secondary block cursor-pointer truncate peer-focus-visible:ring-2 peer-focus-visible:ring-accent-focus
                peer-disabled:cursor-not-allowed peer-disabled:opacity-50"
                title={value ?? "支持 ASS、SRT 及内含 ASS 或 SRT 字幕的 TXT 文件"}>{value ?? "选择文件"}</span>
            </label>
            {subtitle && <button type="button" className="secondary shrink-0" title={`清除${label}`}
              aria-label={`清除${label}`} disabled={disabled} onClick={() => void clear(language)}>×</button>}
          </div>
        </div>;
      })}
      <div className="flex flex-wrap items-end justify-end gap-2 md:col-span-3">
        {current?.can_set_reasoning_effort && <label className="field mr-auto">思考强度（当前项目）
          <select value={effort ?? ""} disabled={disabled || complete}
            onChange={(event) => setEffortDraft((event.currentTarget.value || null) as ReasoningEffort | null)}>
            <option value="">跟随全局设置</option>
            <option value="low">低</option><option value="high">高</option><option value="max">最高</option>
          </select>
        </label>}
        {!complete && !skipped && !active && <button type="button" className="secondary"
          disabled={disabled || !snapshot} onClick={() => void skip()}>
          {action === "skip" ? "正在跳过…" : "跳过分析"}
        </button>}
        {active || action === "stop" ? <button type="button"
          className="secondary whitespace-nowrap border-danger-line text-danger hover:bg-danger-tint"
          disabled={!!action} onClick={() => void stop()}>
          {action === "stop" ? "正在停止…" : "停止分析"}
        </button> : <button className={complete ? "workflow-done" : "primary whitespace-nowrap"}
          disabled={disabled || complete || !current?.configured || !snapshot || (!chinese && !japanese) || duplicate}>
          {running && <Spinner />}
          {running ? "正在提交…" : complete ? "分析已完成" : task?.state === "failed" ? "重新分析" : "开始分析"}
        </button>}
      </div>
    </form>
    {(error || taskError || settingsError) && <p role="alert" className="whitespace-pre-wrap break-words text-sm text-danger">
      {error || (taskError && `${taskError.message}\n本地错误码 ${taskError.code}`) || settingsError}
    </p>}
    {settingsError && <button className="secondary" onClick={() => setRefresh((value) => value + 1)}>重新读取模型配置</button>}
    {current && !current.configured && <p className="text-sm text-ink-muted">请在设置中保存模型 API 密钥。</p>}
    {task?.state === "paused" && <button className="secondary" disabled={disabled}
      onClick={() => void analyse(task)}>继续分析</button>}
    {duplicate && <p className="text-sm text-warning">请选择不同的日语和中文字幕。</p>}
    {complete && <p className="text-sm text-ink-muted">{snapshot?.name}分析</p>}
    {skipped && <p className="text-sm text-ink-muted">已跳过智能分析</p>}
    <details className="text-sm text-ink-muted">
      <summary className="w-fit cursor-pointer">字幕编码</summary>
      <select aria-label="字幕编码" value={encoding} disabled={disabled}
        className="mt-2 rounded-md border border-line-strong bg-panel p-2"
        onChange={(event) => setEncoding(event.currentTarget.value)}>
        <option value="utf-8">UTF-8</option><option value="utf-16">UTF-16</option>
        <option value="gb18030">GB18030</option><option value="shift_jis">Shift JIS</option>
      </select>
    </details>
  </section>
    {(running || terminalTask) && <AnalysisTerminal key={terminalTask?.id ?? "submitting"} task={terminalTask} />}
  </>;
}
