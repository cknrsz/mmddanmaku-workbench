import { afterEach, expect, it, vi } from "vitest";
import { watchAnalysisConsole, watchTask } from "../src/api/taskEvents";
import {
  cancelBilibiliLogin, cancelSendTask, changeDelivery, clearSubtitleLanguage, continueDeliveryQueue, controlTask, createAnalysis, createProject,
  createPreloadProject, createSend, createTask, decideOverduePublication,
  deleteProject, getActiveSendTaskId, getAnalysisRun, getBilibiliLogin, getModelSettings, getPendingSendDecision, getSendPreview, getSendTiming, getSettings,
  getInterruptedSendCount, getOverduePublication, getSendConnection, resumeInterruptedSends,
  listClassifications, listDeliveries, listPreloadParts, listProjects, listReview, listSnapshotItems, listTasks, loadReviewResults,
  logoutBilibili, markReviewReady, mergeSnapshots, openLocalSession, pollBilibiliLogin, prioritizeSendTask, resolveDelivery,
  retryFailedDeliveries, saveSettings, selectSnapshot, startBilibiliLogin,
  analysisStopped, saveModelSettings, saveSendTiming, setProjectCompletion, stopAnalysis, Task, taskActive,
  deleteSubtitle, updateReview, updateReviewOffset, updateReviewText, uploadSubtitle,
} from "../src/api/workbench";

const initial: Task = {
  id: "synthetic-task", project_id: "synthetic-project", kind: "download", state: "running",
  creation_order: 1, version: 2, progress: { done: 0, total: 50, failed: 0, skipped: 0, unknown: 0, withdrawn: 0 },
  checkpoint: { completed_batches: 0 }, next_attempt_at: null, error: null,
};

class FakeSource extends EventTarget {
  static current: FakeSource;
  onopen: (() => void) | undefined;
  onerror: (() => void) | undefined;
  close = vi.fn();

  constructor(readonly url: string) {
    super();
    FakeSource.current = this;
  }

