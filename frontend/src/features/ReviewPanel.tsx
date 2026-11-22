import { Tooltip } from "../components/Tooltip";
import { FormEvent, useEffect, useRef, useState } from "react";
import {
  AnalysisRun, ApiError, getAnalysisRun, listAnalysisRuns, loadReviewResults, ReviewCue, ReviewFilters,
  resetReview, ReviewReady, ReviewResults, ReviewRow, updateReview, updateReviewOffset, updateReviewText,
} from "../api/workbench";
import { useQuestion } from "../components/QuestionDialog";
import { formatDuration } from "../formatDuration";
import { danmakuColorLabel, danmakuModeLabel, DanmakuText } from "./DanmakuAppearance";

function categoryLabel(row: ReviewRow) {
  if (row.checkbox_type === 4) return "UP 主弹幕";
  if (row.classification?.category === "translation") return "翻译";
  if (row.classification?.category === "other") return "其他";
  return "未分类";
}

const diagnosticLabels: Record<string, string> = {
  needs_review: "待人工核对",
  translation_request: "求翻译或缺字幕",
  subtitle_or_translation_topic: "字幕或翻译话题",
};

function CueList({ cues, empty }: { cues: ReviewCue[]; empty: string }) {
  return cues.length ? <ul className="space-y-2">
    {cues.map((cue) => <li key={cue.id}>
      <div className="text-xs tabular-nums text-ink-muted">
        {cue.id.startsWith("ja:") ? "日语 · " : cue.id.startsWith("zh:") ? "中文 · " : ""}
        {formatDuration(cue.start_ms)} – {formatDuration(cue.end_ms)}
      </div>
      <div className="whitespace-pre-wrap break-words">{cue.plaintext}</div>
    </li>)}
  </ul> : <p className="text-ink-muted">{empty}</p>;
}

function ReviewDetails({ row, analysisSkipped }: { row: ReviewRow | undefined; analysisSkipped: boolean }) {
  return <aside aria-label="审核依据" className="min-w-0 space-y-4 bg-canvas p-4 text-sm">
    <h4 className="font-semibold">审核依据</h4>
    {row ? <>
      <div className="space-y-1">
        <p className="[overflow-wrap:anywhere] font-medium">
          <DanmakuText text={row.text} rgb={row.color_rgb} fontSize={row.font_size} />
        </p>
        <p className="text-ink-muted">
          字号 {row.font_size} · 色号 {danmakuColorLabel(row.color_rgb)} · 类型 {danmakuModeLabel(row.mode)}
        </p>
        <p>{categoryLabel(row)} · {row.selection_source === "manual" ? "人工选择"
          : analysisSkipped ? "默认选择" : "建议选择"}</p>
        {row.classification?.diagnostic && <p className="break-words text-warning">
          {diagnosticLabels[row.classification.diagnostic] ?? row.classification.diagnostic}
        </p>}
        {row.classification?.reason && <p className="break-words">{row.classification.reason}</p>}
      </div>
      <div className="space-y-2">
        <h5 className="font-medium">同期字幕</h5>
        <CueList cues={row.nearby_cues} empty="附近没有字幕" />
      </div>
      <div className="space-y-2">
        <h5 className="font-medium">分类依据字幕</h5>
        <CueList cues={row.evidence_cues} empty="没有引用字幕" />
      </div>
    </> : <p className="text-ink-muted">选择一条弹幕查看依据。</p>}
  </aside>;
}

