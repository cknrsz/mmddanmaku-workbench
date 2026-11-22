import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { QuestionProvider, useQuestion } from "./components/QuestionDialog";
import { readHealth } from "./api/health";
import {
  ApiError, BilibiliAccountRole, BilibiliLoginStatus, decideOverduePublication, getBilibiliLogin,
  getInterruptedSendCount, getOverduePublication, getPausedSendHead,
  getDualUpgradeCount, getSendConnection, getSendRate, getSettings, logoutBilibili, openLocalSession,
  resumeInterruptedSends, SendRate, Session, upgradeSendQueue,
} from "./api/workbench";
import { PullCord as YukkuriCord, yukkuriAssetsReady } from "./components/galactrex/pull-cord-yukkuri";
import { PullCord as OldCord } from "./components/galactrex/pull-cord-old";
import { BilibiliAccountMenu, BilibiliLoginDialog } from "./features/BilibiliLogin";
import { Workbench } from "./features/Workbench";
import { SettingsDialog } from "./features/SettingsDialog";
import "./styles.css";

function Startup() {
  const { choose, prompt } = useQuestion();
  const [yukkuri, setYukkuri] = useState(false);
  useEffect(() => {
    let active = true;
    try { if (localStorage.getItem("yukkuri-cord") === "true") void yukkuriAssetsReady().then((ready) => { if (active) setYukkuri(ready); }); } catch { /* Storage is optional. */ }
    return () => { active = false; };
  }, []);
  const [darkTheme, setDarkTheme] = useState(() => document.documentElement.dataset.theme === "dark");
  const [backend, setBackend] = useState<"connecting" | "connected" | "disconnected">("connecting");
  const [session, setSession] = useState<Session>();
  const [connection, setConnection] = useState(0);
  const [logins, setLogins] = useState<Partial<Record<BilibiliAccountRole, BilibiliLoginStatus>>>({});
  const [bilibiliErrors, setBilibiliErrors] = useState<Partial<Record<BilibiliAccountRole, string>>>({});
  const [loginRole, setLoginRole] = useState<BilibiliAccountRole>();
  const overdueLoginPrompted = useRef<string>();
  const overdueQuestionOpen = useRef(false);
  const [overdueError, setOverdueError] = useState("");
  const [loggingOut, setLoggingOut] = useState<BilibiliAccountRole>();
  const [dualAccountEnabled, setDualAccountEnabled] = useState<boolean>();
  const previousDualAccount = useRef<string | null>();
  const [dualUpgradeError, setDualUpgradeError] = useState("");
  const [dualAccountDraft, setDualAccountDraft] = useState<boolean>();
  const secondaryChecked = useRef(false);
  const [showSettings, setShowSettings] = useState(false);
  const [interruptedSends, setInterruptedSends] = useState(0);
  const [resumingSends, setResumingSends] = useState(false);
  const [resumeError, setResumeError] = useState("");
  const [modelVersion, setModelVersion] = useState(0);
  const [reviewRefresh, setReviewRefresh] = useState(0);
  const [bilibiliVersion, setBilibiliVersion] = useState(0);
  const [sendRate, setSendRate] = useState<SendRate | null>(null);
  const [sendConnected, setSendConnected] = useState<boolean | null>(null);
  const [sendRateRefresh, setSendRateRefresh] = useState(0);
  const bilibiliLogin = logins.primary;
  const accountId = bilibiliLogin?.profile?.account_id;
  const secondaryProfile = (dualAccountDraft ?? dualAccountEnabled) ? logins.secondary?.profile : undefined;
  const checkSecondary = dualAccountEnabled === true || dualAccountDraft === true;

  useLayoutEffect(() => {
    const theme = darkTheme ? "dark" : "light";
    const { dataset } = document.documentElement;
    if (dataset.theme === theme) return;
    dataset.theme = theme;
    try {
      localStorage.setItem("danmaku-workbench-theme", theme);
    } catch {
      // The theme still applies when browser storage is unavailable.
    }
  }, [darkTheme]);

  const reconnect = useCallback(() => {
    setSession(undefined);
    setShowSettings(false);
    setLoginRole(undefined);
    setLogins({});
    setBilibiliErrors({});
    setDualAccountEnabled(undefined);
    previousDualAccount.current = undefined;
    setDualUpgradeError("");
    setDualAccountDraft(undefined);
    secondaryChecked.current = false;
    setInterruptedSends(0);
    setOverdueError("");
    overdueLoginPrompted.current = undefined;
    setSendConnected(null);
    setResumeError("");
    setBackend("connecting");
    setConnection((value) => value + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    let sessionOpened = false;
    async function checkBackend() {
      try {
        const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(3000)]);
        await readHealth(signal);
        if (!sessionOpened) {
          const current = await openLocalSession(signal);
          if (controller.signal.aborted) return;
          sessionOpened = true;
          setSession(current);
        }
        if (!controller.signal.aborted) setBackend("connected");
      } catch {
        if (!controller.signal.aborted) setBackend("disconnected");
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(checkBackend, 1000);
      }
    }
    void checkBackend();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [connection]);

  useEffect(() => {
    if (!session) return;
    const controller = new AbortController();
    getSettings(controller.signal).then((settings) => {
      if (!controller.signal.aborted) setDualAccountEnabled(settings.sending.dual_account_enabled);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted && reason instanceof ApiError && reason.status === 401) reconnect();
    });
    return () => controller.abort();
  }, [session, reconnect]);

  useEffect(() => {
    if (!checkSecondary) {
      secondaryChecked.current = false;
      setLogins((current) => current.secondary ? { ...current, secondary: undefined } : current);
    }
    if (!session || loginRole || loggingOut) return;
    const { csrf_token } = session;
    const controller = new AbortController();
    async function check(role: BilibiliAccountRole) {
      try {
        const force = role === "secondary" && !secondaryChecked.current;
        const login = await getBilibiliLogin(csrf_token, controller.signal, role, force);
        if (controller.signal.aborted) return;
        if (role === "secondary") {
          secondaryChecked.current = true;
          if (force) setBilibiliErrors((current) => ({ ...current, secondary: "" }));
        }
        setLogins((current) => ({ ...current, [role]: login }));
      } catch (reason) {
        if (controller.signal.aborted) return;
        if (reason instanceof ApiError && reason.status === 401) reconnect();
        else if (role === "secondary" && !secondaryChecked.current) {
          setBilibiliErrors((current) => ({ ...current, secondary: "暂时无法验证次账号登录，稍后会自动重试。" }));
        }
      }
    }
    function refresh() {
      void check("primary");
      if (checkSecondary && (dualAccountEnabled || !secondaryChecked.current)) void check("secondary");
    }
    refresh();
    const timer = window.setInterval(refresh, 60_000);
    return () => { window.clearInterval(timer); controller.abort(); };
  }, [session, loginRole, loggingOut, reconnect, checkSecondary, dualAccountEnabled]);

  useEffect(() => {
    const loginStatus = bilibiliLogin?.status;
    if (!session || !loginStatus || loginStatus === "unavailable" || loginRole || showSettings || loggingOut) return;
    const controller = new AbortController();
    let timer: number;
    async function refresh(csrf: string) {
      let delay = 3000;
      try {
        if (document.hidden || overdueQuestionOpen.current) return;
        const taskId = await getOverduePublication(controller.signal);
        if (!taskId || controller.signal.aborted || document.hidden) return;
        if (loginStatus !== "valid") {
          if (overdueLoginPrompted.current !== taskId) {
            overdueLoginPrompted.current = taskId;
            setLoginRole("primary");
          }
          return;
        }
        overdueQuestionOpen.current = true;
        try {
          const send = await choose({
            title: "预装弹幕任务时间已过，是否仍进行发送？",
            confirmLabel: "是", alternativeLabel: "否", cancelLabel: null,
          });
          if (send === null) delay = 15000;
          else if (!controller.signal.aborted) {
            await decideOverduePublication(taskId, send, csrf);
            setOverdueError("");
          }
        } finally {
          overdueQuestionOpen.current = false;
        }
      } catch (reason) {
        if (controller.signal.aborted) return;
        if (reason instanceof ApiError && reason.status === 401) reconnect();
        else if (reason instanceof ApiError && reason.code === "bilibili_credential_rejected") {
          setLoginRole("primary");
        } else if (!(reason instanceof ApiError && reason.code === "publication_queue_changed")) {
          setOverdueError(reason instanceof Error ? reason.message : "预装弹幕队列处理失败");
        }
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(() => void refresh(csrf), delay);
      }
    }
    void refresh(session.csrf_token);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [session, bilibiliLogin?.status, loginRole, showSettings, loggingOut, choose, reconnect]);

  useEffect(() => {
    if (!session || resumingSends) return;
    const controller = new AbortController();
    let timer: number;
    async function refresh() {
      try {
        if (!document.hidden) {
          const count = await getInterruptedSendCount(controller.signal);
          if (!controller.signal.aborted) setInterruptedSends(count);
        }
      } catch (error) {
        if (!controller.signal.aborted && error instanceof ApiError && error.status === 401) reconnect();
      }
      if (!controller.signal.aborted) timer = window.setTimeout(refresh, 3000);
    }
    void refresh();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [session, resumingSends, reconnect]);

  const refreshSendRate = useCallback(() => setSendRateRefresh((value) => value + 1), []);

  useEffect(() => {
    if (!session) return;
    const controller = new AbortController();
    let timer: number;
    async function refresh() {
      try {
        const connected = await getSendConnection(controller.signal);
        if (!controller.signal.aborted) setSendConnected(connected);
      } catch (error) {
        if (!controller.signal.aborted && error instanceof ApiError && error.status === 401) reconnect();
      }
      if (!controller.signal.aborted) timer = window.setTimeout(refresh, 1000);
    }
    void refresh();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [session, reconnect]);

  useEffect(() => setSendRate(null), [session, accountId]);

  useEffect(() => {
    if (!session || !accountId) return;
    const controller = new AbortController();
    let timer: number;
    async function refresh() {
      try {
        if (!document.hidden) {
          const rate = await getSendRate(controller.signal);
          if (!controller.signal.aborted) setSendRate(rate);
        }
      } catch (error) {
        if (!controller.signal.aborted && error instanceof ApiError && error.status === 401) reconnect();
      }
      if (!controller.signal.aborted) timer = window.setTimeout(refresh, 5000);
    }
    void refresh();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [session, accountId, reconnect, sendRateRefresh]);

  useEffect(() => {
    setBilibiliVersion((value) => value + 1);
  }, [accountId, bilibiliLogin?.status, dualAccountEnabled,
    logins.secondary?.profile?.account_id, logins.secondary?.profile?.level]);

  const secondary = logins.secondary?.profile;
  const availableSecondaryId = dualAccountEnabled && accountId && (secondary?.level ?? 0) > 0
    ? secondary?.account_id ?? null : null;
  const accountStateLoaded = dualAccountEnabled !== undefined && !!logins.primary
    && (!dualAccountEnabled || !!logins.secondary);
  useEffect(() => {
    if (!session || !accountStateLoaded) return;
    const previous = previousDualAccount.current;
    previousDualAccount.current = availableSecondaryId;
    if (!availableSecondaryId || previous === undefined || previous === availableSecondaryId) return;
    const secondaryId = availableSecondaryId;
    setDualUpgradeError("");
    async function offerUpgrade(csrf: string) {
      try {
        if (!await getDualUpgradeCount() || previousDualAccount.current !== secondaryId) return;
        const answer = await prompt({
          title: "是否为当前正在进行的弹幕发送任务启用双账号发送？",
          message: "在途弹幕结算后，会重新分配当前任务的待发送弹幕，以及队列中符合条件的其他任务。",
          confirmLabel: "启用双账号发送",
        });
        if (answer !== "confirm" || previousDualAccount.current !== secondaryId) return;
        await upgradeSendQueue(secondaryId, csrf);
        setBilibiliVersion((value) => value + 1);
        refreshSendRate();
      } catch (reason) {
        if (reason instanceof ApiError && reason.status === 401) reconnect();
        else setDualUpgradeError(reason instanceof Error ? reason.message : "切换双账号发送失败");
      }
    }
    void offerUpgrade(session.csrf_token);
  }, [session, accountStateLoaded, availableSecondaryId, prompt, reconnect, refreshSendRate]);

  const finishLogin = useCallback((role: BilibiliAccountRole, login: BilibiliLoginStatus) => {
    setLogins((current) => ({ ...current, [role]: login }));
    setBilibiliErrors((current) => ({ ...current, [role]: "" }));
    setLoginRole(undefined);
  }, []);

  async function resumeSends() {
    if (!session || resumingSends) return;
    setResumingSends(true);
    setResumeError("");
    try {
      const head = await getPausedSendHead();
      const confirmed = head && await choose({
        title: "解除任务暂停？",
        message: `任务「${head.project_name}」之前被人为暂停。`,
        confirmLabel: "解除暂停",
        alternativeLabel: "保留暂停",
      });
      if (head && confirmed === null) return;
      await resumeInterruptedSends(session.csrf_token, confirmed
        ? { task_id: head.task_id, expected_version: head.expected_version }
        : undefined);
      refreshSendRate();
      setInterruptedSends(0);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) reconnect();
      else setResumeError(error instanceof Error ? error.message : "恢复发送任务失败，请重试");
    } finally {
      setResumingSends(false);
    }
  }

  async function logout(role: BilibiliAccountRole) {
    if (!session || loggingOut) return;
    setLoggingOut(role);
    setBilibiliErrors((current) => ({ ...current, [role]: "" }));
    try {
      await logoutBilibili(session.csrf_token, role);
      const login: BilibiliLoginStatus = { status: "unconfigured", profile: null, auto_refresh: false };
      setLogins((current) => ({ ...current, [role]: login }));
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) reconnect();
      else setBilibiliErrors((current) => ({
        ...current, [role]: reason instanceof ApiError ? reason.message : "退出登录失败，请重试",
      }));
    } finally {
      setLoggingOut(undefined);
    }
  }

  function secondaryAction() {
    if (logins.secondary?.profile) void logout("secondary");
    else setLoginRole("secondary");
  }

  const offline = session && sendConnected === false;
  const visibleSendRate = session && accountId ? sendRate : null;
  const backendLabel = backend === "connected" ? "后端已连接" :
    backend === "disconnected" ? "后端未连接" : "正在连接后端…";

  return (
    <main data-backend={backend} className="workbench flex h-dvh flex-col gap-4 overflow-x-clip bg-canvas px-3 py-3 sm:px-6 sm:py-4">
      <header className="relative flex min-h-9 shrink-0 flex-wrap items-center justify-between gap-x-3 gap-y-2 rounded-lg bg-canvas">
        <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-0.5">
          <h1 className="text-lg font-semibold tracking-tight sm:text-2xl">弹幕迁移工作台</h1>
          <p role="status" className="inline-flex items-center gap-1.5 text-xs text-ink-muted">
            <span aria-hidden="true" className={`size-2 rounded-full ${backend === "connected" ? "bg-success-dot" : backend === "disconnected" ? "bg-danger-dot" : "bg-idle"}`} />
            {backendLabel}
          </p>
        </div>
        <div className="ml-auto flex max-w-full shrink-0 flex-wrap items-center justify-end gap-1 sm:gap-2">
          {session && <>
            {interruptedSends > 1 && <button type="button"
              className="secondary whitespace-nowrap border-success-line bg-success-tint px-2 py-1.5 text-success sm:px-2.5"
              title="恢复因后台中断而暂停的弹幕发送任务" disabled={resumingSends} onClick={() => void resumeSends()}>
              {resumingSends ? "正在恢复…" : "恢复所有任务"}
            </button>}
            <button type="button" className="secondary inline-flex items-center gap-1.5 px-2.5 py-1.5"
              aria-haspopup="dialog" onClick={() => setShowSettings(true)}>
              <span aria-hidden="true" className="text-lg leading-none">⚙</span>
              设置
            </button>
            {!bilibiliLogin?.profile && <button type="button" className="secondary px-2.5 py-1.5"
              aria-haspopup="dialog" onClick={() => setLoginRole("primary")}>登录</button>}
            {(bilibiliLogin?.profile || secondaryProfile) && <BilibiliAccountMenu
              primary={bilibiliLogin?.profile} secondary={secondaryProfile} busy={loggingOut}
              onLogout={(role) => void logout(role)} />}
          </>}
          <div className="relative z-10 h-9 w-12 overflow-visible">
            {yukkuri ? <YukkuriCord className="pointer-events-none -top-3 sm:-top-4" length={81} threshold={50}
              label={darkTheme ? "开灯，切换到日间模式" : "关灯，切换到夜间模式"}
              isDark={darkTheme} onPull={() => setDarkTheme((dark) => !dark)} /> : <OldCord className="pointer-events-none -top-3 sm:-top-4" length={81} threshold={50} label={darkTheme ? "开灯，切换到日间模式" : "关灯，切换到夜间模式"} onPull={() => setDarkTheme((dark) => !dark)} />}
          </div>
        </div>
        {backend === "connected" && (offline || visibleSendRate) && (
          <p role="status" className={`pointer-events-none absolute top-full right-0 mt-2 whitespace-nowrap text-right font-mono text-xs leading-3 font-semibold tabular-nums ${offline ? "text-danger" : "text-success"}`}>
            {offline ? "网络未连接" : visibleSendRate &&
              `发送速度 ${visibleSendRate.items_per_hour.toLocaleString("zh-CN", {
                minimumFractionDigits: 1, maximumFractionDigits: visibleSendRate.items_per_hour < 0.1 ? 2 : 1,
              })} 条/小时`}
          </p>
        )}
      </header>
      {session && resumeError && <p role="alert" className="shrink-0 text-sm text-danger">{resumeError}</p>}
      {session && overdueError && <p role="alert" className="shrink-0 text-sm text-danger">{overdueError}</p>}
      {session && bilibiliErrors.primary && <p role="alert" className="shrink-0 text-sm text-danger">{bilibiliErrors.primary}</p>}
      {session && !showSettings && bilibiliErrors.secondary && <p role="alert" className="shrink-0 text-sm text-danger">
        {bilibiliErrors.secondary}
      </p>}
      {session && bilibiliLogin?.status === "renewal_required" && <p role="status" className="shrink-0 text-sm text-warning">
        B 站登录已失效，请重新扫码。
      </p>}
      {session && dualAccountEnabled && logins.secondary?.status === "renewal_required" &&
        <div className="flex shrink-0 items-center gap-2 text-sm text-warning">
          <p role="status">次账号登录已失效，请重新扫码。</p>
          <button type="button" className="underline" aria-haspopup="dialog"
            disabled={!!loggingOut} onClick={() => setLoginRole("secondary")}>登录次账号</button>
        </div>}
      {session && bilibiliLogin?.status === "unavailable" && <p role="status" className="shrink-0 text-sm text-warning">
        暂时无法验证 B 站登录，稍后会自动重试。
      </p>}
      {session && bilibiliLogin?.status === "valid" && !bilibiliLogin.auto_refresh && <p className="shrink-0 text-sm text-ink-muted">
        当前 B 站登录无法自动续期。<button type="button" className="ml-2 underline" aria-haspopup="dialog"
          disabled={!!loggingOut} onClick={() => setLoginRole("primary")}>扫码启用自动续期</button>
      </p>}
      {dualUpgradeError && <p role="alert" className="text-sm text-danger">{dualUpgradeError}</p>}
      {session && <Workbench session={session} onUnauthorized={reconnect} modelVersion={modelVersion} reviewRefresh={reviewRefresh}
        bilibiliVersion={bilibiliVersion} sendRate={offline ? null : visibleSendRate} onSendControl={refreshSendRate} />}
      {session && showSettings && <SettingsDialog yukkuri={yukkuri} onYukkuriChange={(enabled) => { setYukkuri(enabled); try { localStorage.setItem("yukkuri-cord", String(enabled)); } catch { /* Storage is optional. */ } }} csrf={session.csrf_token} onUnauthorized={reconnect}
        onClose={() => { setShowSettings(false); setDualAccountDraft(undefined); }}
        onReviewSaved={() => setReviewRefresh((value) => value + 1)}
        onModelSaved={(saved) => setModelVersion(saved.version)}
        onDualAccountSaved={setDualAccountEnabled} onDualAccountDraftChange={setDualAccountDraft}
        secondaryLogin={logins.secondary} secondaryBusy={loggingOut === "secondary"}
        secondaryError={bilibiliErrors.secondary ?? ""} onSecondaryAction={secondaryAction} />}
      {session && loginRole && <BilibiliLoginDialog csrf={session.csrf_token} role={loginRole} onUnauthorized={reconnect}
        onClose={() => setLoginRole(undefined)} onLogin={finishLogin} />}
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<QuestionProvider><Startup /></QuestionProvider>);

