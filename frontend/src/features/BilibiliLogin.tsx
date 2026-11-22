import { useEffect, useRef, useState } from "react";
import {
  ApiError, BilibiliAccountRole, BilibiliLoginStatus, BilibiliProfile, BilibiliQr, cancelBilibiliLogin,
  pollBilibiliLogin, startBilibiliLogin,
} from "../api/workbench";

export function BilibiliLoginDialog({ csrf, role, onClose, onLogin, onUnauthorized }: {
  csrf: string; role: BilibiliAccountRole; onClose: () => void;
  onLogin: (role: BilibiliAccountRole, login: BilibiliLoginStatus) => void; onUnauthorized: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [generation, setGeneration] = useState(0);
  const [qr, setQr] = useState<BilibiliQr>();
  const [state, setState] = useState<"loading" | "waiting" | "scanned" | "expired" | "error">("loading");
  const [error, setError] = useState("");

  useEffect(() => { dialog.current?.showModal(); }, []);

  useEffect(() => {
    const controller = new AbortController();
    let challenge: BilibiliQr | undefined;
    let finished = false;
    let timer: number | undefined;
    const { signal } = controller;

    function fail(reason: unknown) {
      if (reason instanceof ApiError && reason.status === 401) {
        onUnauthorized();
        return;
      }
      setError(reason instanceof ApiError ? reason.message : "暂时无法连接登录服务");
      if (challenge && (!(reason instanceof ApiError) || reason.status === 503)) {
        timer = window.setTimeout(poll, 3000);
      } else {
        setState("error");
      }
    }

    async function poll() {
      if (!challenge || signal.aborted) return;
      try {
        const result = await pollBilibiliLogin(challenge.id, csrf, signal, role);
        if (signal.aborted) return;
        setError("");
        if (result.status === "authenticated" && result.login) {
          finished = true;
          onLogin(role, result.login);
        } else if (result.status !== "authenticated") {
          setState(result.status);
          if (result.status !== "expired") timer = window.setTimeout(poll, 2200);
        }
      } catch (reason) {
        if (!signal.aborted) fail(reason);
      }
    }

    startBilibiliLogin(csrf, undefined, role).then((result) => {
      if (signal.aborted) {
        void cancelBilibiliLogin(result.id, csrf, role).catch(() => undefined);
        return;
      }
      challenge = result;
      setQr(result);
      setState("waiting");
      timer = window.setTimeout(poll, 2200);
    }).catch((reason: unknown) => {
      if (!signal.aborted) fail(reason);
    });

    return () => {
      controller.abort();
      window.clearTimeout(timer);
      if (challenge && !finished) void cancelBilibiliLogin(challenge.id, csrf, role).catch(() => undefined);
    };
  }, [csrf, generation, onLogin, onUnauthorized, role]);

  function regenerate() {
    setQr(undefined);
    setError("");
    setState("loading");
    setGeneration((value) => value + 1);
  }

  const showCode = qr && (state === "waiting" || state === "scanned");
  return <dialog ref={dialog} aria-labelledby="bilibili-login-title"
    onCancel={(event) => { event.preventDefault(); onClose(); }}
    className="w-[calc(100%_-_2rem)] max-w-sm rounded-xl border border-line bg-panel p-0 shadow-xl backdrop:bg-scrim">
    <header className="flex items-center justify-between border-b px-5 py-3">
      <h2 id="bilibili-login-title" className="text-lg font-semibold">{role === "secondary" ? "登录次账号" : "B 站登录"}</h2>
      <button type="button" className="secondary px-2.5 text-lg leading-none" aria-label="关闭登录"
        onClick={onClose}>×</button>
    </header>
    <div className="flex flex-col items-center px-6 py-6 text-center">
      <div className="grid size-56 place-items-center rounded-xl border border-line bg-white">
        {showCode ? <img src={qr.image} width={208} height={208} alt="B 站登录二维码" />
          : <div className="space-y-3 px-4 text-sm text-ink-muted">
            <p role="status">{state === "loading" ? "正在生成二维码…" : state === "expired" ? "二维码已失效" : "无法显示二维码"}</p>
            {state !== "loading" && <button type="button" className="secondary" onClick={regenerate}>刷新二维码</button>}
          </div>}
      </div>
      <p className="mt-3 text-sm text-ink-quiet">手机APP扫码登录</p>
      {state === "scanned" && <p role="status" className="mt-2 text-sm text-ink-secondary">已扫码，请在手机上确认</p>}
      {error && <p role="alert" className="mt-2 text-sm text-danger">{error}</p>}
    </div>
  </dialog>;
}

function accountLabel(profile: BilibiliProfile) {
  return profile.name || `UID ${profile.account_id}`;
}

function AccountAvatar({ profile, className }: { profile: BilibiliProfile; className: string }) {
  const [brokenImage, setBrokenImage] = useState(false);
  useEffect(() => { setBrokenImage(false); }, [profile.avatar_url]);

  return <span aria-hidden="true" className={`grid place-items-center overflow-hidden rounded-full bg-panel-muted ${className}`}>
    {profile.avatar_url && !brokenImage
      ? <img className="size-full object-cover" src={profile.avatar_url} alt="" referrerPolicy="no-referrer"
        onError={() => setBrokenImage(true)} />
      : (profile.name?.[0] || "B")}
  </span>;
}

export function BilibiliAccountMenu({ primary, secondary, busy, onLogout }: {
  primary?: BilibiliProfile | null; secondary?: BilibiliProfile | null;
  busy?: BilibiliAccountRole; onLogout: (role: BilibiliAccountRole) => void;
}) {
  const [open, setOpen] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector("button")?.focus();
    function outside(event: PointerEvent) {
      if (event.target instanceof Node && !container.current?.contains(event.target)) setOpen(false);
    }
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);

  const profile = primary ?? secondary;
  if (!profile) return null;
  const label = [primary && `B 站账号 ${accountLabel(primary)}`, secondary && `次账号 ${accountLabel(secondary)}`]
    .filter(Boolean).join("，");
  const optionClass = "w-full whitespace-nowrap rounded-md px-3 py-2 text-left text-sm hover:bg-panel-muted focus-visible:bg-panel-muted";
  return <div ref={container} className="relative" onKeyDown={(event) => {
    if (event.key === "Escape" && open) { setOpen(false); trigger.current?.focus(); }
  }}>
    <button ref={trigger} type="button" aria-label={label} aria-expanded={open} title={label}
      className="relative block size-9 rounded-full text-sm font-medium text-ink-secondary"
      onClick={() => setOpen((value) => !value)}>
      <AccountAvatar profile={profile} className="size-full border border-line-strong" />
      {primary && secondary && <AccountAvatar profile={secondary}
        className="absolute -right-[3px] -bottom-[3px] size-[18px] border-2 border-canvas text-[9px] leading-none" />}
    </button>
    {open && <div ref={menu} className="absolute right-0 top-full z-20 mt-2 min-w-28 rounded-lg border border-line bg-panel p-1 shadow-lg">
      {primary && <button type="button" disabled={!!busy} onClick={() => onLogout("primary")} className={optionClass}>
        {busy === "primary" ? "正在退出…" : secondary ? "注销主账号" : "退出登录"}
      </button>}
      {secondary && <button type="button" disabled={!!busy} onClick={() => onLogout("secondary")} className={optionClass}>
        {busy === "secondary" ? "正在注销…" : "注销次账号"}
      </button>}
    </div>}
  </div>;
}
