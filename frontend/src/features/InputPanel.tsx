import { Tooltip } from "../components/Tooltip";
import { useEffect, useState } from "react";
import {
  ApiError, DownloadSource, getVideoParts, listSnapshotItems, mergeSnapshots, Project, SnapshotItem, SourceSnapshot, Task, taskActive,
  VideoPart,
} from "../api/workbench";
import { confirmDownloadDuration } from "../confirmDownloadDuration";
import { useQuestion } from "../components/QuestionDialog";
import { formatDuration } from "../formatDuration";
import { DanmakuText, danmakuModeLabel } from "./DanmakuAppearance";
import { VideoPartPicker } from "./VideoPartPicker";

export function Spinner() {
  return <span aria-hidden="true" className="mr-2 inline-block size-3 animate-spin rounded-full border-2
    border-current border-r-transparent motion-reduce:animate-none" />;
}

function SnapshotPreview({ projectId, snapshot, onUnauthorized }: {
  projectId: string; snapshot: SourceSnapshot; onUnauthorized: () => void;
}) {
  const [items, setItems] = useState<SnapshotItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [pages, setPages] = useState(0);
  const [pending, setPending] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setPending(true);
    setError("");
    listSnapshotItems(projectId, snapshot.id, cursor ?? undefined, controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setItems((current) => pages ? [...current, ...result.items] : result.items);
      setCursor(result.next_cursor);
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      else setError(error instanceof Error ? error.message : "读取弹幕失败");
    }).finally(() => {
      if (!controller.signal.aborted) setPending(false);
    });
    return () => controller.abort();
  }, [projectId, snapshot.id, pages, onUnauthorized]);

  return <>
    {error && <p role="alert" className="text-sm text-danger">{error}</p>}
    <div role="region" aria-label="弹幕快照预览" tabIndex={0}
      className="max-h-[23rem] overflow-auto overscroll-contain rounded-lg border border-line">
      <table className="w-full text-left text-sm">
        <thead className="sticky top-0 bg-panel-muted text-ink-secondary">
          <tr><th className="px-3 py-2">时间</th><th className="w-full px-3 py-2">弹幕</th>
            <th className="whitespace-nowrap px-3 py-2">模式</th></tr>
        </thead>
        <tbody className="divide-y divide-line-subtle">
          {items.map((item) => <tr key={item.id}>
            <td className="whitespace-nowrap px-3 py-2 tabular-nums">{formatDuration(item.time_ms)}</td>
            <td className="px-3 py-2 [overflow-wrap:anywhere]">
              <DanmakuText text={item.text} rgb={item.color_rgb} fontSize={item.font_size} />
            </td>
            <td className="whitespace-nowrap px-3 py-2">{danmakuModeLabel(item.mode)}</td>
          </tr>)}
        </tbody>
      </table>
      <div className="px-3 py-2 text-sm text-ink-muted">
        {pending ? <span role="status">正在读取弹幕…</span>
          : (cursor || error) ? <button className="secondary" onClick={() => setPages((value) => value + 1)}>
            {error ? "重试读取" : "加载更多弹幕"}
          </button> : items.length === 0 ? "此快照没有弹幕" : null}
      </div>
    </div>
  </>;
}