function ReviewTable({ projectId, runId, analysisSkipped, csrf, onUnauthorized, onPreview }: {
  projectId: string; runId: string; analysisSkipped: boolean; csrf: string; onUnauthorized: () => void;
  onPreview: (ready: ReviewReady) => Promise<void>;
}) {
  const { choose, confirm, prompt } = useQuestion();
  const [filters, setFilters] = useState<ReviewFilters>({});
  const [search, setSearch] = useState("");
  const [results, setResults] = useState<ReviewResults>();
  const scrollBox = useRef<HTMLDivElement | null>(null);
  const scrollRestored = useRef(false);
  const scrollKey = `danmaku-review-scroll:${encodeURIComponent(projectId)}:${encodeURIComponent(runId)}`;
  function rememberScroll(position: number) {
    try { localStorage.setItem(scrollKey, String(position)); } catch { /* Storage may be unavailable. */ }
  }
  function attachScrollBox(element: HTMLDivElement | null) {
    scrollBox.current = element;
    if (element && !scrollRestored.current) {
      scrollRestored.current = true;
      try {
        const saved = Number(localStorage.getItem(scrollKey));
        if (Number.isFinite(saved) && saved > 0) element.scrollTop = saved;
      } catch { /* The list remains usable without storage. */ }
    }
  }
  const offsetEdited = useRef(false);
  const [focused, setFocused] = useState<string>();
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [saveError, setSaveError] = useState("");
  const busy = loading || saving;

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setLoadError("");
    loadReviewResults(projectId, runId, filters, controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setResults(result);
      setFocused((current) => result.items.some((row) => row.item_id === current)
        ? current : result.items[0]?.item_id);
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      else setLoadError(error instanceof Error ? error.message : "读取审核数据失败");
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [projectId, runId, filters, refresh, onUnauthorized]);

  function filter(changes: ReviewFilters) {
    setLoading(true);
    setSaveError("");
    if (scrollBox.current) scrollBox.current.scrollTop = 0;
    rememberScroll(0);
    scrollRestored.current = true;
    setFilters({ ...filters, ...changes });
  }

  function reloadReview() {
    setLoading(true);
    setRefresh((value) => value + 1);
  }

  function handleSaveError(error: unknown, fallback: string) {
    if (error instanceof ApiError && error.status === 401) onUnauthorized();
    else if (error instanceof ApiError && error.code === "revision_conflict") {
      setSaveError("审核版本已更新，已请求最新数据。请核对后重新操作。");
      reloadReview();
    } else setSaveError(error instanceof Error ? error.message : fallback);
  }

  async function save(itemId: string, selected: boolean) {
    if (!results || busy || loadError) return;
    setSaving(true);
    setSaveError("");
    try {
      const commit = await updateReview(projectId, runId, results.revision, [itemId], selected, csrf);
      const keepChanged = filters.selected === undefined || filters.selected === selected;
      const items = keepChanged
        ? results.items.map((item) => item.item_id === itemId
          ? { ...item, selected, selection_source: "manual" as const } : item)
        : results.items.filter((item) => item.item_id !== itemId);
      setResults({ items, revision: commit.revision, selected_count: commit.selected_count });
      setFocused((current) => items.some((item) => item.item_id === current) ? current : items[0]?.item_id);
    } catch (error) {
      handleSaveError(error, "保存审核选择失败");
    } finally {
      setSaving(false);
    }
  }

  async function editText(item: ReviewRow) {
    if (!results || busy || loadError) return;
    setFocused(item.item_id);
    let text: string | null = null;
    if (!item.text_edited) {
      const original = item.text.replace(/\r\n?/g, "\n");
      text = await prompt({
        title: "修改弹幕",
        initialValue: original,
        multiline: true,
        confirmLabel: "保存",
      });
      if (text === null || text === original) return;
      if (!text.trim()) {
        setSaveError("弹幕内容不能为空。");
        return;
      }
    }
    setSaveError("");
    setSaving(true);
    try {
      const commit = await updateReviewText(projectId, runId, results.revision, item.item_id, text, csrf);
      if (commit.changed_count) reloadReview();
    } catch (error) {
      handleSaveError(error, item.text_edited ? "撤销弹幕修改失败" : "保存弹幕内容失败");
    } finally {
      setSaving(false);
    }
  }

  async function saveOffset(item: ReviewRow, edit: HTMLInputElement) {
    if (!offsetEdited.current) return;
    offsetEdited.current = false;
    const value = edit.value.trim();
    const offset = Number(value);
    edit.value = String(item.offset_ms);
    if (!results || busy || loadError) return;
    if (!/^[+-]?\d+$/.test(value) || !Number.isSafeInteger(offset)) {
      setSaveError("请输入有效的整数毫秒偏移。");
      return;
    }
    setSaveError("");
    const applyToFollowing = await choose({
      title: "将偏移应用到此条及之后的弹幕？",
      message: "范围按原始时间排序，包含筛选隐藏的条目。",
      confirmLabel: "应用到后续",
      alternativeLabel: "仅保存此条",
    });
    if (applyToFollowing === null) return;
    setSaving(true);
    try {
      const commit = await updateReviewOffset(
        projectId, runId, results.revision, item.item_id, offset, applyToFollowing, csrf,
      );
      if (commit.changed_count) reloadReview();
    } catch (error) {
      handleSaveError(error, "保存弹幕偏移失败");
    } finally {
      setSaving(false);
    }
  }

  async function reset() {
    if (!results || busy || loadError) return;
    if (!await confirm({
      title: analysisSkipped ? "重置全部人工选择？" : "重置当前分析运行的全部人工选择？",
      message: analysisSkipped ? "所有弹幕将恢复为勾选状态。" : "所有条目将恢复为默认建议。",
      confirmLabel: "重置选择",
    })) return;
    setSaving(true);
    setSaveError("");
    try {
      await resetReview(projectId, runId, results.revision, csrf);
      reloadReview();
    } catch (error) {
      handleSaveError(error, "重置人工选择失败");
    } finally {
      setSaving(false);
    }
  }

  function submitSearch(event: FormEvent) {
    event.preventDefault();
    if (!busy) filter({ q: search.trim() });
  }

  const rows = loadError ? [] : results?.items ?? [];
  const row = rows.find((item) => item.item_id === focused);
  const selectedInResults = rows.filter((item) => item.selected).length;

  async function openPreview() {
    if (!results || busy || loadError) return;
    setSaving(true);
    setSaveError("");
    try {
      await onPreview({ run_id: runId, revision: results.revision });
    } catch (error) {
      handleSaveError(error, "无法打开投递预览");
    } finally {
      setSaving(false);
    }
  }

  return <div className="grid gap-4 xl:grid-cols-[10rem_minmax(0,1fr)_15rem]">
    <form onSubmit={submitSearch} aria-label="筛选弹幕" className="min-w-0 space-y-3">
      <label className="field">弹幕文字<input value={search} maxLength={200}
        onChange={(event) => setSearch(event.currentTarget.value)} /></label>
      <button className="secondary" disabled={busy}>搜索</button>
      <label className="field">分类
        <select disabled={busy} value={filters.category ?? ""} onChange={(event) => {
          const value = event.currentTarget.value as ReviewFilters["category"] | "";
          filter({ category: value || undefined });
        }}>
          <option value="">全部</option><option value="translation">翻译</option><option value="other">其他</option>
        </select>
      </label>
      <label className="field">选择状态
        <select disabled={busy} value={filters.selected === undefined ? "" : String(filters.selected)}
          onChange={(event) => {
            const value = event.currentTarget.value;
            filter({ selected: value === "" ? undefined : value === "true" });
          }}>
          <option value="">全部</option><option value="true">已选</option><option value="false">未选</option>
        </select>
      </label>
    </form>
    <div className="min-w-0 space-y-3" aria-busy={busy}>
      <div className="flex flex-wrap items-center gap-2">
        <Tooltip text="初始化为分析结果"><button className="secondary" disabled={busy || !results || !!loadError} onClick={reset}>重置</button></Tooltip>
        <button className="primary" disabled={busy || !results || !!loadError}
          onClick={() => void openPreview()}>前往预览&amp;发送</button>
      </div>
      <p role="status" className="text-sm text-ink-muted">
        {saving ? "正在保存…" : loading ? "正在读取…" : results && !loadError
          ? `筛选结果已选 ${selectedInResults}/${rows.length} · 此运行共选 ${results.selected_count} 条 · 修订 ${results.revision}`
          : ""}
      </p>
      {saveError && <p role="alert" className="text-sm text-danger">{saveError}</p>}
      {loadError && <div className="space-y-2">
        <p role="alert" className="text-sm text-danger">{loadError}</p>
        <button className="secondary" disabled={busy} onClick={() => {
          setLoading(true);
          setRefresh((value) => value + 1);
        }}>重试读取</button>
      </div>}
      {!loading && !loadError && rows.length === 0 && <p className="text-sm text-ink-muted">没有符合条件的弹幕。</p>}
      {rows.length > 0 && <div ref={attachScrollBox} onScroll={(event) => rememberScroll(event.currentTarget.scrollTop)}
        role="region" aria-label="弹幕审核列表" tabIndex={0}
        className="max-h-[34rem] overflow-auto overscroll-y-contain rounded border border-line">
        <table aria-label="弹幕审核" className="w-full text-left text-sm">
          <thead className="sticky top-0 bg-panel-muted">
            <tr><th className="p-2">勾选</th><th className="p-2">时间</th>
              <th className="p-2">弹幕</th><th className="whitespace-nowrap p-2">操作</th><th className="p-2">分类</th>
              <th scope="col" className="whitespace-nowrap p-2">偏移（毫秒）</th><th className="p-2">来源</th></tr>
          </thead>
          <tbody>
            {rows.map((item) => <tr key={item.item_id}
              data-focused={item.item_id === focused || undefined}
              data-needs-review={item.classification?.diagnostic === "needs_review" || undefined}
              className="border-t data-[focused=true]:bg-review-focus data-[needs-review=true]:bg-warning-tint
                data-[needs-review=true]:data-[focused=true]:bg-warning-badge">
              <td className="p-2"><input type="checkbox" aria-label={`勾选弹幕 ${item.text}`}
                checked={item.selected} disabled={busy}
                onChange={(event) => {
                  setFocused(item.item_id);
                  void save(item.item_id, event.currentTarget.checked);
                }} /></td>
              <td className="whitespace-nowrap p-2 tabular-nums">{formatDuration(item.time_ms)}</td>
              <td className="min-w-24 p-2">
                <button className="whitespace-pre-wrap break-all text-left underline underline-offset-2"
                  aria-pressed={item.item_id === focused}
                  onClick={() => setFocused(item.item_id)}>
                  <DanmakuText text={item.text} rgb={item.color_rgb} fontSize={item.font_size} />
                </button>
              </td>
              <td className="whitespace-nowrap p-2">
                <Tooltip text={item.text_edited ? "撤销弹幕内容修改" : "修改弹幕内容"}><button type="button" className="secondary" disabled={busy}
                  onClick={() => void editText(item)}>{item.text_edited ? "撤销" : "修改"}</button></Tooltip>
              </td>
              <td className="whitespace-nowrap p-2">
                {categoryLabel(item)}
                {item.classification?.diagnostic === "needs_review" &&
                  <div className="text-xs text-warning">待人工核对</div>}
              </td>
              <td className="whitespace-nowrap p-2">
                <Tooltip text="负数提前，正数延后。输入偏移量后会询问是否为该行后面的弹幕统一应用该偏移量"><input key={item.offset_ms} type="text" className="w-24 rounded border border-line px-2 py-1 tabular-nums"
                  aria-label={`弹幕偏移（毫秒） ${item.text}`}
                  defaultValue={item.offset_ms} disabled={busy}
                  onFocus={() => setFocused(item.item_id)}
                  onChange={() => { offsetEdited.current = true; }}
                  onBlur={(event) => void saveOffset(item, event.currentTarget)} /></Tooltip>
              </td>
              <td className="whitespace-nowrap p-2">{item.selection_source === "manual" ? "人工"
                : analysisSkipped ? "默认" : "建议"}</td>
            </tr>)}
          </tbody>
        </table>
      </div>}
    </div>
    <ReviewDetails row={row} analysisSkipped={analysisSkipped} />
  </div>;
}