  send(name: string, data: unknown) {
    this.dispatchEvent(new MessageEvent(name, { data: JSON.stringify(data) }));
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it("submits the edited review text with its item and expected revision", async () => {
  const commit = { revision: 5, changed_count: 1, selected_count: 3 };
  const fetch = vi.fn().mockResolvedValue(Response.json(commit));
  vi.stubGlobal("fetch", fetch);
  await expect(updateReviewText("project", "run", 4, "item", "合成改写", "csrf")).resolves.toEqual(commit);
  const [[url, init]] = fetch.mock.calls;
  expect(url).toBe("/api/v1/projects/project/review/text");
  expect(init.method).toBe("PATCH");
  expect(init.headers.get("X-CSRF-Token")).toBe("csrf");
  expect(JSON.parse(init.body)).toEqual({ run_id: "run", expected_revision: 4, item_id: "item", text: "合成改写" });
});

it("reads the latest sending connection status without caching", async () => {
  const fetch = vi.fn().mockResolvedValue(Response.json(false));
  vi.stubGlobal("fetch", fetch);
  const { signal } = new AbortController();
  await expect(getSendConnection(signal)).resolves.toBe(false);
  expect(fetch).toHaveBeenCalledWith("/api/v1/send-queue/connection", { signal, cache: "no-store" });
});

it("reads preloading options and submits only the selected part or overdue decision", async () => {
  const fetch = vi.fn().mockImplementation(() => Promise.resolve(Response.json(null)));
  vi.stubGlobal("fetch", fetch);
  const part = { bvid: "BV0000000002", page: 2, cid: "123", duration_ms: 15000, title: "合成分 P" };
  const { signal } = new AbortController();
  await listPreloadParts();
  await createPreloadProject(part, "csrf");
  await getOverduePublication(signal);
  await decideOverduePublication("earliest-task", false, "csrf");
  await prioritizeSendTask(initial, "csrf", "active-preload");
  const [list, create, overdue, decide, prioritize] = fetch.mock.calls;
  expect(list).toEqual(["/api/v1/preload/parts", { cache: "no-store" }]);
  expect(create[0]).toBe("/api/v1/preload/projects");
  expect(JSON.parse(create[1].body)).toEqual({ bvid: part.bvid, page: part.page });
  expect(overdue).toEqual(["/api/v1/preload/overdue", { signal, cache: "no-store" }]);
  expect(decide[0]).toBe("/api/v1/preload/overdue");
  expect(JSON.parse(decide[1].body)).toEqual({ task_id: "earliest-task", send: false });
  expect(JSON.parse(prioritize[1].body)).toEqual({ expected_version: initial.version, confirmed_preload_task_id: "active-preload" });
  for (const [, init] of [create, decide, prioritize]) expect(init.headers.get("X-CSRF-Token")).toBe("csrf");
});

it.each([
  ["BV", { source_bvid: "BV0000000001", source_page: 2 }],
  ["CID", { source_cid: "9007199254740993" }],
] as const)("creates a target-only project and supplies the %s source on the download task", async (_type, source) => {
  const fetch = vi.fn().mockImplementation(() => Promise.resolve(Response.json({})));
  vi.stubGlobal("fetch", fetch);
  const target = { target_bvid: "BV0000000002", target_page: 1 };
  await createProject(target, "csrf", "bilibili");
  await createTask("project", "download-key", "csrf", "bilibili", source);
  const [creation, download] = fetch.mock.calls;
  expect(creation[0]).toBe("/api/v1/projects?source=bilibili");
  expect(JSON.parse(creation[1].body)).toEqual(target);
  expect(download[0]).toBe("/api/v1/projects/project/tasks");
  expect(JSON.parse(download[1].body)).toEqual({ kind: "download", mode: "bilibili", ...source });
  expect(download[1].headers.get("Idempotency-Key")).toBe("download-key");
});

it("uses scoped snapshot reads and writes, including the reviewed revision and merge confirmation", async () => {
  const fetch = vi.fn().mockImplementation(() => Promise.resolve(Response.json({})));
  vi.stubGlobal("fetch", fetch);
  await listSnapshotItems("project", "first", "opaque=cursor");
  await mergeSnapshots("project", ["first", "second"], "csrf", true);
  await selectSnapshot("project", "merged", "csrf");
  await markReviewReady("project", { run_id: "analysis", revision: 3 }, "csrf");
  await clearSubtitleLanguage("project", "chinese", "csrf");
  const [read, merge, select, review, clear] = fetch.mock.calls;
  expect(read[0]).toBe("/api/v1/projects/project/snapshots/first/items?cursor=opaque%3Dcursor");
  expect(merge[0]).toBe("/api/v1/projects/project/snapshots/merge");
  expect(select[0]).toBe("/api/v1/projects/project/workflow/snapshot");
  expect(review[0]).toBe("/api/v1/projects/project/workflow/review");
  expect(clear[0]).toBe("/api/v1/projects/project/workflow/subtitles/chinese");
  expect(clear[1].method).toBe("DELETE");
  expect(JSON.parse(merge[1].body)).toEqual({ snapshot_ids: ["first", "second"], confirm_duration_mismatch: true });
  expect(JSON.parse(select[1].body)).toEqual({ snapshot_id: "merged" });
  expect(JSON.parse(review[1].body)).toEqual({ run_id: "analysis", revision: 3 });
  expect([merge, select, review, clear].map(([, init]) => init.headers.get("X-CSRF-Token")))
    .toEqual(["csrf", "csrf", "csrf", "csrf"]);
});

it("opens a local session without a pairing code", async () => {
  const fetch = vi.fn().mockResolvedValue(Response.json({ csrf_token: "synthetic-csrf" }));
  vi.stubGlobal("fetch", fetch);
  vi.stubGlobal("navigator", {});
  await expect(openLocalSession()).resolves.toEqual({ csrf_token: "synthetic-csrf" });
  const [path, init] = fetch.mock.calls[0];
  expect(path).toBe("/api/v1/session");
  expect(init.method).toBe("POST");
  expect(init.headers).toEqual({ "X-Danmaku-Local": "1" });
  expect(init.body).toBeUndefined();
});

it("deletes a project with CSRF and accepts an empty response", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetch);
  await expect(deleteProject("synthetic-project", "synthetic-csrf")).resolves.toBeUndefined();
  const [path, init] = fetch.mock.calls[0];
  expect(path).toBe("/api/v1/projects/synthetic-project");
  expect(init.method).toBe("DELETE");
  expect(init.headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(init.body).toBeUndefined();
});

it("sets project completion to the confirmed value with CSRF", async () => {
  const activity = { stage: "send", running: false };
  const fetch = vi.fn()
    .mockResolvedValueOnce(Response.json({ id: initial.project_id, completed: true, activity }))
    .mockResolvedValueOnce(Response.json({ id: initial.project_id, completed: false, activity }));
  vi.stubGlobal("fetch", fetch);
  for (const completed of [true, false]) {
    await expect(setProjectCompletion(initial.project_id, completed, "synthetic-csrf"))
      .resolves.toMatchObject({ completed, activity });
    const [path, init] = fetch.mock.lastCall!;
    expect(path).toBe(`/api/v1/projects/${initial.project_id}/completion`);
    expect(init.method).toBe("PATCH");
    expect(init.headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
    expect(JSON.parse(init.body)).toEqual({ completed });
  }
});

it("reads distinct activity for each project from the project list", async () => {
  const items = [
    { id: "synthetic-download", creation_order: 2, completed: false, activity: { stage: "download", running: true } },
    { id: "synthetic-review", creation_order: 1, completed: false, activity: { stage: "review", running: false } },
  ];
  const fetch = vi.fn().mockResolvedValue(Response.json({ items, next_cursor: null }));
  vi.stubGlobal("fetch", fetch);
  await expect(listProjects()).resolves.toMatchObject({ items });
});

it("deletes a project subtitle with CSRF and reports analysis conflicts", async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce(new Response(null, { status: 204 }))
    .mockResolvedValueOnce(Response.json({ error: { code: "subtitle_in_use", message: "字幕已被引用" } }, { status: 409 }));
  vi.stubGlobal("fetch", fetch);
  await expect(deleteSubtitle("synthetic-project", "synthetic-subtitle", "synthetic-csrf")).resolves.toBeUndefined();
  const [path, init] = fetch.mock.calls[0];
  expect(path).toBe("/api/v1/projects/synthetic-project/subtitles/synthetic-subtitle");
  expect(init.method).toBe("DELETE");
  expect(init.headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(init.body).toBeUndefined();
  await expect(deleteSubtitle("synthetic-project", "synthetic-subtitle", "synthetic-csrf"))
    .rejects.toMatchObject({ status: 409, code: "subtitle_in_use", message: "字幕已被引用" });
});

it("checks the saved Bilibili login without sending credentials", async () => {
  const status = { status: "renewal_required", profile: null, auto_refresh: false };
  const fetch = vi.fn().mockResolvedValue(Response.json(status));
  vi.stubGlobal("fetch", fetch);
  const signal = new AbortController().signal;
  await expect(getBilibiliLogin("synthetic-csrf", signal)).resolves.toEqual(status);
  const [path, init] = fetch.mock.calls[0];
  expect(path).toBe("/api/v1/bilibili/login/status");
  expect(init.method).toBe("POST");
  expect(init.headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(init.body).toBeUndefined();
  expect(init.signal).toBe(signal);
});

it.each(["primary", "secondary"] as const)("scopes the Bilibili QR flow and logout for the %s account", async (role) => {
  const status = { status: "unconfigured", profile: null, auto_refresh: false };
  const fetch = vi.fn()
    .mockResolvedValueOnce(Response.json(status))
    .mockResolvedValueOnce(Response.json({ id: "synthetic-attempt", image: "data:image/png;base64,AAAA" }, { status: 201 }))
    .mockResolvedValueOnce(Response.json({ status: "scanned", login: null }))
    .mockImplementation(() => Promise.resolve(new Response(null, { status: 204 })));
  vi.stubGlobal("fetch", fetch);
  const csrf = "synthetic-csrf";
  const suffix = role === "secondary" ? "?role=secondary" : "";
  await expect(getBilibiliLogin(csrf, undefined, role)).resolves.toEqual(status);
  const qr = await startBilibiliLogin(csrf, undefined, role);
  await expect(pollBilibiliLogin(qr.id, csrf, undefined, role)).resolves.toEqual({ status: "scanned", login: null });
  await cancelBilibiliLogin(qr.id, csrf, role);
  await logoutBilibili(csrf, role);
  expect(fetch.mock.calls.map(([path, init]) => [path, init.method])).toEqual([
    [`/api/v1/bilibili/login/status${suffix}`, "POST"],
    [`/api/v1/bilibili/login/qr${suffix}`, "POST"],
    [`/api/v1/bilibili/login/qr/synthetic-attempt/poll${suffix}`, "POST"],
    [`/api/v1/bilibili/login/qr/synthetic-attempt${suffix}`, "DELETE"],
    [`/api/v1/bilibili/login/${suffix}`, "DELETE"],
  ]);
  for (const [, init] of fetch.mock.calls) {
    expect(init.headers.get("X-CSRF-Token")).toBe(csrf);
    expect(init.body).toBeUndefined();
  }
});

it("can bypass the cached secondary login status", async () => {
  const status = { status: "renewal_required", profile: null, auto_refresh: false };
  const fetch = vi.fn().mockResolvedValue(Response.json(status));
  vi.stubGlobal("fetch", fetch);
  await expect(getBilibiliLogin("synthetic-csrf", undefined, "secondary", true)).resolves.toEqual(status);
  expect(fetch.mock.calls[0][0]).toBe("/api/v1/bilibili/login/status?role=secondary&force=true");
});

it("sends live account and target confirmation and saves model and timing settings", async () => {
  const fetch = vi.fn().mockImplementation(() => Promise.resolve(Response.json({ configured: true })));
  vi.stubGlobal("fetch", fetch);
  await getSettings();
  await saveSettings({
    llm: { provider: "openai_compatible", base_url: "https://api.deepseek.com", model: "deepseek-flash", remove_api_key: true },
    sending: { item_min_seconds: 5, item_max_seconds: 7, rate_limit_seconds: 180 },
  }, "synthetic-csrf");
  await getSendPreview(initial.project_id, "synthetic-run");
  await createSend(initial.project_id, "synthetic-run", 3, "synthetic-key", "synthetic-csrf",
    { target_cid: "72002", account_id: "17001" });
  const [[read, get], [settings, put], [preview], [, send]] = fetch.mock.calls;
  expect(read).toBe("/api/v1/settings");
  expect(get.body).toBeUndefined();
  expect(settings).toBe(read);
  expect(put.method).toBe("PUT");
  expect(put.headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(JSON.parse(put.body)).toMatchObject({
    llm: { remove_api_key: true }, sending: { rate_limit_seconds: 180 },
  });
  expect(preview).toContain("limit=500");
  expect(send.headers.get("Idempotency-Key")).toBe("synthetic-key");
  expect(JSON.parse(send.body)).toEqual({
    kind: "send", run_id: "synthetic-run", expected_review_revision: 3,
    confirmation: { target_cid: "72002", account_id: "17001" },
  });
});

it("forwards the idempotency key, CSRF token and expected version", async () => {
  const fetch = vi.fn().mockImplementation(() => Promise.resolve(Response.json(initial)));
  vi.stubGlobal("fetch", fetch);
  await createTask(initial.project_id, "synthetic-key", "synthetic-csrf");
  await controlTask(initial, "pause", "synthetic-csrf");

  const [submit, pause] = fetch.mock.calls;
  expect(submit[0]).toBe("/api/v1/projects/synthetic-project/tasks");
  expect(submit[1].headers.get("Idempotency-Key")).toBe("synthetic-key");
  expect(submit[1].headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(JSON.parse(submit[1].body)).toEqual({ kind: "download" });
  expect(pause[0]).toBe("/api/v1/tasks/synthetic-task/pause");
  expect(pause[1].headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(JSON.parse(pause[1].body)).toEqual({ expected_version: 2 });
});

it("requests the frozen Bilibili batch and uses scoped delivery filters and versioned resolution", async () => {
  const fetch = vi.fn().mockImplementation(() => Promise.resolve(Response.json(initial)));
  vi.stubGlobal("fetch", fetch);
  await getSendPreview("synthetic-project", "synthetic-run", "page=2");
  const confirmation = { target_cid: "72002", account_id: "17001" };
  await createSend("synthetic-project", "synthetic-run", 7, "send-key", "synthetic-csrf", confirmation);
  await listDeliveries("synthetic-task", "unknown", 2, 10);
  await resolveDelivery("synthetic-task", "synthetic-delivery", 13, "retry", "synthetic-csrf");

  const [preview, create, ledger, resolve] = fetch.mock.calls;
  expect(preview[0]).toBe("/api/v1/projects/synthetic-project/send-preview?run_id=synthetic-run&limit=500&cursor=page%3D2");
  expect(create[0]).toBe("/api/v1/projects/synthetic-project/tasks");
  expect(create[1].headers.get("Idempotency-Key")).toBe("send-key");
  expect(create[1].headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(JSON.parse(create[1].body)).toEqual({ kind: "send", run_id: "synthetic-run", expected_review_revision: 7, confirmation });
  expect(ledger[0]).toBe("/api/v1/tasks/synthetic-task/deliveries?page=2&limit=10&status=unknown");
  expect(resolve[0]).toBe("/api/v1/tasks/synthetic-task/deliveries/synthetic-delivery/resolve");
  expect(resolve[1].headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(JSON.parse(resolve[1].body)).toEqual({ expected_version: 13, outcome: "retry" });
});

it("posts a versioned retry for failed deliveries in the original batch", async () => {
  const fetch = vi.fn().mockResolvedValue(Response.json(initial));
  vi.stubGlobal("fetch", fetch);
  await retryFailedDeliveries({ ...initial, kind: "send", state: "failed" }, "synthetic-csrf");
  const [url, options] = fetch.mock.calls[0];
  expect(url).toBe("/api/v1/tasks/synthetic-task/deliveries/retry-failed");
  expect(options.method).toBe("POST");
  expect(options.headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(JSON.parse(options.body)).toEqual({ expected_version: initial.version });
});

it("withdraws and requeues a delivery with the current version and tracks it in the withdrawn filter", async () => {
  const fetch = vi.fn().mockImplementation(() => Promise.resolve(Response.json(initial)));
  vi.stubGlobal("fetch", fetch);
  const id = "000000000001";
  await changeDelivery(initial.id, id, 2, "withdraw", "synthetic-csrf");
  await changeDelivery(initial.id, id, 3, "resend", "synthetic-csrf");
  await listDeliveries(initial.id, "withdrawn", 1, 10, undefined, [id], "secondary");

  const [withdraw, resend, list] = fetch.mock.calls;
  expect(list[0]).toBe(`/api/v1/tasks/${initial.id}/deliveries?page=1&limit=10&status=withdrawn&account_role=secondary&track=${id}`);
  for (const [call, action, version] of [[withdraw, "withdraw", 2], [resend, "resend", 3]] as const) {
    expect(call[0]).toBe(`/api/v1/tasks/${initial.id}/deliveries/${id}/${action}`);
    expect(call[1].method).toBe("POST");
    expect(call[1].headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
    expect(JSON.parse(call[1].body)).toEqual({ expected_version: version });
  }
});

it("reads the active send task and requests priority with the queued task version", async () => {
  const queued = { ...initial, kind: "send", state: "queued" } satisfies Task;
  const fetch = vi.fn()
    .mockResolvedValueOnce(Response.json("synthetic-active-task"))
    .mockResolvedValueOnce(Response.json({ ...queued, version: queued.version + 1 }));
  vi.stubGlobal("fetch", fetch);
  expect(await getActiveSendTaskId()).toBe("synthetic-active-task");
  expect((await prioritizeSendTask(queued, "synthetic-csrf")).version).toBe(queued.version + 1);
  expect(fetch.mock.calls[0][0]).toBe("/api/v1/send-queue/active");
  const [url, init] = fetch.mock.calls[1];
  expect(url).toBe("/api/v1/tasks/synthetic-task/send-priority");
  expect(init.method).toBe("POST");
  expect(init.headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(JSON.parse(init.body)).toEqual({ expected_version: queued.version });
});

it("requests send cancellation with the current version and treats it as terminal", async () => {
  const task = { ...initial, kind: "send", state: "queued" } satisfies Task;
  const cancelled = { ...task, state: "cancelled", version: task.version + 1 } satisfies Task;
  const fetch = vi.fn().mockResolvedValue(Response.json(cancelled));
  vi.stubGlobal("fetch", fetch);

  expect(await cancelSendTask(task, "synthetic-csrf")).toEqual(cancelled);
  const [url, init] = fetch.mock.calls[0];
  expect(url).toBe("/api/v1/tasks/synthetic-task/send-cancel");
  expect(init.method).toBe("POST");
  expect(init.headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(JSON.parse(init.body)).toEqual({ expected_version: task.version });
  expect(taskActive(cancelled)).toBe(false);
});

it("reads the queue decision and posts the selected batch version to continue", async () => {
  const paused = { ...initial, kind: "send", state: "paused" } satisfies Task;
  const fetch = vi.fn()
    .mockResolvedValueOnce(Response.json(paused))
    .mockResolvedValueOnce(Response.json({ ...paused, state: "failed" }));
  vi.stubGlobal("fetch", fetch);
  expect(await getPendingSendDecision()).toEqual(paused);
  await continueDeliveryQueue(paused, "synthetic-csrf");

  const [read, [url, options]] = fetch.mock.calls;
  expect(read[0]).toBe("/api/v1/send-queue/pending-decision");
  expect(url).toBe("/api/v1/tasks/synthetic-task/deliveries/continue-queue");
  expect(options.method).toBe("POST");
  expect(options.headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(JSON.parse(options.body)).toEqual({ expected_version: paused.version });
});

it("reads the interrupted send count and resumes the queue with CSRF", async () => {
  const fetch = vi.fn().mockResolvedValueOnce(Response.json(2)).mockResolvedValueOnce(Response.json(2));
  vi.stubGlobal("fetch", fetch);
  const { signal } = new AbortController();

  expect(await getInterruptedSendCount(signal)).toBe(2);
  expect(await resumeInterruptedSends("synthetic-csrf")).toBe(2);
  const [[read, readOptions], [write, writeOptions]] = fetch.mock.calls;
  expect(read).toBe("/api/v1/send-queue/interrupted");
  expect(readOptions.signal).toBe(signal);
  expect(write).toBe("/api/v1/send-queue/resume-interrupted");
  expect(writeOptions.method).toBe("POST");
  expect(writeOptions.headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(writeOptions.body).toBeUndefined();
});

it("reapplies an explicit mock pause against a refreshed running version", async () => {
  const sending = { ...initial, kind: "send", checkpoint: { batch_id: initial.id } } satisfies Task;
  const paused = { ...sending, version: 9, state: "pausing" };
  const fetch = vi.fn()
    .mockResolvedValueOnce(Response.json({ error: { code: "version_conflict" } }, { status: 409 }))
    .mockResolvedValueOnce(Response.json({ ...sending, version: 8 }))
    .mockResolvedValueOnce(Response.json(paused));
  vi.stubGlobal("fetch", fetch);
  expect(await controlTask(sending, "pause", "synthetic-csrf")).toEqual(paused);
  expect(fetch.mock.calls[1][0]).toBe(`/api/v1/tasks/${sending.id}`);
  expect(JSON.parse(fetch.mock.calls[2][1].body)).toEqual({ expected_version: 8 });
});

it("replays from the provided version, ignores stale events and closes at the terminal event", () => {
  vi.stubGlobal("EventSource", FakeSource);
  const onTask = vi.fn();
  const onConnection = vi.fn();
  watchTask(initial, onTask, onConnection, vi.fn());
  const source = FakeSource.current;
  expect(source.url).toBe("/api/v1/tasks/synthetic-task/events?after=2");
  source.onopen?.();

  source.send("task.updated", initial);
  const paused = { ...initial, state: "paused", version: 3 } satisfies Task;
  source.send("task.updated", paused);
  source.send("task.updated", initial);
  const final = { ...initial, state: "succeeded", version: 14 } satisfies Task;
  source.send("reset", final);
  expect(onTask.mock.calls).toEqual([[paused], [final]]);
  expect(onConnection.mock.calls).toEqual([["connecting"], ["live"], ["finished"]]);
  expect(source.close).toHaveBeenCalledOnce();
});

it("receives analysis text and reconnect snapshots through the task event stream", () => {
  vi.stubGlobal("EventSource", FakeSource);
  const onUpdate = vi.fn();
  const onConnection = vi.fn();
  watchAnalysisConsole(initial.id, onUpdate, onConnection);
  const source = FakeSource.current;
  expect(source.url).toBe("/api/v1/tasks/synthetic-task/events?analysis_console=true");
  const first = { replace: true, reasoning: "合成", output: "" };
  const delta = { replace: false, reasoning: "思考", output: '{"items":[]}' };
  source.onopen?.();
  source.send("analysis.console", first);
  source.send("analysis.console", delta);
  source.onerror?.();
  source.onopen?.();
  const replay = { replace: true, reasoning: "合成思考", output: delta.output };
  source.send("analysis.console", replay);
  source.send("analysis.console.end", {});
  expect(onUpdate.mock.calls).toEqual([[first], [delta], [replay]]);
  expect(onConnection.mock.calls).toEqual([["live"], ["disconnected"], ["live"], ["finished"]]);
  expect(source.close).toHaveBeenCalledOnce();
});

it("requests stop for the current analysis and recognizes its stopped state", async () => {
  const stopped = { ...initial, kind: "classify", state: "failed", version: 3,
    error: { code: "analysis_stopped", message: "分析已停止", retryable: false } } satisfies Task;
  const fetch = vi.fn().mockResolvedValue(Response.json(stopped, { status: 202 }));
  vi.stubGlobal("fetch", fetch);
  const result = await stopAnalysis(stopped.id, "synthetic-csrf");
  const [url, options] = fetch.mock.calls[0];
  expect(url).toBe("/api/v1/tasks/synthetic-task/stop");
  expect(options.method).toBe("POST");
  expect(options.headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(options.body).toBeUndefined();
  expect(result).toEqual(stopped);
  expect(analysisStopped(result)).toBe(true);
  expect(taskActive(result)).toBe(false);
});

it("polls on a disconnected stream and stops polling after EventSource reconnects", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("EventSource", FakeSource);
  const update = { ...initial, version: 3 };
  const fetch = vi.fn().mockImplementation(() => Promise.resolve(Response.json(update)));
  vi.stubGlobal("fetch", fetch);
  const onTask = vi.fn();
  const onConnection = vi.fn();
  const close = watchTask(initial, onTask, onConnection, vi.fn());
  const source = FakeSource.current;
  source.onerror?.();
  await vi.advanceTimersByTimeAsync(0);
  expect(fetch).toHaveBeenCalledOnce();
  expect(onTask).toHaveBeenCalledWith(update);
  expect(onConnection).toHaveBeenLastCalledWith("polling");
  source.onopen?.();
  await vi.advanceTimersByTimeAsync(4000);
  expect(fetch).toHaveBeenCalledOnce();
  expect(onConnection).toHaveBeenLastCalledWith("live");
  expect(source.close).not.toHaveBeenCalled();
  close();
  expect(source.close).toHaveBeenCalledOnce();
});

it("stops polling and requests reconnection on an expired session", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("EventSource", FakeSource);
  const fetch = vi.fn().mockResolvedValue(Response.json({}, { status: 401 }));
  vi.stubGlobal("fetch", fetch);
  const onUnauthorized = vi.fn();
  watchTask(initial, vi.fn(), vi.fn(), onUnauthorized);
  FakeSource.current.onerror?.();
  await vi.advanceTimersByTimeAsync(4000);
  expect(fetch).toHaveBeenCalledOnce();
  expect(FakeSource.current.close).toHaveBeenCalledOnce();
  expect(onUnauthorized).toHaveBeenCalledOnce();
});


it.each(["srt", "txt"] as const)("phase 2 client submits live mode and uploads original %s bytes", async (format) => {
  const response = () => Promise.resolve(new Response("{}", { status: 201 }));
  const mockedFetch = vi.fn<typeof fetch>().mockImplementation(response);
  vi.stubGlobal("fetch", mockedFetch);

  await createTask(initial.project_id, "synthetic-key", "synthetic-csrf", "bilibili");
  expect(JSON.parse(String(mockedFetch.mock.calls[0][1]?.body))).toEqual({
    kind: "download", mode: "bilibili",
  });
  const file = new File(["1\n00:00:01,000 --> 00:00:02,000\n合成"], `synthetic.${format}`);
  await uploadSubtitle(initial.project_id, file, format, "utf-8", "synthetic-csrf");
  const [url, options] = mockedFetch.mock.calls[1];
  expect(url).toBe(`/api/v1/projects/synthetic-project/subtitles?format=${format}&encoding=utf-8&filename=synthetic.${format}`);
  expect(new Headers(options?.headers).get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(new Headers(options?.headers).get("Content-Type")).toBe("application/octet-stream");
  expect(options?.body).toBe(file);
});

it("sends model credentials only on settings writes and pins analysis inputs", async () => {
  const mockedFetch = vi.fn<typeof fetch>().mockImplementation(async () => Response.json({}));
  vi.stubGlobal("fetch", mockedFetch);
  await getModelSettings();
  await saveModelSettings({
    provider: "openai_compatible", base_url: "https://model.example.invalid/v1",
    model: "synthetic-model", api_key: "synthetic-credential",
    thinking_enabled: false, reasoning_effort: "low", extra_requirements: "合成要求",
  }, "synthetic-csrf");
  await createAnalysis("p1", "analysis-key", "synthetic-csrf", {
    snapshot_id: "s1", subtitle_version_id: "u1", japanese_subtitle_version_id: "u2", llm_config_version: 4,
  });
  await getAnalysisRun("p1", "r1");
  await listClassifications("p1", "r1", "next=page");
  await listTasks("p1", "next=page", "classify");
  const [read, save, submit, run, rows, tasks] = mockedFetch.mock.calls;
  expect(read[0]).toBe("/api/v1/settings/llm");
  expect(read[1]?.body).toBeUndefined();
  expect(save[0]).toBe("/api/v1/settings/llm");
  expect(save[1]?.method).toBe("PUT");
  expect(new Headers(save[1]?.headers).get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(JSON.parse(String(save[1]?.body))).toMatchObject({
    api_key: "synthetic-credential", thinking_enabled: false, reasoning_effort: "low", extra_requirements: "合成要求",
  });
  expect(submit[0]).toBe("/api/v1/projects/p1/tasks");
  expect(new Headers(submit[1]?.headers).get("Idempotency-Key")).toBe("analysis-key");
  expect(new Headers(submit[1]?.headers).get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(JSON.parse(String(submit[1]?.body))).toEqual({
    kind: "classify", snapshot_id: "s1", subtitle_version_id: "u1",
    japanese_subtitle_version_id: "u2", llm_config_version: 4,
  });
  expect(run[0]).toBe("/api/v1/projects/p1/runs/r1");
  expect(rows[0]).toBe("/api/v1/projects/p1/runs/r1/classifications?cursor=next%3Dpage");
  expect(tasks[0]).toBe("/api/v1/projects/p1/tasks?cursor=next%3Dpage&kind=classify");
});

it("updates send timing and explicitly removes a saved model key", async () => {
  const mockedFetch = vi.fn<typeof fetch>().mockImplementation(async () => Response.json({}));
  vi.stubGlobal("fetch", mockedFetch);
  const timing = { item_min_seconds: 3, item_max_seconds: 5, rate_limit_seconds: 45 };
  await getSendTiming();
  await saveSendTiming(timing, "synthetic-csrf");
  await saveModelSettings({
    provider: "openai_compatible", base_url: "https://api.deepseek.com", model: "deepseek-flash", remove_api_key: true,
  }, "synthetic-csrf");
  const [read, save, remove] = mockedFetch.mock.calls;
  expect(read[0]).toBe("/api/v1/settings/sending");
  expect(read[1]?.body).toBeUndefined();
  expect(save[0]).toBe(read[0]);
  expect(save[1]?.method).toBe("PUT");
  expect(new Headers(save[1]?.headers).get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(JSON.parse(String(save[1]?.body))).toEqual(timing);
  expect(remove[0]).toBe("/api/v1/settings/llm");
  const deletion = JSON.parse(String(remove[1]?.body));
  expect(deletion.remove_api_key).toBe(true);
  expect(deletion).not.toHaveProperty("api_key");
});

it.each([
  { subtitle_version_id: "zh1", japanese_subtitle_version_id: null },
  { subtitle_version_id: null, japanese_subtitle_version_id: "ja1" },
])("submits analysis with one subtitle language (%j)", async (subtitles) => {
  const mockedFetch = vi.fn<typeof fetch>().mockImplementation(async () => Response.json({}));
  vi.stubGlobal("fetch", mockedFetch);
  const input = { snapshot_id: "s1", llm_config_version: 0, ...subtitles };
  await createAnalysis("p1", "analysis-key", "synthetic-csrf", input);
  expect(JSON.parse(String(mockedFetch.mock.calls[0][1]?.body))).toEqual({ kind: "classify", ...input });
});

it("skips analysis without a model or subtitles", async () => {
  const mockedFetch = vi.fn<typeof fetch>().mockImplementation(async () => Response.json({}));
  vi.stubGlobal("fetch", mockedFetch);
  await createAnalysis("p1", "skip-key", "synthetic-csrf", { snapshot_id: "s1", skip_analysis: true });
  const [path, init] = mockedFetch.mock.calls[0];
  expect(path).toBe("/api/v1/projects/p1/tasks");
  expect(new Headers(init?.headers).get("Idempotency-Key")).toBe("skip-key");
  expect(JSON.parse(String(init?.body))).toEqual({ kind: "classify", snapshot_id: "s1", skip_analysis: true });
});

it.each(["max", null] as const)("submits the project's thinking effort (%s)", async (reasoning_effort) => {
  const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(async () => Response.json({}));
  vi.stubGlobal("fetch", fetch);
  const input = { snapshot_id: "s1", subtitle_version_id: "zh1", llm_config_version: 1, reasoning_effort };
  await createAnalysis("p1", "analysis-key", "synthetic-csrf", input);
  expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toEqual({ kind: "classify", ...input });
});

it("sends review filters, an explicit page and an atomic revisioned update", async () => {
  const fetch = vi.fn().mockImplementation(() => Promise.resolve(Response.json({})));
  vi.stubGlobal("fetch", fetch);
  const signal = new AbortController().signal;
  await listReview("project", "run", { category: "other", selected: false, q: "合成 & %" },
    "cursor==", 50, signal);
  const [url, options] = fetch.mock.calls[0];
  const params = new URL(url, "http://127.0.0.1").searchParams;
  const query: Record<string, string> = {};
  params.forEach((value, key) => { query[key] = value; });
  expect(query).toEqual({
    run_id: "run", category: "other", selected: "false",
    q: "合成 & %", cursor: "cursor==", limit: "50",
  });
  expect(options.signal).toBe(signal);

  await updateReview("project", "run", 7, ["first", "second"], false, "synthetic-csrf");
  const [path, init] = fetch.mock.calls[1];
  expect(path).toBe("/api/v1/projects/project/review");
  expect(init.method).toBe("PATCH");
  expect(init.headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(JSON.parse(init.body)).toEqual({
    run_id: "run", expected_revision: 7, item_ids: ["first", "second"], selected: false,
  });
});

it("sends a signed millisecond offset and range choice with the review revision", async () => {
  const commit = { revision: 8, changed_count: 3, selected_count: 2 };
  const fetch = vi.fn().mockResolvedValue(Response.json(commit));
  vi.stubGlobal("fetch", fetch);
  expect(await updateReviewOffset("project", "run", 7, "synthetic-item", -125, true, "synthetic-csrf")).toEqual(commit);
  const [path, init] = fetch.mock.calls[0];
  expect(path).toBe("/api/v1/projects/project/review/offset");
  expect(init.method).toBe("PATCH");
  expect(init.headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(JSON.parse(init.body)).toEqual({
    run_id: "run", expected_revision: 7, item_id: "synthetic-item", offset_ms: -125, apply_to_following: true,
  });
});

it("loads all matching review rows in cursor order for one scrollable list", async () => {
  const items = Array.from({ length: 10_001 }, (_, i) => ({ item_id: `synthetic-${i}` }));
  const fetch = vi.fn(async (url: string) => {
    const params = new URL(url, "http://127.0.0.1").searchParams;
    expect(params.get("q")).toBe("合成");
    expect(params.get("limit")).toBe("500");
    const offset = Number(params.get("cursor") ?? 0);
    const end = offset + 500;
    return Response.json({
      items: items.slice(offset, end), next_cursor: end < items.length ? String(end) : null,
      revision: 8, selected_count: 2,
    });
  });
  vi.stubGlobal("fetch", fetch);
  const signal = new AbortController().signal;

  await expect(loadReviewResults("project", "run", { q: "合成" }, signal))
    .resolves.toEqual({ items, revision: 8, selected_count: 2 });
  expect(fetch).toHaveBeenCalledTimes(21);
  expect(fetch).toHaveBeenLastCalledWith(expect.stringContaining("cursor=10000"), { signal });
});

it("rejects a scrollable list assembled from different review revisions", async () => {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(Response.json({ items: [], next_cursor: "next", revision: 8, selected_count: 0 }))
    .mockResolvedValueOnce(Response.json({ items: [], next_cursor: null, revision: 9, selected_count: 0 })));

  await expect(loadReviewResults("project", "run", {}, new AbortController().signal))
    .rejects.toMatchObject({ status: 409, code: "revision_conflict" });
});
