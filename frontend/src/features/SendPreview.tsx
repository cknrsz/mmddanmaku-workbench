import { useEffect, useRef, useState } from "react";
import {
  ApiError, BilibiliSettings, createSend, getBilibiliSettings, getSendPreview, SendItem,
  SendPreview as Preview, Task,
} from "../api/workbench";
import { useQuestion } from "../components/QuestionDialog";
import { formatDuration } from "../formatDuration";
import { danmakuColorLabel, danmakuModeLabel } from "./DanmakuAppearance";

const effects = {
  unsupported_mode: "模式不支持", unsupported_color: "颜色不支持", unsupported_font_size: "字号不支持",
};

export function SendAttributes({ item, accountRole = item.account_role }: {
  item: SendItem; accountRole?: SendItem["account_role"];
}) {
  return <span className="text-ink-muted">
    {item.time_ms < 0 ? `${item.time_ms} 毫秒` : formatDuration(item.time_ms)} · 字号 {item.font_size} · {danmakuColorLabel(item.color_rgb)} · {danmakuModeLabel(item.mode)}
    {item.checkbox_type === 4 && " · UP 主弹幕"}
    {item.effect_differences.map((effect) => <span key={effect} className="text-warning"> · {effects[effect]}</span>)}
    {accountRole && (accountRole === "secondary" ? " · 次账号发送" : " · 主账号发送")}
  </span>;
}