export function InputPanel({ project, csrf, snapshots, task, downloads, targetPart, onDownload, onMerged, onResume, onUnauthorized }: {
  project: Project; csrf: string; snapshots: SourceSnapshot[]; task?: Task; downloads: Task[]; targetPart?: VideoPart;
  onDownload: (source: DownloadSource) => Promise<Task>; onMerged: (snapshot: SourceSnapshot) => void;
  onResume: (task: Task) => Promise<void>; onUnauthorized: () => void;
}) {
  const { confirm } = useQuestion();
  const [rows, setRows] = useState<Array<{ id: number; inputType: "BV" | "CID"; cid: string; part?: VideoPart }>>(
    [{ id: 0, inputType: "BV", cid: "" }],
  );
  const [nextRowId, setNextRowId] = useState(1);
  const [rowTasks, setRowTasks] = useState<Record<number, string>>({});
  const [pendingRows, setPendingRows] = useState<number[]>([]);
  function updateRow(id: number, update: Partial<(typeof rows)[number]>) {
    setRows((current) => current.map((row) => row.id === id ? { ...row, ...update } : row));
  }
  const [previewId, setPreviewId] = useState("");
  const [pending, setPending] = useState(false);
  const [merging, setMerging] = useState(false);
  const [error, setError] = useState("");
  const [dismissedError, setDismissedError] = useState<Task>();
  const active = pending || (!!task && taskActive(task));
  const preview = snapshots.find((entry) => entry.id === previewId) ?? snapshots[0];
  const originals = snapshots.filter((entry) => entry.range_kind !== "merged");
  const taskError = pending || (task?.id === dismissedError?.id && task?.version === dismissedError?.version)
    ? null : task?.error;
  const errorText = error || (taskError && `${taskError.message}\n错误码 ${taskError.code}`);

  function report(error: unknown) {
    if (error instanceof ApiError && error.status === 401) onUnauthorized();
    else if (error instanceof ApiError) {
      const detail = error.code ? ` · 错误码 ${error.code}` : "";
      setError(`${error.message}\n本地 API HTTP ${error.status}${detail}`);
    }
    else setError(error instanceof Error ? error.message : "请求失败");
  }

  async function download(row?: (typeof rows)[number], resume?: Task) {
    const source = row?.inputType === "CID"
      ? (/^[1-9][0-9]{0,19}$/.test(row.cid) ? { source_cid: row.cid } : undefined)
      : (row?.part ? { source_bvid: row.part.bvid, source_page: row.part.page } : undefined);
    if (!resume && !source) return;
    if (row) setPendingRows((current) => [...current, row.id]);
    else setPending(true);
    setError("");
    try {
      if (!resume && row?.inputType === "BV" && row.part) {
        const target = targetPart ?? (await getVideoParts(project.target_bvid, "bilibili"))
          .find((entry) => entry.page === project.target_page);
        if (!target) throw new Error("找不到项目的目标分 P，无法比较视频时长");
        if (!await confirmDownloadDuration(row.part.duration_ms, target.duration_ms, (message) => confirm({
          title: "源视频与目标视频时长不同", message, confirmLabel: "继续下载",
        }))) return;
      }
      setDismissedError(task);
      if (resume) {
        await onResume(resume);
        setPreviewId(resume.id);
      } else if (source) {
        const download = await onDownload(source);
        if (row) setRowTasks((current) => ({ ...current, [row.id]: download.id }));
        setPreviewId(download.id);
      }
    } catch (error) {
      report(error);
    } finally {
      if (row) setPendingRows((current) => current.filter((id) => id !== row.id));
      else setPending(false);
    }
  }

  async function merge() {
    setMerging(true);
    setError("");
    const ids = originals.map((entry) => entry.id);
    try {
      let merged: SourceSnapshot;
      try {
        merged = await mergeSnapshots(project.id, ids, csrf);
      } catch (error) {
        if (!(error instanceof ApiError) || error.code !== "snapshot_duration_mismatch") throw error;
        if (!await confirm({
          title: "是否继续合并？", message: error.message, confirmLabel: "继续合并",
        })) return;
        merged = await mergeSnapshots(project.id, ids, csrf, true);
      }
      onMerged(merged);
      setPreviewId(merged.id);
    } catch (error) {
      report(error);
    } finally {
      setMerging(false);
    }
  }

  return <section aria-label="弹幕拉取" className="workflow-panel">
    <div className="space-y-2">{rows.map((row, index) => {
      const rowTask = downloads.find((entry) => entry.id === rowTasks[row.id]);
      const rowPending = pendingRows.includes(row.id) || (!!rowTask && taskActive(rowTask));
      const valid = row.inputType === "CID" ? /^[1-9][0-9]{0,19}$/.test(row.cid) : !!row.part;
      return <form key={row.id} className={`grid items-end gap-3 ${row.inputType === "CID"
        ? "sm:grid-cols-[auto_auto_minmax(9rem,18rem)_auto] sm:justify-start"
        : "sm:grid-cols-2 lg:grid-cols-[auto_auto_minmax(9rem,1fr)_auto_minmax(10rem,1.5fr)_auto]"}`}
        onSubmit={(event) => { event.preventDefault(); void download(row); }}>
        <div className="flex h-10 w-8 items-center justify-center"><button type="button" aria-label={index === 0 ? "新增输入行" : `删除输入行 ${index + 1}`}
          title={index === 0 ? "新增输入行" : "删除输入行"}
          className="grid size-[22px] place-items-center rounded-full border border-line-strong bg-panel text-ink-secondary shadow-sm transition-colors hover:border-accent-focus hover:bg-accent-tint hover:text-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent-focus disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-line-strong disabled:hover:bg-panel disabled:hover:text-ink-secondary"
          disabled={index === 0 ? rows.length >= 4 : rowPending}
          onClick={() => {
            if (index === 0) {
              setRows((current) => current.length >= 4 ? current : [...current, { id: nextRowId, inputType: "BV", cid: "" }]);
              setNextRowId((current) => current + 1);
            } else setRows((current) => current.filter((item) => item.id !== row.id));
          }}><svg aria-hidden="true" viewBox="0 0 16 16" className="size-3" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round">
            <path d="M3.5 8h9" />{index === 0 && <path d="M8 3.5v9" />}
          </svg></button></div>
        <div className="field"><span id={`input-type-${row.id}`} className={index === 0 ? "" : "sr-only"}>输入类型</span>
          <details className="input-type-menu" onKeyDown={(event) => {
            if (event.key === "Escape") { event.currentTarget.open = false; event.currentTarget.querySelector("summary")?.focus(); }
          }} onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) event.currentTarget.open = false; }}>
            <summary aria-labelledby={`input-type-${row.id}`} aria-disabled={rowPending}
              onClick={(event) => { if (rowPending) event.preventDefault(); }}>{row.inputType}</summary>
            <div className="input-type-options">{(["BV", "CID"] as const).map((type) =>
              <Tooltip key={type} text={type === "BV" ? "视频的BV号" : "具体到分p的视频唯一标识，即使视频已删除仍能获取到弹幕"}>
                <button type="button" onClick={(event) => {
                  updateRow(row.id, { inputType: type }); setError("");
                  const menu = event.currentTarget.closest("details");
                  if (menu) { menu.open = false; menu.querySelector("summary")?.focus(); }
                }}>{type}</button>
              </Tooltip>)}</div>
          </details>
        </div>
        <div className={row.inputType === "BV" ? "contents" : "hidden"}>
          <VideoPartPicker label="源视频" showLabels={index === 0} initialBvid={index === 0 ? project.source_bvid ?? "" : ""} part={row.part}
            initialPage={index === 0 ? project.source_page : undefined}
            disabled={rowPending || row.inputType !== "BV"}
            onPart={(part) => { updateRow(row.id, { part }); setError(""); }} onError={report} />
        </div>
        {row.inputType === "CID" && <label className="field"><span className={index === 0 ? "" : "sr-only"}>源视频 CID 号</span>
          <input value={row.cid} inputMode="numeric" maxLength={20} disabled={rowPending}
            onChange={(event) => { updateRow(row.id, { cid: event.currentTarget.value.trim() }); setError(""); }} />
        </label>}
        <button className="primary whitespace-nowrap" disabled={!valid || rowPending}>
          {rowPending && <Spinner />}{rowPending ? "弹幕下载中" : "下载当前弹幕"}
        </button>
      </form>;
    })}</div>
    {errorText && <p role="alert"
      className="whitespace-pre-wrap rounded-md border border-danger-line bg-danger-tint p-3 text-sm text-danger [overflow-wrap:anywhere]">
      {errorText}
    </p>}
    {task?.state === "paused" && <button className="secondary" disabled={active} onClick={() => void download(undefined, task)}>继续下载</button>}
    {preview && <>
      <div className="flex flex-wrap items-end justify-between gap-3 border-t border-line-subtle pt-4">
        {snapshots.length > 1 ? <label className="field flex-1">弹幕快照
          <select value={preview.id} onChange={(event) => setPreviewId(event.currentTarget.value)}>
            {snapshots.map((entry) => <option key={entry.id} value={entry.id}>{entry.name}</option>)}
          </select>
        </label> : <div className="min-w-0">
          <h3 className="font-semibold">弹幕快照</h3>
          <p className="break-words text-sm text-ink-muted">{preview.name}</p>
        </div>}
        {originals.length > 1 && <button className="secondary" disabled={merging} onClick={() => void merge()}>
          {merging ? "正在合并…" : "合并所有弹幕快照"}
        </button>}
      </div>
      <p className="text-sm text-ink-muted">共 {preview.item_count} 条弹幕</p>
      <SnapshotPreview key={preview.id} projectId={project.id} snapshot={preview} onUnauthorized={onUnauthorized} />
    </>}
  </section>;
}
