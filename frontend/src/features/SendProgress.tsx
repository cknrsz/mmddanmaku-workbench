import { useEffect, useState } from "react";
import { ApiError, awaitingSendDecision, cancelSendTask, controlTask, getActiveSendTaskId, prioritizeSendTask, Project, publicationLabels, SendAccountProgress, SendRate, Task, TaskState, terminal, usePrimarySendAccount } from "../api/workbench";
import { Connection, watchTask } from "../api/taskEvents";
import { useQuestion } from "../components/QuestionDialog";
import { formatDuration } from "../formatDuration";
import { DeliveryPanel } from "./DeliveryPanel";

const stateLabels: Record<TaskState, string> = {
  queued: "投递排队中", running: "弹幕发送中", backoff: "弹幕发送中", pausing: "正在暂停",
  paused: "已暂停", succeeded: "发送完成", failed: "发送失败",
  cancelling: "正在取消投递", cancelled: "取消投递",
  needs_review: "需人工核对",
};

const connectionLabels: Record<Connection, string> = {
  connecting: "正在连接进度", live: "实时进度", polling: "连接中断，正在轮询", finished: "任务已结束",
};

function RetryTime({ deadline }: { deadline: string }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [deadline]);
  const date = new Date(deadline);
  const remainingMs = Math.max(0, date.getTime() - now);
  return <span className="text-ink-muted">下次尝试 {date.toLocaleTimeString()} · 剩余 {formatDuration(remainingMs)}</span>;
}