export function ReviewPanel({ projectId, preferredRunId, csrf, onUnauthorized, onPreview }: {
  projectId: string; csrf: string; onUnauthorized: () => void; onPreview: (ready: ReviewReady) => Promise<void>;
  preferredRunId?: string;
}) {
  const [runs, setRuns] = useState<AnalysisRun[]>([]);
  const [runId, setRunId] = useState("");
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  async function load(after?: string, signal?: AbortSignal, preferred = runId || preferredRunId) {
    setLoading(true);
    setError("");
    try {
      const result = await listAnalysisRuns(projectId, after, signal);
      const items = result.items;
      if (!after && preferred && !items.some((run) => run.id === preferred)) {
        items.unshift(await getAnalysisRun(projectId, preferred, signal));
      }
      if (signal?.aborted) return;
      setRuns((current) => after ? [...current, ...items.filter(
        (run) => !current.some((entry) => entry.id === run.id),
      )] : items);
      setCursor(result.next_cursor);
      if (!after) {
        const latest = items.find((run) => run.state === "succeeded");
        setRunId(preferred || latest?.id || "");
      }
    } catch (error) {
      if (signal?.aborted) return;
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      else setError(error instanceof Error ? error.message : "读取分析运行失败");
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    void load(undefined, controller.signal, preferredRunId ?? "");
    return () => controller.abort();
  }, [projectId, preferredRunId]);

  const finishedRuns = runs.filter((run) => run.state === "succeeded");
  const nameCounts = new Map<string, number>();
  for (const run of finishedRuns) nameCounts.set(run.name, (nameCounts.get(run.name) ?? 0) + 1);
  const namesSeen = new Map<string, number>();

  return <section aria-label="人工审核" className="min-w-0 space-y-4 rounded-lg border bg-panel p-4">
    <div className="flex flex-wrap items-end gap-2">
      <label className="field min-w-0 flex-1">分析运行
        <select value={runId} disabled={loading} onChange={(event) => setRunId(event.currentTarget.value)}>
          <option value="">选择运行</option>
          {finishedRuns.map((run) => {
            const index = (namesSeen.get(run.name) ?? 0) + 1;
            namesSeen.set(run.name, index);
            return <option key={run.id} value={run.id}>
              {run.name}{(nameCounts.get(run.name) ?? 0) > 1 && ` · 第 ${index} 份`}
            </option>;
          })}
        </select>
      </label>
      <button className="secondary" disabled={loading} onClick={() => load()}>刷新运行</button>
      {cursor && <button className="secondary" disabled={loading} onClick={() => load(cursor)}>更多运行</button>}
    </div>
    {error && <p role="alert" className="text-sm text-danger">{error}</p>}
    {runId ? <ReviewTable key={runId} projectId={projectId} runId={runId} csrf={csrf}
      analysisSkipped={finishedRuns.some((run) => run.id === runId && run.strategy === "skipped")}
      onUnauthorized={onUnauthorized} onPreview={onPreview} /> :
      !error && <p className="text-sm text-ink-muted">
      {loading ? "正在读取运行…" : "请选择已结束的分析运行。"}
    </p>}
  </section>;
}
