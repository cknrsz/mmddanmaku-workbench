import { expect, it } from "vitest";
import { SettingsView } from "../src/api/workbench";
import { makeSettingsDraft, reviewSettingsDraft } from "../src/features/settingsDraft";

const saved: SettingsView = {
  bilibili: { configured: true, version: 1, account_id: "17001" },
  llm: {
    configured: true, can_set_reasoning_effort: true, provider: "openai_compatible", base_url: "https://api.deepseek.com",
    model: "deepseek-flash", version: 2, thinking_enabled: true, reasoning_effort: "high", extra_requirements: "",
  },
  review: { uncheck_small_by_default: true },
  sending: { item_min_seconds: 5, item_max_seconds: 7, rate_limit_seconds: 180, dual_account_enabled: false },
};

it("treats restored fields, whitespace, and equivalent timing values as unchanged", () => {
  const draft = makeSettingsDraft(saved);
  expect(reviewSettingsDraft(saved, draft).dirty).toBe(false);
  draft.llm.thinking = false;
  draft.sending.itemMin = "6";
  expect(reviewSettingsDraft(saved, draft).dirty).toBe(true);
  draft.llm.thinking = true;
  draft.llm.requirements = "  ";
  draft.sending.itemMin = "05";
  expect(reviewSettingsDraft(saved, draft)).toEqual({ dirty: false, update: {}, errors: {} });
});

it("combines the edited sections and supports undoing key removal", () => {
  const draft = makeSettingsDraft(saved);
  draft.llm.removeKey = true;
  expect(reviewSettingsDraft(saved, draft).update.llm?.remove_api_key).toBe(true);
  draft.llm.removeKey = false;
  expect(reviewSettingsDraft(saved, draft).dirty).toBe(false);
  draft.llm.requirements = "合成要求";
  draft.sending.rateLimit = "45";
  expect(reviewSettingsDraft(saved, draft)).toMatchObject({
    dirty: true, errors: {}, update: {
      llm: { extra_requirements: "合成要求" },
      sending: { rate_limit_seconds: 45 },
    },
  });
});

it("flags invalid edits and leaves a default offline model alone when editing timing", () => {
  const initial: SettingsView = { ...saved, llm: {
    ...saved.llm, provider: "mock", base_url: null, model: "synthetic-v1", version: 0,
  } };
  const draft = makeSettingsDraft(initial);
  draft.sending.itemMin = "8";
  expect(reviewSettingsDraft(initial, draft)).toMatchObject({
    dirty: true, errors: { sending: expect.any(String) },
  });
  draft.sending.itemMin = "3";
  expect(reviewSettingsDraft(initial, draft).update).toEqual({ sending: {
    item_min_seconds: 3, item_max_seconds: 7, rate_limit_seconds: 180,
  } });
});

it("changes the dual-account setting independently of the timing fields", () => {
  const draft = makeSettingsDraft(saved);
  draft.sending.dualAccountEnabled = true;
  expect(reviewSettingsDraft(saved, draft).update).toEqual({ sending: { dual_account_enabled: true } });
  draft.sending.dualAccountEnabled = false;
  expect(reviewSettingsDraft(saved, draft).dirty).toBe(false);
});

it("saves the review preference independently and detects undoing the change", () => {
  const draft = makeSettingsDraft(saved);
  draft.review.uncheck_small_by_default = false;
  expect(reviewSettingsDraft(saved, draft).update).toEqual({ review: { uncheck_small_by_default: false } });
  draft.review.uncheck_small_by_default = true;
  expect(reviewSettingsDraft(saved, draft).dirty).toBe(false);
});
