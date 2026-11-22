import { expect, it } from "vitest";
import type { Delivery, DeliveryAttempt, DeliveryPage } from "../src/api/workbench";
import { updateDeliveryView } from "../src/features/DeliveryPanel";

const withdrawn: Delivery = {
  id: "000000000001",
  status: "withdrawn", resending: false, error_code: null, bilibili_error: null, receipt: null, delivered_at: null,
  item: {
    item_id: "id:101", time_ms: 1000, text: "合成弹幕", font_size: 25, color_rgb: 0xFFFFFF,
    mode: 1, checkbox_type: 0, effect_differences: [],
  },
  attempts: [],
};
const started: DeliveryAttempt = {
  number: 1, started_at: "2026-01-01T00:00:00Z", finished_at: null, outcome: "in_flight",
  error_code: null, bilibili_error: null, receipt: null, resolution: null, resolved_by: null, resolved_at: null,
};
const sending: Delivery = { ...withdrawn, status: "in_flight", resending: true, attempts: [started] };

function page(items: Delivery[], tracked_items: Delivery[] = []): DeliveryPage {
  return {
    items, tracked_items, next_cursor: null, task_version: 1, total_count: items.length,
    batch: {
      id: "synthetic-task", run_id: "synthetic-run", review_revision: 1, mode: "live", account_id: "17001",
      target: { bvid: "BV0000000001", cid: "73", page: 1, duration_ms: 2000, title: "合成分 P" },
    },
  };
}

it.each([
  ["confirmed", "confirmed", "sent"],
  ["rejected", "failed", "failed"],
  ["unknown", "unknown", "failed"],
] as const)("keeps the %s resend in the withdrawn view for its exit animation", (outcome, status, kind) => {
  const previous = updateDeliveryView({ page: page([withdrawn]), motions: new Map() }, page([sending]), true);
  expect(previous.page?.items).toEqual([sending]);
  expect(previous.motions.size).toBe(0);
  const finished: Delivery = {
    ...sending, status, resending: false, attempts: [{ ...started, outcome, finished_at: "2026-01-01T00:00:01Z" }],
  };
  const settling = updateDeliveryView(previous, page([], [finished]), true);
  expect(settling.motions.get(sending.id)).toMatchObject({ kind, row: { id: sending.id, status }, fromSending: true });
  const refreshed = updateDeliveryView(settling, page([]), true);
  expect(refreshed.motions.get(sending.id)).toBe(settling.motions.get(sending.id));
  expect(updateDeliveryView(previous, page([], [finished]), false).motions.size).toBe(0);
});

it("rebounds inside the withdrawn view when a resend is rate-limited", () => {
  const queued: Delivery = {
    ...sending, status: "pending",
    attempts: [{ ...started, outcome: "limited", finished_at: "2026-01-01T00:00:01Z" }],
  };
  const settling = updateDeliveryView({ page: page([sending]), motions: new Map() }, page([queued]), true);
  expect(settling.page?.items).toEqual([queued]);
  expect(settling.motions.get(queued.id)).toMatchObject({ kind: "returned", row: queued, fromSending: true });
});

it("retains a successfully withdrawn pending row until it slides out", () => {
  const pending: Delivery = { ...withdrawn, status: "pending" };
  const following: Delivery = { ...pending, id: "000000000002" };
  const before = { page: page([pending, following]), motions: new Map() };
  const after = page([following], [withdrawn]);

  expect(updateDeliveryView(before, after, true).motions.size).toBe(0);
  expect(updateDeliveryView(before, before.page, true, pending.id).motions.size).toBe(0);
  const updated = updateDeliveryView(before, after, true, pending.id);
  expect(updated.page?.items).toEqual([following]);
  expect(updated.motions.get(pending.id)).toMatchObject({ kind: "withdrawn", row: withdrawn });
  expect(updateDeliveryView(updated, page([following]), true).motions.get(pending.id))
    .toBe(updated.motions.get(pending.id));
});

it("plays a withdrawal in the all-status view and keeps its updated row", () => {
  const pending: Delivery = { ...withdrawn, status: "pending" };
  const after = page([withdrawn]);
  const updated = updateDeliveryView({ page: page([pending]), motions: new Map() }, after, false, pending.id);

  expect(updated.page?.items).toEqual([withdrawn]);
  expect(updated.motions.get(pending.id)?.kind).toBe("withdrawn");
  expect(updateDeliveryView(updated, after, false).motions.get(pending.id)).toBe(updated.motions.get(pending.id));
});

it("keeps each account's exit animation when another row settles during collapse", () => {
  const primary: Delivery = { ...sending, item: { ...sending.item, account_role: "primary" } };
  const secondary: Delivery = {
    ...sending, id: "000000000002", item: { ...sending.item, item_id: "id:102", account_role: "secondary" },
  };
  function confirmed(row: Delivery): Delivery {
    return {
      ...row, status: "confirmed", resending: false,
      attempts: [{ ...started, outcome: "confirmed", finished_at: "2026-01-01T00:00:01Z" }],
    };
  }
  const first = updateDeliveryView({ page: page([primary, secondary]), motions: new Map() },
    page([secondary], [confirmed(primary)]), true);
  expect(first.page?.items).toEqual([secondary]);
  expect(first.motions.get(primary.id)?.kind).toBe("sent");
  const both = updateDeliveryView(first, page([], [confirmed(secondary)]), true);
  expect(both.motions.get(primary.id)).toBe(first.motions.get(primary.id));
  expect(both.motions.get(secondary.id)).toMatchObject({ kind: "sent", fromSending: true });
});
