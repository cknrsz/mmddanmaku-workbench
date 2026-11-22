import { Pagination } from "./Pagination";
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import {
  ApiError, awaitingSendDecision, BilibiliAccountRole, changeDelivery, continueDeliveryQueue, Delivery, DeliveryFilter, DeliveryPage, DeliveryResolution, DeliveryStatus,
  getTask, listDeliveries, resolveDelivery, retryFailedDeliveries, Task,
} from "../api/workbench";
import { useQuestion } from "../components/QuestionDialog";
import { SendAttributes } from "./SendPreview";
import { useRowsPerPage } from "./useRowsPerPage";

const maxTrackedRows = 10;
const deliveryRowGapRem = 0.5;
const statuses: Record<DeliveryStatus, string> = {
  pending: "待投递", in_flight: "发送中", confirmed: "发送成功", failed: "失败", skipped: "已跳过", unknown: "待核对",
  withdrawn: "撤销",
};
type StatusFilter = Exclude<DeliveryFilter, "pending"> | "";

function readStatusFilter(key: string): StatusFilter | undefined {
  const saved = sessionStorage.getItem(key);
  if (saved === "pending" || saved === "active") return "active";
  return saved !== null && (saved === "" || Object.hasOwn(statuses, saved)) ? saved as StatusFilter : undefined;
}

type DeliveryMotion = {
  kind: "sent" | "returned" | "failed" | "withdrawn";
  row: Delivery;
  fromSending: boolean;
  wasDizzy: boolean;
  position: number;
};
type DeliveryView = { page?: DeliveryPage; motions: Map<string, DeliveryMotion> };

function hasDeliveryError(row: Delivery): boolean {
  if (row.status === "confirmed" || row.status === "skipped" || row.status === "withdrawn") return false;
  return row.status === "failed" || row.status === "unknown" || row.attempts.some((attempt) =>
    attempt.finished_at !== null && attempt.outcome !== "confirmed");
}

export function updateDeliveryView(
  current: DeliveryView, page: DeliveryPage, animate: boolean, withdrawingId?: string,
): DeliveryView {
  if (!current.page) return { page, motions: new Map() };

  const visible = new Map(page.items.map((row) => [row.id, row]));
  const tracked = new Map(page.tracked_items?.map((row) => [row.id, row]));
  const motions = new Map(current.motions);
  for (const [id, motion] of motions) {
    const row = visible.get(id);
    if (motion.kind === "withdrawn" && (!row || row.status === "withdrawn")) continue;
    if (motion.kind === "returned" ? row?.status !== "pending" : row) motions.delete(id);
  }

  for (const [position, previous] of current.page.items.entries()) {
    const row = visible.get(previous.id) ?? tracked.get(previous.id);
    if (previous.id === withdrawingId && row?.status === "withdrawn" && previous.status === "pending") {
      motions.set(row.id, { kind: "withdrawn", row, fromSending: false, wasDizzy: hasDeliveryError(previous), position });
      continue;
    }
    if (!animate) continue;
    const attempt = row?.attempts.at(-1);
    const before = previous.attempts.at(-1);
    if (!row || !attempt || !attempt.finished_at ||
      (attempt.number === before?.number && attempt.outcome === before?.outcome)) continue;

    const fromSending = previous.status === "in_flight";
    const wasDizzy = hasDeliveryError(previous);
    if (row.status === "confirmed" && attempt.outcome === "confirmed") {
      motions.set(row.id, { kind: "sent", row: { ...previous, status: row.status }, fromSending, wasDizzy, position });
    } else if (attempt.outcome !== "confirmed" && row.status !== "skipped" && row.status !== "withdrawn") {
      if (visible.has(row.id)) motions.set(row.id, { kind: "returned", row, fromSending, wasDizzy, position });
      else if (row.status === "failed" || row.status === "unknown") {
        motions.set(row.id, { kind: "failed", row: { ...previous, status: row.status }, fromSending, wasDizzy, position });
      }
    }
  }
  return { page, motions };
}

