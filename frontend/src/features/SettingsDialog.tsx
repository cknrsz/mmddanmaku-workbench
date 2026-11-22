import { yukkuriAssetsReady } from "../components/galactrex/pull-cord-yukkuri";
import { useQuestion } from "../components/QuestionDialog";
import { FormEvent, useEffect, useRef, useState } from "react";
import {
  ApiError, BilibiliLoginStatus, DEEPSEEK_MODEL, DEEPSEEK_MODEL_LABEL, getSettings,
  ModelSettings, ReasoningEffort, saveSettings, SettingsView,
} from "../api/workbench";
import {
  deepSeekEndpoint, makeSettingsDraft, reviewSettingsDraft,
  SettingsDraft, SettingsSection, settingsSections,
} from "./settingsDraft";

function ModelFields({ saved, value, onChange }: {
  saved: SettingsView; value: SettingsDraft["llm"]; onChange: (value: SettingsDraft["llm"]) => void;
}) {
  const hasKey = saved.llm.base_url === deepSeekEndpoint(saved) && saved.llm.configured;
  return <div className="space-y-4">
    <div className="grid gap-3 sm:grid-cols-2">
      <label className="field">供应商
        <select defaultValue="deepseek"><option value="deepseek">DeepSeek</option></select>
      </label>
      <label className="field">模型
        <select name="model" defaultValue={DEEPSEEK_MODEL}>
          <option value={DEEPSEEK_MODEL}>{DEEPSEEK_MODEL_LABEL}</option>
        </select>
      </label>
    </div>
    {hasKey && <div className="flex flex-wrap items-center justify-between gap-2">
      <span className="select-none text-sm">{value.removeKey ? "保存后删除密钥" : "API 密钥　••••••••　已保存"}</span>
      <button type="button" className="secondary" onClick={() => onChange({ ...value, removeKey: !value.removeKey })}>
        {value.removeKey ? "撤销删除" : "删除密钥"}
      </button>
    </div>}
    {!value.removeKey && <label className="field">{hasKey ? "替换 API 密钥" : "API 密钥"}
      <input name="api_key" type="password" autoComplete="new-password" maxLength={1024}
        value={value.key} onChange={(event) => onChange({ ...value, key: event.currentTarget.value })}
        onCopy={(event) => event.preventDefault()} onCut={(event) => event.preventDefault()}
        onDragStart={(event) => event.preventDefault()} />
    </label>}
    <label className="flex items-center gap-2 text-sm">
      <input type="checkbox" checked={value.thinking}
        onChange={(event) => onChange({ ...value, thinking: event.currentTarget.checked })} />
      启用思考
    </label>
    <label className={value.thinking ? "field" : "field opacity-60"}>思考强度
      <select value={value.effort} disabled={!value.thinking}
        onChange={(event) => onChange({ ...value, effort: event.currentTarget.value as ReasoningEffort })}>
        <option value="low">低</option><option value="high">高</option><option value="max">最高</option>
      </select>
    </label>
    <label className="field">额外分析要求
      <textarea name="extra_requirements" rows={5} maxLength={4000} value={value.requirements}
        onChange={(event) => onChange({ ...value, requirements: event.currentTarget.value })} />
    </label>
  </div>;
}

function TimingFields({ value, onChange }: {
  value: SettingsDraft["sending"]; onChange: (value: SettingsDraft["sending"]) => void;
}) {
  return <div className="space-y-4">
    <div className="grid grid-cols-2 gap-3">
      <label className="field">每条间隔下限（秒）
        <input type="number" min={0} max={86400} name="item_min_seconds" value={value.itemMin}
          onChange={(event) => onChange({ ...value, itemMin: event.currentTarget.value })} />
      </label>
      <label className="field">每条间隔上限（秒）
        <input type="number" min={0} max={86400} name="item_max_seconds" value={value.itemMax}
          onChange={(event) => onChange({ ...value, itemMax: event.currentTarget.value })} />
      </label>
    </div>
    <label className="field">限流重试等待（秒）
      <input type="number" min={0} max={86400} name="rate_limit_seconds" value={value.rateLimit}
        onChange={(event) => onChange({ ...value, rateLimit: event.currentTarget.value })} />
    </label>
  </div>;
}

