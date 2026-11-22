import { renderToStaticMarkup } from "react-dom/server";
import { expect, it, vi } from "vitest";
import type { Task } from "../src/api/workbench";
import { QuestionProvider } from "../src/components/QuestionDialog";
import { SendProgress } from "../src/features/SendProgress";
import { SendAttributes } from "../src/features/SendPreview";

vi.mock("../src/features/DeliveryPanel", () => ({
  DeliveryPanel: ({ dual }: { dual: boolean }) => <div data-dual={dual} />,
}));

const resumedTask: Task = {
  id: "synthetic-send", project_id: "synthetic-project", kind: "send", state: "queued",
  creation_order: 1, version: 4,
  progress: { done: 2, total: 5, failed: 0, skipped: 0, unknown: 0, withdrawn: 0 },
  checkpoint: { started: true }, next_attempt_at: "2099-01-01T00:00:00Z", error: null,
};
const limitError = { code: "send_rate_limited", message: "发送限流，等待重试", retryable: true };

it.each([
  ["waiting", "正在等待稿件公开"],
  ["checking", "核对计划公开时间"],
  ["blocked", "等待进一步操作"],
] as const)("shows publication status %s for a scheduled batch", (status, label) => {
  const task: Task = {
    ...resumedTask, checkpoint: { publication: "pending" }, next_attempt_at: null,
  };
  const markup = renderToStaticMarkup(<QuestionProvider>
    <SendProgress task={task} csrf="synthetic-csrf" publicationStatus={status}
      onTask={() => {}} onControl={() => {}} onUnauthorized={() => {}} />
  </QuestionProvider>);
  expect(markup).toContain(`<h3 class="font-semibold">${label}</h3>`);
});

it("shows the countdown until publication time and waits for dispatch", () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2099-01-01T00:00:00Z"));
  try {
    const task: Task = { ...resumedTask, checkpoint: { publication: "pending" }, next_attempt_at: null };
    const render = (status: "waiting" | "checking", publishAt: number) => renderToStaticMarkup(<QuestionProvider>
      <SendProgress task={task} csrf="synthetic-csrf" publicationStatus={status} publishAt={publishAt}
        onTask={() => {}} onControl={() => {}} onUnauthorized={() => {}} />
    </QuestionProvider>);
    const future = Date.now() / 1000 + 90061;
    expect(render("waiting", future)).toContain("预计公开时间：");
    expect(render("waiting", future)).toContain("还剩 1天 1时 1分 1秒");
    const due = render("waiting", Date.now() / 1000 - 1);
    expect(due).toContain('<h3 class="font-semibold">等待计划投递</h3>');
    expect(due).not.toContain("还剩");
    expect(render("checking", future)).not.toContain("预计公开时间：");
  } finally {
    vi.useRealTimers();
  }
});

it.each([
  ["actively sending", { ...resumedTask, state: "running", next_attempt_at: null }, "弹幕发送中"],
  ["resumed during a send interval", resumedTask, "弹幕发送中"],
  ["between sends", { ...resumedTask, state: "backoff" }, "弹幕发送中"],
  ["waiting on a rate limit", { ...resumedTask, state: "backoff", error: limitError }, "弹幕发送中"],
  ["paused during a send interval", { ...resumedTask, state: "paused" }, "已暂停"],
  ["waiting behind another batch", {
    ...resumedTask, checkpoint: { started: true, waiting_after_priority: true },
  }, "投递排队中"],
  ["waiting for the first send", {
    ...resumedTask, checkpoint: {}, next_attempt_at: null,
  }, "投递排队中"],
] as const)("shows the status for a batch %s", (_scenario, task, label) => {
  const markup = renderToStaticMarkup(<QuestionProvider>
    <SendProgress task={task} csrf="synthetic-csrf" onTask={() => {}} onControl={() => {}} onUnauthorized={() => {}} />
  </QuestionProvider>);
  expect(markup).toContain(`<h3 class="font-semibold">${label}</h3>`);
  if (task.next_attempt_at) expect(markup).toContain("下次尝试");
  if (task.error) expect(markup).toContain(task.error.message);
});

it("stacks account countdowns next to the send status and uses two delivery columns", () => {
  const task: Task = {
    ...resumedTask, state: "backoff",
    checkpoint: { started: true, accounts: {
      primary: { state: "waiting", next_attempt_at: "2099-01-01T00:00:05Z", error: limitError },
      secondary: { state: "waiting", next_attempt_at: "2099-01-01T00:00:10Z", error: null },
    } },
  };
  const markup = renderToStaticMarkup(<QuestionProvider>
    <SendProgress task={task} csrf="synthetic-csrf" onTask={() => {}} onControl={() => {}} onUnauthorized={() => {}} />
  </QuestionProvider>);
  expect(markup.match(/下次尝试/g)).toHaveLength(2);
  expect(markup).toMatch(/弹幕发送中<\/h3><div class="flex flex-col">.*主账号.*下次尝试.*发送限流，等待重试.*次账号.*下次尝试/);
  expect(markup).toMatch(/改为主账号发送.*取消该投递.*暂停/);
  expect(markup).toContain('data-dual="true"');
});

it("returns to one delivery column immediately when primary-only is selected", () => {
  const task: Task = {
    ...resumedTask, state: "running",
    checkpoint: { primary_only: true, accounts: {
      primary: { state: "ready", next_attempt_at: null, error: null },
      secondary: { state: "sending", next_attempt_at: null, error: null },
    } },
  };
  const markup = renderToStaticMarkup(<QuestionProvider>
    <SendProgress task={task} csrf="synthetic-csrf" onTask={() => {}} onControl={() => {}} onUnauthorized={() => {}} />
  </QuestionProvider>);
  expect(markup).toContain('data-dual="false"');
});

it.each([
  [undefined, "次账号发送"],
  ["primary", "主账号发送"],
] as const)("appends the planned or actual sender to the attributes (%s)", (accountRole, label) => {
  const markup = renderToStaticMarkup(<SendAttributes accountRole={accountRole} item={{
    item_id: "id:101", time_ms: 2000, text: "合成弹幕", font_size: 25, color_rgb: 0xFFFFFF, mode: 5,
    checkbox_type: 0, effect_differences: [], account_role: "secondary",
  }} />);
  expect(markup).toContain(`0分2秒 · 字号 25 · #ffffff · 顶部弹幕 · ${label}`);
});