const attemptLabels = {
  in_flight: "发送中", confirmed: "已确认", rejected: "明确拒绝", limited: "限流", blocked: "发送暂停", unknown: "结果未知",
};
const resolutionLabels = { confirmed: "人工确认", skip: "人工跳过", retry: "人工允许重发" };
const errorLabels: Record<string, string> = {
  delivery_unknown: "回执未确认", delivery_rejected: "发送器明确拒绝", send_rate_limited: "发送限流",
  unsupported_effect: "效果不支持", manual_skip: "人工跳过", duplicate_live_delivery: "已有真实投递记录",
  unstable_source: "缺少稳定弹幕 ID", bilibili_delivery_rejected: "B 站明确拒绝", bilibili_video_unavailable: "B 站视频不可用",
  bilibili_credential_missing: "登录凭据缺失", bilibili_credential_rejected: "B 站未确认登录",
  bilibili_send_disabled: "真实投递权限已关闭", bilibili_account_changed: "B 站账号已更换",
  credential_store_unavailable: "无法读取 B 站登录凭据", bilibili_access_denied: "B 站拒绝发送或要求验证",
  bilibili_sdk_missing: "缺少 B 站接入依赖",
};

function DeliveryError({ error_code: code, bilibili_error: bilibili }: Pick<Delivery, "error_code" | "bilibili_error">) {
  if (!code && !bilibili) return null;
  return <div className="break-words text-warning">
    {code && <p>错误码 <code>{code}</code>{errorLabels[code] && ` · ${errorLabels[code]}`}</p>}
    {bilibili && <p>
      {bilibili.code !== null && <>B 站错误码 <code>{bilibili.code}</code> · </>}
      HTTP {bilibili.http_status}
      {bilibili.message && ` · ${bilibili.message}`}
    </p>}
  </div>;
}

function deliveryStatusLabel(row: Delivery) {
  return row.resending && row.status === "pending" ? "等待重发" : statuses[row.status];
}

function DeliveryDetails({ row, trigger, onClose }: { row: Delivery; trigger: HTMLButtonElement; onClose: () => void }) {
  const close = useRef<HTMLButtonElement>(null);
  const content = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const workspace = trigger.closest("main");
    if (workspace) workspace.inert = true;
    close.current?.focus();
    return () => {
      if (workspace) workspace.inert = false;
      trigger.focus({ preventScroll: true });
    };
  }, [trigger]);

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-scrim p-4 sm:p-8"
      onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section role="dialog" aria-modal="true" aria-labelledby="delivery-details-title"
        className="flex max-h-full w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-line bg-panel shadow-xl"
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.stopPropagation();
            onClose();
          }
          if (event.key === "Tab" && event.target === (event.shiftKey ? close.current : content.current)) {
            event.preventDefault();
            (event.shiftKey ? content.current : close.current)?.focus();
          }
        }}>
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-line bg-canvas px-4 py-3 sm:px-6">
          <h2 id="delivery-details-title" className="text-lg font-semibold">投递详细信息</h2>
          <button ref={close} type="button" className="secondary py-1.5" onClick={onClose}>关闭</button>
        </header>
        <div ref={content} tabIndex={0} role="region" aria-label="弹幕与投递记录"
          className="min-h-0 space-y-4 overflow-y-auto overscroll-contain break-words p-4 text-sm focus-visible:-outline-offset-2 sm:p-6">
          <div className="space-y-2 rounded-lg border border-line bg-canvas px-4 py-3">
            <span className="inline-block rounded-md bg-accent-selected px-2 py-0.5 text-xs font-medium text-accent-strong">
              {deliveryStatusLabel(row)}
            </span>
            <p className="whitespace-pre-wrap leading-relaxed">{row.item.text}</p>
            <p className="text-xs"><SendAttributes item={row.item} /></p>
          </div>
          <DeliveryError error_code={row.error_code} bilibili_error={row.bilibili_error} />
          {row.receipt && <p className="break-all text-ink-secondary">回执 {row.receipt}</p>}
          {row.delivered_at && <p className="text-ink-secondary">确认时间 {new Date(row.delivered_at).toLocaleString()}</p>}
          {row.attempts.length > 0 && <section aria-labelledby="delivery-attempts-title">
            <h3 id="delivery-attempts-title" className="mb-2 font-semibold">尝试记录（{row.attempts.length}）</h3>
            <ol className="divide-y divide-line rounded-lg border border-line">
              {row.attempts.map((attempt) => <li key={attempt.number} className="space-y-1 px-4 py-3">
                <p className="font-medium">第 {attempt.number} 次 · {attemptLabels[attempt.outcome]}</p>
                <p className="text-xs text-ink-muted">
                  {new Date(attempt.started_at).toLocaleString()}
                  {attempt.finished_at && ` → ${new Date(attempt.finished_at).toLocaleTimeString()}`}
                </p>
                <DeliveryError error_code={attempt.error_code} bilibili_error={attempt.bilibili_error} />
                {attempt.resolution && <p>
                  {attempt.resolution === "retry" && !attempt.resolved_by ? "自动重试" : resolutionLabels[attempt.resolution]}
                  {attempt.resolved_by && <> · {attempt.resolved_by}</>}
                  {attempt.resolved_at && ` · ${new Date(attempt.resolved_at).toLocaleString()}`}</p>}
              </li>)}
            </ol>
          </section>}
        </div>
      </section>
    </div>,
    document.body,
  );
}