export function SettingsDialog({
  yukkuri, onYukkuriChange, csrf, onClose, onUnauthorized, onModelSaved, onReviewSaved, onDualAccountSaved, onDualAccountDraftChange,
  secondaryLogin, secondaryBusy, secondaryError, onSecondaryAction,
}: {
  yukkuri: boolean; onYukkuriChange: (enabled: boolean) => void;
  csrf: string; onClose: () => void; onUnauthorized: () => void;
  onModelSaved: (settings: ModelSettings) => void;
  onReviewSaved: () => void;
  onDualAccountSaved: (enabled: boolean) => void; onDualAccountDraftChange: (enabled: boolean) => void;
  secondaryLogin?: BilibiliLoginStatus; secondaryBusy: boolean; secondaryError: string; onSecondaryAction: () => void;
}) {
  const { confirm } = useQuestion();
  const dialog = useRef<HTMLDialogElement>(null);
  const confirmExit = useRef<HTMLDialogElement>(null);
  const [section, setSection] = useState<SettingsSection>("llm");
  const [saved, setSaved] = useState<SettingsView>();
  const [draft, setDraft] = useState<SettingsDraft>();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const change = saved && draft ? reviewSettingsDraft(saved, draft) : undefined;

  useEffect(() => { dialog.current?.showModal(); }, []);

  useEffect(() => {
    const controller = new AbortController();
    getSettings(controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setSaved(result);
      setDraft(makeSettingsDraft(result));
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      else setError(error instanceof Error ? error.message : "无法读取设置");
    });
    return () => controller.abort();
  }, [onUnauthorized]);

  function edit<K extends SettingsSection>(key: K, value: SettingsDraft[K]) {
    setDraft((current) => current && { ...current, [key]: value });
    setError("");
    setMessage("");
  }

  function leave() {
    confirmExit.current?.close();
    dialog.current?.close();
    onClose();
  }

  function requestExit() {
    if (saving) return;
    if (change?.dirty) confirmExit.current?.showModal();
    else leave();
  }

  async function save(exit: boolean) {
    if (saving || !saved || !change?.dirty) return;
    const invalid = (Object.keys(change.errors) as SettingsSection[])[0];
    if (invalid) {
      confirmExit.current?.close();
      setSection(invalid);
      setError(change.errors[invalid] ?? "");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const result = await saveSettings(change.update, csrf);
      setSaved(result);
      setDraft(makeSettingsDraft(result));
      if (result.llm.version !== saved.llm.version) onModelSaved(result.llm);
      if (result.review.uncheck_small_by_default !== saved.review.uncheck_small_by_default) onReviewSaved();
      onDualAccountSaved(result.sending.dual_account_enabled);
      if (exit) leave();
      else setMessage("设置已保存");
    } catch (error) {
      confirmExit.current?.close();
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      else setError(error instanceof Error ? error.message : "保存设置失败");
    } finally {
      setSaving(false);
    }
  }

  const yukkuriHelp = () => confirm({ title: "油库里拉绳", message: "将当前用于切换UI外观的拉绳组件切换为油库里琪露诺/大妖精。\n需自行下载相应素材：\n油库里琪露诺：https://booth.pm/downloadables/5221454?variation_id=10373165\n油库里大妖精：https://booth.pm/downloadables/5221490?variation_id=10373209\n将两份psd文件放入 frontend\\public\\yukkuri 文件夹下，双击 setup-yukkuri-assets.cmd 即可。", confirmLabel: "知道了", cancelLabel: null });
  async function toggleYukkuri(enabled: boolean) {
    if (!enabled) { onYukkuriChange(false); return; }
    if (await yukkuriAssetsReady()) onYukkuriChange(true);
    else await yukkuriHelp();
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void save(false);
  }

  return <>
    <dialog ref={dialog} onCancel={(event) => { event.preventDefault(); requestExit(); }}
      aria-labelledby="settings-title"
      className="size-[min(40rem,calc(100dvw_-_2rem),calc(100dvh_-_2rem))] max-h-none max-w-none overflow-hidden rounded-xl border border-line bg-panel p-0 text-ink shadow-xl backdrop:bg-scrim">
      <form onSubmit={submit} autoComplete="off" noValidate className="flex h-full flex-col">
        <header className="flex shrink-0 items-center justify-between border-b bg-panel px-5 py-3">
          <h2 id="settings-title" className="text-lg font-semibold">设置</h2>
          <button type="button" aria-label="退出设置" className="secondary px-2.5 text-lg leading-none"
            disabled={saving} onClick={requestExit}>×</button>
        </header>
        <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_minmax(0,2.5fr)] overflow-y-auto sm:grid-cols-[11rem_minmax(0,1fr)]">
          <nav aria-label="设置分类" className="space-y-1 border-r bg-canvas p-2 sm:p-3">
            {(Object.keys(settingsSections) as SettingsSection[]).map((key) => <button key={key} type="button"
              aria-pressed={section === key} aria-controls="settings-fields" disabled={saving}
              onClick={() => setSection(key)}
              className="w-full rounded-md px-3 py-2 text-left text-sm font-medium text-ink-secondary hover:bg-panel-muted aria-pressed:bg-accent-selected aria-pressed:text-accent-strong">
              {settingsSections[key]}
            </button>)}
          </nav>
          <section id="settings-fields" aria-labelledby="settings-section-title" className="space-y-4 p-4 sm:p-6">
            <h3 id="settings-section-title" className="font-semibold">{settingsSections[section]}</h3>
            {error && <p role="alert" className="text-sm text-danger">{error}</p>}
            {saved && draft ? <fieldset disabled={saving}>
              {section === "llm" && <ModelFields saved={saved} value={draft.llm}
                onChange={(value) => edit("llm", value)} />}
              {section === "review" && <div className="flex items-center gap-2 text-sm"><label className="flex items-center gap-2">
                <input type="checkbox" checked={draft.review.uncheck_small_by_default}
                  onChange={(event) => edit("review", { uncheck_small_by_default: event.currentTarget.checked })} />
                默认不勾选小字弹幕
              </label><button type="button" aria-label="小字弹幕说明" className="help-button" onClick={() => void confirm({ title: "小字弹幕说明", message: "小字弹幕在曾经的MMD中往往是翻译内容", confirmLabel: "知道了", cancelLabel: null })}>?</button></div>}
              {section === "sending" && <div className="space-y-4">
                <TimingFields value={draft.sending} onChange={(value) => edit("sending", value)} />
                <div className="flex items-center gap-2 text-sm"><label className="flex items-center gap-2">
                  <input type="checkbox" checked={draft.sending.dualAccountEnabled} onChange={(event) => {
                    const dualAccountEnabled = event.currentTarget.checked;
                    edit("sending", { ...draft.sending, dualAccountEnabled });
                    onDualAccountDraftChange(dualAccountEnabled);
                  }} />
                  启用双账号投递
                </label><button type="button" aria-label="双账号投递说明" className="help-button" onClick={() => void confirm({ title: "双账号投递说明", message: "添加次账号以加快迁移速度\n若次账号等级为lv3及以上，则与主账号平分任务\n若次账号等级为lv2，则只承担滚动弹幕，并尝试与主账号平分任务\n若次账号等级为lv1，则只承担白色滚动弹幕，并尝试与主账号平分任务\n若账号等级为lv0，请先转正答题。", confirmLabel: "知道了", cancelLabel: null })}>?</button></div>
                {draft.sending.dualAccountEnabled && <div className="space-y-2">
                  {!secondaryLogin && !secondaryError && <p role="status" className="text-sm">
                    正在检查次账号登录…
                  </p>}
                  {secondaryLogin && <button type="button" className="secondary" disabled={secondaryBusy}
                    aria-haspopup={secondaryLogin?.profile ? undefined : "dialog"} onClick={onSecondaryAction}>
                    {secondaryBusy ? "正在注销…" : secondaryLogin?.profile ? "注销次账号" : "登录次账号"}
                  </button>}
                  {secondaryLogin?.profile?.level === 0 && <p role="status" className="text-sm text-warning">
                    次账号无弹幕发送权限，请先答题转正
                  </p>}
                  {secondaryLogin?.status === "renewal_required" && <p role="status" className="text-sm text-warning">
                    次账号登录已失效，请重新扫码。
                  </p>}
                  {secondaryLogin?.status === "unavailable" && <p role="status" className="text-sm text-warning">
                    暂时无法验证次账号登录。
                  </p>}
                  {secondaryError && <p role="alert" className="text-sm text-danger">{secondaryError}</p>}
                </div>}
              </div>}
            </fieldset> : !error && <p className="text-sm">正在读取设置…</p>}
          </section>
        </div>
        <footer className="flex shrink-0 items-center justify-end gap-2 border-t bg-panel px-5 py-3">
          <div className="mr-auto flex items-center gap-2 text-sm"><label className="flex items-center gap-2"><input type="checkbox" checked={yukkuri} onChange={(event) => void toggleYukkuri(event.currentTarget.checked)} />启用油库里拉绳</label><button type="button" className="help-button" aria-label="油库里拉绳说明" onClick={() => void yukkuriHelp()}>?</button></div>
          {message && <p role="status" className="text-sm text-ink-muted">{message}</p>}
          <button type="button" className="secondary" disabled={saving} onClick={requestExit}>退出</button>
          <button type="submit" className="primary" disabled={!change?.dirty || saving}>{saving ? "正在保存…" : "保存"}</button>
        </footer>
      </form>
    </dialog>
    <dialog ref={confirmExit} aria-labelledby="unsaved-settings-title"
      onCancel={(event) => { event.preventDefault(); if (!saving) confirmExit.current?.close(); }}
      className="w-[calc(100%_-_2rem)] max-w-sm rounded-lg border border-line bg-panel p-5 shadow-xl backdrop:bg-scrim">
      <h2 id="unsaved-settings-title" className="font-semibold">有未保存的更改，是否保存？</h2>
      <div className="mt-5 flex justify-end gap-2">
        <button type="button" className="secondary" disabled={saving} onClick={leave}>退出</button>
        <button type="button" className="primary" disabled={saving} onClick={() => void save(true)}>
          {saving ? "正在保存…" : "保存"}
        </button>
      </div>
    </dialog>
  </>;
}