export function SendPreview({ projectId, runId, revision, csrf, sendInQueue, publishAt, onSendConflict, onCreated, onUnauthorized, onStale }: {
  projectId: string; runId: string; revision: number; csrf: string;
  sendInQueue: boolean; onSendConflict: () => void;
  publishAt?: number | null;
  onCreated: (task: Task) => void; onUnauthorized: () => void; onStale: () => void;
}) {
  const { confirm } = useQuestion();
  const [preview, setPreview] = useState<Preview>();
  const [settings, setSettings] = useState<BilibiliSettings>();
  const [confirmed, setConfirmed] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let pageCursor: string | undefined;
    async function loadPreview() {
      const first = await getSendPreview(projectId, runId, undefined, controller.signal);
      if (first.revision !== revision) return first;

      const items = [...first.items];
      let nextCursor = first.next_cursor;
      while (nextCursor && !controller.signal.aborted) {
        pageCursor = nextCursor;
        const page = await getSendPreview(projectId, runId, pageCursor, controller.signal);
        items.push(...page.items);
        nextCursor = page.next_cursor;
      }
      return { ...first, items, next_cursor: nextCursor };
    }
    loadPreview().then((result) => {
      if (!controller.signal.aborted) setPreview(result);
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      else if (error instanceof ApiError && error.status === 400 && pageCursor) onStale();
      else setError(error instanceof Error ? error.message : "读取投递预览失败");
    });
    return () => controller.abort();
  }, [projectId, runId, revision, refresh, onUnauthorized]);

  useEffect(() => {
    const controller = new AbortController();
    getBilibiliSettings(controller.signal).then((result) => {
      if (!controller.signal.aborted) setSettings(result);
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      else setError(error instanceof Error ? error.message : "读取 B 站账号设置失败");
    });
    return () => controller.abort();
  }, [refresh, onUnauthorized]);

  const accountId = settings?.configured ? settings.account_id : null;
  const canSubmit = !sendInQueue && !!preview && preview.revision === revision &&
    !!preview.selected_count && !preview.outside_count && preview.live_target && !!accountId && confirmed;

  useEffect(() => {
    if (sendInQueue) setConfirmed(false);
  }, [sendInQueue]);

  async function submit(primaryOnly: boolean) {
    if (!canSubmit || !preview || !accountId || pending) return;
    if (preview.duration_mismatch_message && !await confirm({
      title: "是否继续真实投递？", message: preview.duration_mismatch_message, confirmLabel: "继续投递",
    })) return;
    const confirmation = {
      target_cid: preview.target.cid, account_id: accountId,
      secondary_account_id: primaryOnly ? null : preview.secondary_account_id ?? null,
      secondary_level: primaryOnly ? null : preview.secondary_level ?? null,
      ...(primaryOnly ? { primary_only: true } : {}),
    };
    setPending(true);
    setError("");
    const accountKey = primaryOnly ? `${accountId}:primary-only` : confirmation.secondary_account_id
      ? `${accountId}:${confirmation.secondary_account_id}:${confirmation.secondary_level}` : accountId;
    const storageKey = `danmaku:send:${projectId}:${runId}:${revision}:live:${accountKey}`;
    try {
      const key = sessionStorage.getItem(storageKey) ?? crypto.randomUUID();
      sessionStorage.setItem(storageKey, key);
      const task = await createSend(projectId, runId, revision, key, csrf, confirmation);
      sessionStorage.removeItem(storageKey);
      if (mounted.current) onCreated(task);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      else if (error instanceof ApiError && error.code === "send_active") {
        sessionStorage.removeItem(storageKey);
        setConfirmed(false);
        if (mounted.current) onSendConflict();
      } else if (error instanceof ApiError && error.code === "revision_conflict") {
        sessionStorage.removeItem(storageKey);
        onStale();
      } else if (error instanceof ApiError && error.code === "target_confirmation_mismatch") {
        setError("目标分 P 与投递记录不符，请刷新页面后重新确认。");
      } else {
        setError(error instanceof Error ? error.message : "创建投递失败");
        if (error instanceof ApiError && ["bilibili_account_changed", "send_accounts_changed"].includes(error.code ?? "")) {
          sessionStorage.removeItem(storageKey);
          setPreview(undefined);
          setSettings(undefined);
          setConfirmed(false);
          setRefresh((value) => value + 1);
        }
      }
    } finally {
      setPending(false);
    }
  }

  return <section aria-label="投递预览" className="flex min-h-0 flex-1 flex-col items-start gap-3 rounded-lg border border-line bg-canvas p-4 text-sm">
    {error && <p role="alert" className="shrink-0 text-danger">{error}</p>}
    {!preview && (error ? <button className="secondary shrink-0" onClick={() => {
      setError("");
      setRefresh((value) => value + 1);
    }}>重新读取预览</button> : <p role="status">正在读取投递预览…</p>)}
    {preview && <>
      <div className="w-full shrink-0 space-y-2 break-words">
        {(preview.source_parts ?? [preview.source]).map((part, index, parts) => <p key={index}>
          <strong>源片{parts.length > 1 ? index + 1 : ""}</strong>{" "}
          {"bvid" in part
            ? `${part.bvid} P${part.page} · ${part.video_title?.trim() || part.title.trim() || "未记录标题"} · ${formatDuration(part.duration_ms)}`
            : `CID ${part.cid} · 片长未知`}
        </p>)}
        <p><strong>目标</strong> {preview.target.bvid} P{preview.target.page} · {preview.target.title} · {formatDuration(preview.target.duration_ms)}</p>
        {preview.duration_mismatch_message && <p className="text-warning">
          {preview.duration_mismatch_message}请核对弹幕是否对齐。
        </p>}
        <p>审核修订 {preview.revision} · 整个运行已选 {preview.selected_count} 条 · 效果不支持将计为失败 {preview.unsupported_count} 条</p>
        <p>重复投递将跳过 {preview.duplicate_count} 条 · 缺少稳定弹幕 ID 将计为失败 {preview.unstable_count} 条</p>
        {preview.outside_count > 0 && <p role="alert" className="text-danger">
          {preview.outside_count} 条已选弹幕超出视频时间轴。请返回审核调整偏移或取消勾选相关条目。
        </p>}
      </div>
      {preview.items.length > 0 && <ul
        aria-label={preview.outside_count ? "越界弹幕" : "真实投递已选弹幕"}
        tabIndex={0}
        className="min-h-16 max-h-[36rem] w-full space-y-2 overflow-y-auto overscroll-contain focus-visible:-outline-offset-2">
        {preview.items.map((item) => <li key={item.item_id} className="break-words rounded-md border border-line bg-panel px-3 py-2">
          <div>{item.text}</div><SendAttributes item={item} />
        </li>)}
      </ul>}
      <div className="w-full shrink-0 space-y-2">
        {preview.revision !== revision && <p role="alert" className="text-warning">审核版本已更新，请重新核对。
          <button className="secondary ml-2" onClick={onStale}>刷新审核</button>
        </p>}
        {!preview.live_target && <p>真实投递需要已解析的 B 站目标分 P。</p>}
        {sendInQueue ? <p role="status" className="text-warning">投递任务正在进行，请先取消该投递</p>
          : preview.live_target && (accountId ? <label className="flex items-start gap-2">
          <input type="checkbox" checked={confirmed} disabled={pending} onChange={(event) => setConfirmed(event.currentTarget.checked)} />
          <span>我确认使用 UID {accountId}
            {preview.secondary_account_id && ` 和次账号 UID ${preview.secondary_account_id}`}
            {" "}向 {preview.target.bvid} P{preview.target.page} 发送已选弹幕。</span>
        </label> : <p>{settings ? "请在右上角登录 B 站。" :
          "正在读取 B 站账号设置…"}</p>)}
        <div className="flex flex-wrap gap-2">
          <button className="primary" disabled={pending || !canSubmit}
            onClick={() => void submit(false)}>{pending ? "正在创建…" : publishAt ? "确认预装弹幕" : "确认并发送"}</button>
          <button className="secondary" disabled={pending || !canSubmit}
            onClick={() => void submit(true)}>{publishAt ? "使用主账号预装" : "使用主账号发送"}</button>
        </div>
        <p className="text-ink-muted">{publishAt
          ? `预计 ${new Date(publishAt * 1000).toLocaleString("zh-CN")} 公开，公开时间前 10 秒核对计划，公开时间后 5 秒开始投递。`
          : "投递默认按提交顺序执行。队首暂停时，排队批次仍可插队。"}</p>
      </div>
    </>}
  </section>;
}