type DeliveryPanelProps = {
  task: Task; csrf: string; onTask: (task: Task) => void; onRetry: (task: Task) => void; onUnauthorized: () => void;
};

export function DeliveryPanel({ dual, ...props }: DeliveryPanelProps & { dual: boolean }) {
  const [pending, setPending] = useState(false);
  const actions = { pending, setPending };

  return <div className={`grid min-h-0 flex-1 gap-3 border-t pt-3 ${dual ? "grid-cols-2" : "grid-cols-1"}`}>
    <DeliveryColumn key={dual ? "primary" : "all"} role={dual ? "primary" : undefined} {...props} {...actions} />
    {dual && <DeliveryColumn key="secondary" role="secondary" {...props} {...actions} />}
  </div>;
}

function DeliveryColumn({ task, csrf, onTask, onRetry, onUnauthorized, role, pending, setPending }: DeliveryPanelProps & {
  role?: BilibiliAccountRole; pending: boolean; setPending: (pending: boolean) => void;
}) {
  const { confirm } = useQuestion();
  const reviewAvailable = task.progress.unknown > 0;
  const sharedStatusKey = `danmaku:delivery-status:${task.project_id}`;
  const statusStorageKey = role ? `${sharedStatusKey}:${role}` : sharedStatusKey;
  const accountLabel = role === "secondary" ? "次账号" : role === "primary" ? "主账号" : "";
  const title = `${accountLabel}投递明细`;
  const [{ status, remembered }, setFilter] = useState(() => {
    const saved = readStatusFilter(statusStorageKey);
    const initial: StatusFilter = saved ?? (reviewAvailable ? "unknown" : "active");
    return { status: initial, remembered: saved !== undefined };
  });
  const [pageIndex, setPageIndex] = useState(0);
  const [details, setDetails] = useState<{ row: Delivery; trigger: HTMLButtonElement }>();
  const [{ page, motions }, setView] = useState<DeliveryView>(() => ({ motions: new Map() }));
  const visibleIds = useRef<string[]>([]);
  const loadedRange = useRef<string>();
  const withdrawingId = useRef<string>();
  const [refresh, setRefresh] = useState(0);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const { viewport, size: pageSize, rowHeight } = useRowsPerPage(
    6, { limit: 500, trailingGapRem: deliveryRowGapRem },
  );
  const cardHeight = `calc(${rowHeight} - var(--delivery-row-gap))`;
  const pageNumber = Math.floor(pageIndex / pageSize) + 1;
  const totalPages = Math.ceil((page?.total_count ?? 0) / pageSize);
  const currentPage = Math.min(pageNumber, Math.max(1, totalPages));
  const detailsId = details?.row.id;
  const withdrawing = [...motions.values()].some((motion) => motion.kind === "withdrawn");
  const totalCount = page?.total_count;

  const [mode, setMode] = useState<DeliveryPage["batch"]["mode"]>();
  const resendView = status === "withdrawn" || status === "skipped" || status === "failed";
  const animate = status === "active" || resendView;
  const freshPage = page?.task_version === task.version;
  const needsDecision = awaitingSendDecision(task);
  const canRetry = needsDecision || (mode === "live" && task.state === "failed" && task.progress.failed > 0);

  useEffect(() => {
    if (totalCount !== undefined && !withdrawing) {
      setPageIndex((index) => Math.min(index, Math.max(0, totalCount - 1)));
    }
  }, [totalCount, withdrawing]);

  useEffect(() => {
    if (remembered || resendView || (!reviewAvailable && !canRetry)) return;
    const next = reviewAvailable ? "unknown" : "failed";
    if (status === next || (status === "active" && (motions.size ||
      (!loadError && (!page || page.task_version < task.version))))) return;
    setStatus(next);
    move(1);
  }, [remembered, canRetry, reviewAvailable, task.version, status, resendView, page, motions, loadError]);

  function setStatus(next: StatusFilter) {
    setFilter((current) => ({ ...current, status: next }));
  }

  useEffect(() => {
    const controller = new AbortController();
    setLoadError("");
    const withdrawal = withdrawingId.current;
    const track = [...new Set([...(withdrawal ? [withdrawal] : []), ...(detailsId ? [detailsId] : []),
      ...(animate ? visibleIds.current : [])])]
      .slice(0, maxTrackedRows);
    const key = `${status}:${pageNumber}:${pageSize}`;
    listDeliveries(task.id, status, pageNumber, pageSize, controller.signal, track, role).then((result) => {
      if (!controller.signal.aborted) {
        const inFlight = result.items.filter((row) => row.status === "in_flight");
        visibleIds.current = [...inFlight, ...result.items].map((row) => row.id);
        const sameRange = loadedRange.current === key;
        loadedRange.current = key;
        setView((current) => sameRange
          ? updateDeliveryView(current, result, animate, withdrawal)
          : { page: result, motions: new Map() });
        if (withdrawal && withdrawingId.current === withdrawal &&
          (result.items.some((row) => row.id === withdrawal && row.status === "withdrawn") ||
            result.tracked_items?.some((row) => row.id === withdrawal && row.status === "withdrawn"))) {
          withdrawingId.current = undefined;
        }
        setDetails((current) => {
          if (!current) return current;
          const row = result.items.find((row) => row.id === current.row.id) ??
            result.tracked_items?.find((row) => row.id === current.row.id);
          return row ? { ...current, row } : current;
        });
        setMode(result.batch.mode);
      }
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      else setLoadError(error instanceof Error ? error.message : "读取投递明细失败");
    });
    return () => controller.abort();
  }, [task.id, task.version, status, pageNumber, pageSize, refresh, animate, detailsId, onUnauthorized, role]);

  async function changePendingDelivery(row: Delivery, action: "withdraw" | "resend") {
    if (pending || !freshPage) return;
    const resend = action === "resend";
    if (resend && !await confirm({
      title: task.state === "paused" ? "重新发送这条弹幕并恢复本批次？" : "重新发送这条弹幕？",
      message: task.state === "paused" ? "本批次其他待投递弹幕也会继续发送。" : undefined,
      confirmLabel: task.state === "paused" ? "重发并恢复" : "重新发送",
    })) return;
    setPending(true);
    setActionError("");
    if (!resend) withdrawingId.current = row.id;
    try {
      const next = await changeDelivery(task.id, row.id, task.version, action, csrf);
      if (resend) onRetry(next);
      else onTask(next);
    } catch (error) {
      if (withdrawingId.current === row.id) withdrawingId.current = undefined;
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      else setActionError(error instanceof Error ? error.message : resend ? "重新发送失败" : "撤销发送失败");
      if (error instanceof ApiError && error.status === 409) {
        const latest = await getTask(task.id).catch(() => undefined);
        if (latest) onRetry(latest);
      }
    } finally {
      setRefresh((value) => value + 1);
      setPending(false);
    }
  }

  async function resolve(id: string, outcome: Exclude<DeliveryResolution, "retry">) {
    if (!page || pending) return;
    setPending(true);
    setActionError("");
    try {
      await resolveDelivery(task.id, id, Math.max(task.version, page.task_version), outcome, csrf);
      const next = await getTask(task.id);
      const resumed = task.state === "paused" && ["queued", "running", "backoff"].includes(next.state);
      if (status === "unknown" && next.progress.unknown === 0) {
        setStatus(resumed ? "active" : outcome === "skip" ? "skipped" : "confirmed");
        move(1);
      }
      if (resumed) onRetry(next);
      else onTask(next);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      else {
        setActionError(error instanceof Error ? error.message : "保存核对结果失败");
        if (error instanceof ApiError && error.status === 409) {
          const latest = await getTask(task.id).catch(() => undefined);
          if (latest) onRetry(latest);
        }
      }
    } finally {
      setRefresh((value) => value + 1);
      setPending(false);
    }
  }

  async function decide(action: "retry" | "continue") {
    setPending(true);
    setActionError("");
    try {
      if (action === "retry") {
        const next = await retryFailedDeliveries(task, csrf);
        setStatus("active");
        move(1);
        onRetry(next);
      } else {
        onTask(await continueDeliveryQueue(task, csrf));
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      else setActionError(error instanceof Error ? error.message : "保存投递决定失败");
      if (error instanceof ApiError && error.status === 409) {
        const latest = await getTask(task.id).catch(() => undefined);
        if (latest) onRetry(latest);
      }
    } finally {
      setPending(false);
    }
  }

  function move(next: number) {
    visibleIds.current = [];
    loadedRange.current = undefined;
    withdrawingId.current = undefined;
    setView({ motions: new Map() });
    setActionError("");
    setPageIndex((next - 1) * pageSize);
  }

  function finishMotion(id: string, motion: DeliveryMotion) {
    setView((current) => {
      if (current.motions.get(id) !== motion) return current;
      const remaining = new Map(current.motions);
      remaining.delete(id);
      return { ...current, motions: remaining };
    });
  }

  const visibleRows = (page?.items ?? []).map((row) => ({ row, motion: motions.get(row.id) }));
  const visibleRowIds = new Set(visibleRows.map(({ row }) => row.id));
  for (const motion of [...motions.values()].sort((left, right) => left.position - right.position)) {
    if (motion.kind !== "returned" && !visibleRowIds.has(motion.row.id)) {
      visibleRows.splice(motion.position, 0, { row: motion.row, motion });
    }
  }

  return <section aria-label={title} className="flex min-h-0 min-w-0 flex-col gap-2.5 text-sm">
    <div className="flex shrink-0 flex-wrap items-center justify-between gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <h4 className="font-semibold">{title}</h4>
        {role !== "secondary" && canRetry && <button className="primary" disabled={pending}
          onClick={() => decide("retry")}>{needsDecision ? "先重试失败弹幕" : "一键重试"}（{task.progress.failed} 条）</button>}
        {role !== "secondary" && needsDecision && <button className="secondary" disabled={pending}
          onClick={() => decide("continue")}>继续下一批</button>}
      </div>
      <label className="ml-auto flex items-center gap-2 text-sm">投递状态
        <select className="rounded-md border border-line-strong bg-panel px-2.5 py-1.5"
          aria-label={role ? `${accountLabel}投递状态` : undefined}
          value={status} disabled={pending} onChange={(event) => {
          const next = event.currentTarget.value as StatusFilter;
          sessionStorage.setItem(statusStorageKey, next);
          setFilter({ status: next, remembered: true });
          move(1);
        }}>
          <option value="">全部</option>
          <option value="active">待投递</option>
          {Object.entries(statuses).filter(([value]) => value !== "pending").map(([value, label]) =>
            <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
    </div>
    {(loadError || actionError) && <p role="alert" className="shrink-0 text-danger">{loadError || actionError}</p>}
    <div ref={viewport} className="flex-1 [overflow-y:clip]" style={{
      "--delivery-row-gap": `${deliveryRowGapRem}rem`, minHeight: visibleRows.length ? cardHeight : 0,
    } as CSSProperties}>
      {!page && !loadError && <p data-backend-notice role="status">正在读取投递明细…</p>}
      {page && !loadError && visibleRows.length === 0 && <p data-backend-notice>没有符合条件的投递条目。</p>}
      {page && visibleRows.length > 0 && <ul className="delivery-list" data-account={role}>
        {visibleRows.map(({ row, motion }) => <li key={row.id} className="delivery-row"
          data-motion={motion?.kind} data-from-sending={motion?.fromSending} data-was-dizzy={motion?.wasDizzy}
          data-stays={motion?.kind === "withdrawn" && visibleRowIds.has(row.id)}
          onAnimationEnd={(event) => {
            if (motion && event.target === event.currentTarget && event.animationName === "delivery-collapse") {
              finishMotion(row.id, motion);
            }
          }}>
          <div className="delivery-row-clip">
            <div className="delivery-card flex flex-col justify-center gap-1.5 rounded-lg border border-line bg-canvas/40 px-3 sm:px-4"
              style={{ height: cardHeight }}
              data-sending={animate && row.status === "in_flight"}
              onAnimationEnd={(event) => {
                if (motion?.kind === "returned" && event.target === event.currentTarget &&
                  event.animationName === "delivery-rebound") finishMotion(row.id, motion);
                if (motion?.kind === "withdrawn" && visibleRowIds.has(row.id) && event.target === event.currentTarget &&
                  event.animationName === "delivery-withdraw") finishMotion(row.id, motion);
              }}>
              <div className="flex min-w-0 items-center gap-3">
                <div className="min-w-0 flex-1 space-y-0.5">
                  <p className="truncate" title={row.item.text}><strong>{deliveryStatusLabel(row)}</strong>
                    {" · "}{row.item.text}
                    {(motion?.kind === "sent" || motion?.kind === "withdrawn"
                      ? motion.wasDizzy : Boolean(motion) || hasDeliveryError(row)) &&
                      <span className="delivery-dizzy" aria-hidden="true" />}
                  </p>
                  <p className="truncate text-xs"><SendAttributes item={row.item}
                    accountRole={row.status === "pending"
                      ? task.checkpoint.primary_only ? "primary" : row.item.account_role ?? "primary"
                      : row.attempts.at(-1)?.account_role ?? row.item.account_role ??
                        (row.attempts.length ? "primary" : null)} /></p>
                  {!motion && <button type="button" className="block rounded text-xs leading-5 text-accent hover:underline"
                    aria-haspopup="dialog" onClick={(event) => setDetails({ row, trigger: event.currentTarget })}>
                    详细信息
                  </button>}
                </div>
                {!motion && mode === "live" && row.status === "pending" && <button type="button"
                  className="secondary shrink-0 px-2.5 py-1.5 text-xs" disabled={pending || !freshPage}
                  onClick={() => void changePendingDelivery(row, "withdraw")}>撤销发送</button>}
                {!motion && resendView && mode === "live" && row.status === status && <button type="button"
                  className="primary shrink-0 px-2.5 py-1.5 text-xs"
                  disabled={pending || !freshPage || task.state === "pausing" || task.state === "cancelling" || task.progress.unknown > 0}
                  onClick={() => void changePendingDelivery(row, "resend")}>重新发送</button>}
                {!motion && row.status === "unknown" && <div className="flex shrink-0 gap-1.5">
                  <button type="button" className="secondary whitespace-nowrap px-2.5 py-1.5 text-xs"
                    disabled={pending} onClick={() => void resolve(row.id, "confirmed")}>人工确认</button>
                  <button type="button" className="secondary whitespace-nowrap px-2.5 py-1.5 text-xs"
                    disabled={pending} onClick={() => void resolve(row.id, "skip")}>人工跳过</button>
                </div>}
              </div>
            </div>
          </div>
        </li>)}
      </ul>}
    </div>
    {details && <DeliveryDetails {...details} onClose={() => setDetails(undefined)} />}
    <div className="min-h-[1.875rem] shrink-0">
      {totalPages > 0 && <Pagination label={`${title}分页`} page={currentPage} totalPages={totalPages}
        disabled={pending} onPageChange={move} />}
    </div>
  </section>;
}