function PublicationCountdown({ publishAt, now }: { publishAt: number; now: number }) {
  const date = new Date(publishAt * 1000);
  const seconds = Math.max(0, Math.ceil((date.getTime() - now) / 1000));
  const pad = (value: number) => String(value).padStart(2, "0");
  const time = `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  return <span className="text-sm text-ink-muted tabular-nums">
    预计公开时间：{time} · 还剩 {Math.floor(seconds / 86400)}天 {Math.floor(seconds % 86400 / 3600)}时 {Math.floor(seconds % 3600 / 60)}分 {seconds % 60}秒
  </span>;
}

function EstimatedRemainingTime({ remaining, rate }: { remaining: number | null; rate: number }) {
  const [remainingMs, setRemainingMs] = useState<number | null>(null);

  useEffect(() => {
    const estimateMs = remaining !== null && rate > 0 ? remaining * 3_600_000 / rate : null;
    setRemainingMs(estimateMs);
    if (estimateMs === null || estimateMs === 0) return;

    const deadline = performance.now() + estimateMs;
    const timer = window.setInterval(() => {
      const next = Math.max(0, deadline - performance.now());
      setRemainingMs(next);
      if (next === 0) window.clearInterval(timer);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [remaining, rate]);

  return <span className="ml-auto whitespace-nowrap tabular-nums">
    预计剩余时间：{remainingMs === null ? "暂不可用" : formatDuration(remainingMs)}
  </span>;
}

function AccountStatus({ label, status, paused }: { label: string; status: SendAccountProgress; paused: boolean }) {
  return <span className="inline-flex items-baseline gap-1.5 text-[0.8125rem]/4">
    <span>{label} · {status.state === "finished" ? "已完成" : paused ? "已暂停" : status.state === "sending" ? "发送中" :
      status.state === "ready" ? "待发送" : ""}</span>
    {!paused && status.next_attempt_at && <RetryTime deadline={status.next_attempt_at} />}
    {status.error && <span className="text-warning">{status.error.message}</span>}
  </span>;
}

export function SendProgress({ task, csrf, sendRate, publicationStatus, publishAt, onTask, onControl, onUnauthorized }: {
  sendRate?: SendRate | null;
  task: Task; csrf: string; onTask: (task: Task) => void; onControl: () => void; onUnauthorized: () => void;
  publicationStatus?: Project["publication_status"];
  publishAt?: Project["preload_publish_at"];
}) {
  const { confirm } = useQuestion();
  const [initial, setInitial] = useState(task);
  const [connection, setConnection] = useState<Connection>("connecting");
  const [pending, setPending] = useState(false);
  const [activeSendId, setActiveSendId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [now, setNow] = useState(Date.now);
  const waitingToSend = task.state === "queued" && !!task.next_attempt_at &&
    task.checkpoint.started && !task.checkpoint.waiting_after_priority;
  const inQueue = task.state === "queued" && !waitingToSend && task.checkpoint.publication !== "pending";

  useEffect(() => watchTask(initial, onTask, setConnection, onUnauthorized), [initial, onUnauthorized]);

  useEffect(() => {
    if (!inQueue) {
      setActiveSendId(null);
      return;
    }
    const controller = new AbortController();
    let timer: number;
    async function refresh() {
      try {
        const id = await getActiveSendTaskId(controller.signal);
        if (!controller.signal.aborted) setActiveSendId(id);
      } catch (error) {
        if (controller.signal.aborted) return;
        setActiveSendId(null);
        if (error instanceof ApiError && error.status === 401) onUnauthorized();
      }
      if (!controller.signal.aborted) timer = window.setTimeout(refresh, 3000);
    }
    void refresh();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [inQueue, task.version, onUnauthorized]);

  const decision = awaitingSendDecision(task);
  const action = decision ? undefined : task.state === "paused" ? "resume" :
    ["queued", "running", "backoff"].includes(task.state) ? "pause" : undefined;
  const state = decision ? "等待处理失败弹幕"
    : publicationStatus && (task.checkpoint.publication === "pending" || task.checkpoint.publication === "dispatch")
      ? publicationLabels[publicationStatus]
    : waitingToSend ? stateLabels.running : stateLabels[task.state];
  const publicationWaiting = state === publicationLabels.waiting && publishAt != null && Number.isFinite(publishAt);
  useEffect(() => {
    if (!publicationWaiting) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [publicationWaiting, publishAt]);
  const publicationDue = publicationWaiting && now >= publishAt * 1000;
  const { primary, secondary } = task.checkpoint.accounts ?? {};
  const dual = !!secondary && !task.checkpoint.primary_only;
  const sending = state === stateLabels.running;
  const paused = task.state === "paused";
  const pausing = task.state === "pausing";
  const accountStatus = primary && secondary && (
    (sending && (dual || secondary.state === "sending")) || (dual && (pausing || paused))
  );
  const primarySending = sending && primary?.state !== "finished";
  const secondarySending = sending && !!secondary && secondary.state !== "finished" && !task.checkpoint.primary_only;
  useEffect(() => {
    if (sending) onControl();
  }, [sending, primarySending, secondarySending, onControl]);

  const rate = sendRate?.task_id === task.id ? sendRate.items_per_hour : 0;
  const { total, done, unknown } = task.progress;
  const remaining = total === null ? null : Math.max(0, total - done - unknown);

  const canCancel = !terminal(task) && task.state !== "cancelling";

  const canUsePrimary = !!task.checkpoint.secondary_upgrade_id || (!!secondary && !task.checkpoint.primary_only);

  async function prioritize() {
    try {
      return await prioritizeSendTask(task, csrf);
    } catch (reason) {
      if (!(reason instanceof ApiError) || reason.code !== "preload_priority_confirmation") throw reason;
      const head = await getActiveSendTaskId();
      if (head && await confirm({
        title: "预装弹幕任务正在进行，是否仍要插队？", confirmLabel: "仍要插队",
      })) return prioritizeSendTask(task, csrf, head);
    }
  }

  async function control(command: "pause" | "resume" | "prioritize" | "cancel" | "primary-only") {
    if (command === "primary-only" && !await confirm({
      title: "确定改为主账号发送？",
      message: "此批次剩余待发弹幕将由主账号发送。已发出的请求会继续等待回执。",
      confirmLabel: "改为主账号发送",
    })) return;
    if (command === "cancel" && !await confirm({
      title: "确定取消投递？", message: "尚未发送的待投递弹幕将归入「撤销」。", confirmLabel: "取消投递",
    })) return;
    setPending(true);
    setError("");
    try {
      const next = command === "primary-only" ? await usePrimarySendAccount(task, csrf) :
        command === "cancel" ? await cancelSendTask(task, csrf) :
        command === "prioritize" ? await prioritize() : await controlTask(task, command, csrf);
      if (!next) return;
      if (command === "prioritize") setActiveSendId(null);
      onTask(next);
      onControl();
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      else setError(error instanceof Error ? error.message : "修改发送状态失败");
    } finally {
      setPending(false);
    }
  }

  return <section aria-label="发送进度" className="flex min-h-0 flex-1 flex-col gap-3 overflow-x-clip rounded-xl border border-line bg-panel p-3 sm:p-4">
    <div className="flex shrink-0 flex-wrap items-center justify-between gap-2">
      <div className={accountStatus
        ? "flex min-w-0 items-center gap-3 overflow-x-auto overflow-y-hidden whitespace-nowrap [scrollbar-width:none]"
        : "flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1"}>
        <h3 className="font-semibold">{publicationDue ? "等待计划投递" : state}</h3>
        {publicationWaiting && !publicationDue && <PublicationCountdown publishAt={publishAt} now={now} />}
        {accountStatus ? <div className="flex flex-col">
          <AccountStatus label="主账号" status={primary} paused={paused || (pausing && primary.state !== "sending")} />
          <AccountStatus label="次账号" status={secondary} paused={paused || (pausing && secondary.state !== "sending")} />
        </div> : task.next_attempt_at && <span className="text-sm"><RetryTime deadline={task.next_attempt_at} /></span>}
        {task.error && <p className="min-w-0 break-words text-sm text-warning">{task.error.message}</p>}
        {error && <p role="alert" className="min-w-0 break-words text-sm text-danger">{error}</p>}
      </div>
      {canCancel && <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
        {canUsePrimary && <button className="secondary py-1.5" disabled={pending}
          onClick={() => void control("primary-only")}>改为主账号发送</button>}
        <button className="secondary py-1.5 text-danger" disabled={pending} onClick={() => void control("cancel")}>取消该投递</button>
        {inQueue && activeSendId && activeSendId !== task.id &&
          <button className="secondary py-1.5" disabled={pending} onClick={() => void control("prioritize")}>插队发送</button>}
        {action && <button className="secondary py-1.5" disabled={pending}
          onClick={() => void control(action)}>{action === "pause" ? "暂停" : "恢复"}</button>}
      </div>}
    </div>
    <progress aria-label="发送进度" value={task.progress.done}
      max={Math.max(1, task.progress.total ?? 1)} className="block h-2 w-full shrink-0 accent-action" />
    <p className="flex shrink-0 flex-wrap justify-between gap-x-3 gap-y-1 text-sm text-ink-secondary" role="status">
      <span>
        {done} / {total ?? "待定"} 条 · {connectionLabels[connection]}
        {" · "}{done - task.progress.failed - task.progress.skipped - task.progress.withdrawn} 条成功
        {" · "}{task.progress.failed} 条失败 · {task.progress.skipped} 条跳过 · {task.progress.withdrawn} 条撤销 · {unknown} 条待核对
      </span>
      {sending && <EstimatedRemainingTime remaining={remaining} rate={rate} />}
    </p>
    <DeliveryPanel dual={dual} task={task} csrf={csrf} onTask={onTask}
      onRetry={(next) => { onTask(next); setInitial(next); }} onUnauthorized={onUnauthorized} />
  </section>;
}
