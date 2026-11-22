import {
  DEEPSEEK_ENDPOINT, DEEPSEEK_MODEL, ReasoningEffort, SendTiming, SettingsUpdate, SettingsView,
} from "../api/workbench";

export const settingsSections = {
  llm: "弹幕LLM分析",
  review: "审核设置",
  sending: "弹幕投递",
};
export type SettingsSection = keyof typeof settingsSections;

export type SettingsDraft = {
  llm: { thinking: boolean; effort: ReasoningEffort; requirements: string; key: string; removeKey: boolean };
  review: SettingsView["review"];
  sending: { itemMin: string; itemMax: string; rateLimit: string; dualAccountEnabled: boolean };
};

export function deepSeekEndpoint(settings: SettingsView) {
  return settings.llm.base_url === `${DEEPSEEK_ENDPOINT}/v1` ? settings.llm.base_url : DEEPSEEK_ENDPOINT;
}

export function makeSettingsDraft(settings: SettingsView): SettingsDraft {
  const timing = settings.sending;
  return {
    llm: {
      thinking: settings.llm.thinking_enabled, effort: settings.llm.reasoning_effort,
      requirements: settings.llm.extra_requirements, key: "", removeKey: false,
    },
    review: { ...settings.review },
    sending: {
      itemMin: String(timing.item_min_seconds), itemMax: String(timing.item_max_seconds),
      rateLimit: String(timing.rate_limit_seconds), dualAccountEnabled: timing.dual_account_enabled,
    },
  };
}

function intervalInSeconds(value: string) {
  const seconds = value.trim() ? Number(value) : NaN;
  return Number.isInteger(seconds) && seconds >= 0 && seconds <= 86400 ? seconds : null;
}

export function reviewSettingsDraft(saved: SettingsView, draft: SettingsDraft) {
  const update: SettingsUpdate = {};
  const errors: Partial<Record<SettingsSection, string>> = {};
  const { thinking, effort, requirements, key, removeKey } = draft.llm;
  const cleanedKey = key.trim();
  const cleanedRequirements = requirements.trim();
  if (thinking !== saved.llm.thinking_enabled || effort !== saved.llm.reasoning_effort ||
      cleanedRequirements !== saved.llm.extra_requirements || cleanedKey || removeKey) {
    update.llm = {
      provider: "openai_compatible", base_url: deepSeekEndpoint(saved), model: DEEPSEEK_MODEL,
      thinking_enabled: thinking, reasoning_effort: effort, extra_requirements: cleanedRequirements,
      ...(removeKey ? { remove_api_key: true } : cleanedKey ? { api_key: cleanedKey } : {}),
    };
  }

  if (draft.review.uncheck_small_by_default !== saved.review.uncheck_small_by_default) {
    update.review = draft.review;
  }

  const { itemMin, itemMax, rateLimit, dualAccountEnabled } = draft.sending;
  const min = intervalInSeconds(itemMin);
  const max = intervalInSeconds(itemMax);
  const retry = intervalInSeconds(rateLimit);
  if (min === null || max === null || retry === null) {
    errors.sending = "请填写 0 到 86400 的整数秒。";
  } else if (min > max) {
    errors.sending = "每条间隔的上限必须大于或等于下限。";
  } else {
    const timing: SendTiming = {
      item_min_seconds: min, item_max_seconds: max, rate_limit_seconds: retry,
    };
    if ((Object.keys(timing) as (keyof SendTiming)[]).some((field) => timing[field] !== saved.sending[field])) {
      update.sending = timing;
    }
    if (dualAccountEnabled !== saved.sending.dual_account_enabled) {
      update.sending = { ...update.sending, dual_account_enabled: dualAccountEnabled };
    }
  }

  return { update, errors, dirty: Object.keys(update).length > 0 || Object.keys(errors).length > 0 };
}
