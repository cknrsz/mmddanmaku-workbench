import { Tooltip } from "../components/Tooltip";
import { Pagination } from "./Pagination";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError, createPreloadProject, createProject, deleteProject, getPendingSendDecision, getProject,
  listPreloadParts, listProjects, PreloadPart, Project, ProjectInput, publicationLabels,
  renameProject, SendRate, Session, setProjectCompletion, Task, VideoPart,
} from "../api/workbench";
import { useQuestion } from "../components/QuestionDialog";
import { ProjectName } from "./ProjectName";
import { PreloadPicker } from "./PreloadPicker";
import { ProjectWorkflow } from "./ProjectWorkflow";
import { useRowsPerPage } from "./useRowsPerPage";
import { VideoPartPicker } from "./VideoPartPicker";

const activityLabels: Record<Project["activity"]["stage"], { running: string; waiting: string }> = {
  download: { running: "弹幕下载中", waiting: "弹幕待下载" },
  classify: { running: "弹幕分析中", waiting: "弹幕待分析" },
  review: { running: "弹幕待审核", waiting: "弹幕待审核" },
  send: { running: "弹幕发送中", waiting: "弹幕待发送" },
};
const selectedProjectKey = "danmaku:selected-project";

export function ProjectCardStatus({ project, activity }: { project: Project; activity: Project["activity"] }) {
  if (project.completed && (!activity.running || !activity.active)) {
    return <span role="img" aria-label="迁移完成" title="迁移完成"
      className="shrink-0 text-xs font-bold text-success">✓</span>;
  }
  if (project.publication_status) {
    return <span className="shrink-0 text-xs text-ink-quiet">{publicationLabels[project.publication_status]}</span>;
  }
  if (project.send_partial_failure) {
    return <span className="flex items-center gap-1 text-xs font-semibold text-warning">
      <span aria-hidden="true">?</span>部分弹幕发送失败，待核对
    </span>;
  }
  if (project.send_needs_review) {
    return <span className="shrink-0 text-xs font-semibold text-warning">需人工核对</span>;
  }
  const queue = activity.stage === "send" && activity.active && !activity.running ? project.send_queue : null;
  return <span className="shrink-0 text-xs text-ink-quiet">
    {queue ? `投递排队中（${queue.position}/${queue.total}）`
      : activityLabels[activity.stage][activity.running ? "running" : "waiting"]}
  </span>;
}

function readSelectedProject() {
  try {
    return localStorage.getItem(selectedProjectKey);
  } catch {
    return null;
  }
}

function ProjectForm({ onSubmit, pending, onUnauthorized }: {
  onSubmit: (project: ProjectInput) => Promise<void>;
  pending: boolean;
  onUnauthorized: () => void;
}) {
  const [part, setPart] = useState<VideoPart>();
  const [error, setError] = useState("");

  function report(error: unknown) {
    if (error instanceof ApiError && error.status === 401) onUnauthorized();
    else setError(error instanceof Error ? error.message : "请求失败");
  }

  return <>
    {error && <p role="alert" className="text-sm text-danger">{error}</p>}
    <form onSubmit={(event) => {
      event.preventDefault();
      if (!part) return;
      setError("");
      void onSubmit({ target_bvid: part.bvid, target_page: part.page }).catch(report);
    }} className="grid grid-cols-[minmax(0,1fr)_auto] items-end gap-2">
      <VideoPartPicker label="目标视频" part={part} disabled={pending} onPart={setPart} onError={report} />
      <button className="primary" disabled={pending || !part}>创建项目</button>
    </form>
  </>;
}

export function Workbench({ session, onUnauthorized, modelVersion, reviewRefresh, bilibiliVersion, sendRate, onSendControl }: {
  session: Session;
  onUnauthorized: () => void;
  modelVersion: number;
  reviewRefresh: number;
  bilibiliVersion: number;
  sendRate: SendRate | null;
  onSendControl: () => void;
}) {
  const { confirm, prompt } = useQuestion();
  const [openDelivery, setOpenDelivery] = useState<Task>();
  const [sendDecision, setSendDecision] = useState<Task | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selected, setSelected] = useState<string>();
  const [projectIndex, setProjectIndex] = useState(0);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [preloadLoading, setPreloadLoading] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [preloadParts, setPreloadParts] = useState<PreloadPart[]>();
  const [error, setError] = useState("");
  const [menu, setMenu] = useState<string>();
  const [menuAbove, setMenuAbove] = useState(false);
  const [liveActivity, setLiveActivity] = useState<{ id: string; activity: Project["activity"] }>();
  const sidebar = useRef<HTMLElement>(null);
  const menuCard = useRef<HTMLLIElement | null>(null);
  const { viewport, size: projectsPerPage, rowHeight } = useRowsPerPage(5.125, { trailingGapRem: 0.5 });
  const cardHeight = `calc(${rowHeight} - 0.5rem)`;
  const orderedProjects = [...projects].sort((left, right) => right.creation_order - left.creation_order);
  const totalPages = Math.ceil(orderedProjects.length / projectsPerPage);
  const projectPage = Math.min(Math.floor(projectIndex / projectsPerPage) + 1, Math.max(1, totalPages));
  const pageStart = (projectPage - 1) * projectsPerPage;
  const visibleProjects = orderedProjects.slice(pageStart, pageStart + projectsPerPage);
  const project = orderedProjects.find((entry) => entry.id === selected);
  const updateActivity = useCallback((id: string, activity?: Project["activity"]) => {
    setLiveActivity((current) => activity ? { id, activity } : current?.id === id ? undefined : current);
  }, []);

  useEffect(() => setMenu(undefined), [pageStart, projectsPerPage]);

  useEffect(() => {
    if (!menu) return;
    function outside(event: PointerEvent) {
      if (event.target instanceof Node && !menuCard.current?.contains(event.target)) setMenu(undefined);
    }
    function escape(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      menuCard.current?.querySelector<HTMLButtonElement>("[aria-expanded]")?.focus();
      setMenu(undefined);
    }
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [menu]);

  function report(error: unknown) {
    if (error instanceof ApiError && error.status === 401) onUnauthorized();
    else setError(error instanceof Error ? error.message : "请求失败");
  }

  function runProjectAction(entry: Project, action: (entry: Project) => Promise<void>) {
    menuCard.current?.querySelector<HTMLButtonElement>("[aria-expanded]")?.focus();
    setMenu(undefined);
    void action(entry);
  }

  useEffect(() => {
    if (loading || !selected) return;
    try {
      localStorage.setItem(selectedProjectKey, selected);
    } catch {
      // Project selection still works when browser storage is unavailable.
    }
  }, [selected, loading]);

  async function load() {
    setLoading(true);
    setError("");
    try {
      const items: Project[] = [];
      let after: string | undefined;
      do {
        const result = await listProjects(after);
        items.push(...result.items);
        after = result.next_cursor ?? undefined;
      } while (after);
      items.sort((left, right) => right.creation_order - left.creation_order);
      const saved = readSelectedProject();
      const selectedIndex = Math.max(0, items.findIndex((entry) => entry.id === saved));
      setProjects(items);
      setSelected(items[selectedIndex]?.id);
      setProjectIndex(selectedIndex);
    } catch (error) {
      report(error);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, []);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    async function refresh() {
      try {
        if (!document.hidden) {
          const task = await getPendingSendDecision(controller.signal);
          if (!controller.signal.aborted) setSendDecision(task);
        }
      } catch (error) {
        if (!controller.signal.aborted && error instanceof ApiError && error.status === 401) onUnauthorized();
      }
      if (!controller.signal.aborted) timer = window.setTimeout(refresh, 3000);
    }
    void refresh();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [onUnauthorized]);

  useEffect(() => {
    if (!visibleProjects.length || loading || pending) return;
    const controller = new AbortController();
    const watchedIds = new Set(visibleProjects.map((entry) => entry.id));
    if (selected) watchedIds.add(selected);

    async function refreshProjectStatus() {
      if (document.hidden) return;
      const statuses = new Map<string, Pick<Project, "completed" | "activity" | "send_queue" |
        "send_needs_review" | "send_partial_failure" | "publication_status" | "preload_publish_at">>();
      let after: string | undefined;
      try {
        do {
          const page = await listProjects(after, controller.signal);
          for (const entry of page.items) {
            if (watchedIds.has(entry.id)) {
              statuses.set(entry.id, {
                completed: entry.completed, activity: entry.activity,
                send_queue: entry.send_queue, send_needs_review: entry.send_needs_review,
                send_partial_failure: entry.send_partial_failure,
                publication_status: entry.publication_status,
                preload_publish_at: entry.preload_publish_at,
              });
            }
          }
          after = page.next_cursor ?? undefined;
        } while (after && statuses.size < watchedIds.size);
        if (controller.signal.aborted) return;
        setProjects((current) => current.map((entry) => {
          const next = statuses.get(entry.id);
          if (!next || (next.completed === entry.completed && next.send_needs_review === entry.send_needs_review
            && next.send_partial_failure === entry.send_partial_failure
            && next.publication_status === entry.publication_status
            && next.preload_publish_at === entry.preload_publish_at
            && next.activity.stage === entry.activity.stage
            && next.activity.running === entry.activity.running && next.activity.active === entry.activity.active
            && next.send_queue?.position === entry.send_queue?.position
            && next.send_queue?.total === entry.send_queue?.total)) return entry;
          return { ...entry, ...next };
        }));
      } catch (error) {
        if (!controller.signal.aborted && error instanceof ApiError && error.status === 401) onUnauthorized();
      }
    }

    let timer: number;
    async function refresh() {
      await refreshProjectStatus();
      if (!controller.signal.aborted) timer = window.setTimeout(refresh, 3000);
    }
    void refresh();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [projects.length, pageStart, projectsPerPage, selected, loading, pending, liveActivity?.id, liveActivity?.activity.stage,
    liveActivity?.activity.running, liveActivity?.activity.active, onUnauthorized]);

  function openCreatedProject(project: Project) {
    setProjects((current) => [project, ...current]);
    setSelected(project.id);
    setProjectIndex(0);
    setMenu(undefined);
    setShowForm(false);
    setPreloadParts(undefined);
  }

  async function submit(input: ProjectInput) {
    setPending(true);
    setError("");
    try {
      openCreatedProject(await createProject(input, session.csrf_token, "bilibili"));
    } finally {
      setPending(false);
    }
  }

  async function preloadNotice(title: string) {
    await confirm({ title, confirmLabel: "确定", cancelLabel: null });
  }

  async function startPreload() {
    setPending(true);
    setPreloadLoading(true);
    setError("");
    try {
      const parts = await listPreloadParts();
      if (!parts.length || parts.length > 1 || parts[0].already_created) setPreloadLoading(false);
      if (!parts.length) await preloadNotice("未找到已过审但尚未公开的视频");
      else if (parts.length > 1) setPreloadParts(parts);
      else if (parts[0].already_created) await preloadNotice("项目已创建");
      else openCreatedProject(await createPreloadProject(parts[0].part, session.csrf_token));
    } catch (error) {
      setPreloadLoading(false);
      if (error instanceof ApiError && error.code === "preload_project_exists") await preloadNotice("项目已创建");
      else report(error);
    } finally {
      setPreloadLoading(false);
      setPending(false);
    }
  }

  async function selectPreloadPart(part: VideoPart) {
    setPending(true);
    try {
      openCreatedProject(await createPreloadProject(part, session.csrf_token));
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      else if (error instanceof ApiError && error.code === "preload_project_exists") {
        setPreloadParts((parts) => parts?.map((entry) => entry.part.bvid === part.bvid && entry.part.page === part.page
          ? { ...entry, already_created: true } : entry));
        throw error;
      } else throw error;
    } finally {
      setPending(false);
    }
  }

  async function openSendDecision(task: Task) {
    setPending(true);
    setError("");
    try {
      const existing = projects.find((entry) => entry.id === task.project_id);
      const entry = existing ?? await getProject(task.project_id);
      if (!existing) setProjects((current) => [entry, ...current]);
      const newerCount = projects.filter((item) => item.creation_order > entry.creation_order).length;
      setProjectIndex(newerCount);
      setMenu(undefined);
      setSelected(entry.id);
      setOpenDelivery({ ...task });
    } catch (error) {
      report(error);
    } finally {
      setPending(false);
    }
  }

  async function rename(entry: Project) {
    const input = await prompt({ title: "项目名称", initialValue: entry.name, confirmLabel: "保存名称" });
    if (input === null) return;
    const name = input.trim();
    if (name === entry.name) return;
    if (!name || Array.from(name).length > 120) {
      setError("项目名称须为 1–120 个字符");
      return;
    }
    setPending(true);
    setError("");
    try {
      const updated = await renameProject(entry.id, name, session.csrf_token);
      setProjects((current) => current.map((item) => item.id === entry.id ? updated : item));
    } catch (error) {
      report(error);
    } finally {
      setPending(false);
    }
  }

  async function changeCompletion(entry: Project) {
    const completed = !entry.completed;
    const confirmation = `确定将「${entry.name}」标记为${completed ? "已完成" : "未完成"}？`;
    if (!await confirm({ title: confirmation, confirmLabel: completed ? "标记为已完成" : "标记为未完成" })) return;
    setPending(true);
    setError("");
    try {
      const updated = await setProjectCompletion(entry.id, completed, session.csrf_token);
      setProjects((current) => current.map((item) => item.id === entry.id ? updated : item));
    } catch (error) {
      report(error);
    } finally {
      setPending(false);
    }
  }

  async function remove(entry: Project) {
    if (!await confirm({
      title: `永久删除「${entry.name}」？`,
      message: "本地任务、字幕、审核和投递记录将一并删除。已发送到 B 站的弹幕会保留。",
      confirmLabel: "永久删除",
    })) return;
    setPending(true);
    setError("");
    try {
      await deleteProject(entry.id, session.csrf_token);
      const index = orderedProjects.findIndex((item) => item.id === entry.id);
      const replacement = orderedProjects[index + 1] ?? orderedProjects[index - 1];
      setProjects((current) => current.filter((item) => item.id !== entry.id));
      setSelected((current) => current === entry.id ? replacement?.id : current);
      setProjectIndex((current) => Math.min(current, Math.max(0, projects.length - 2)));
      setOpenDelivery((task) => task?.project_id === entry.id ? undefined : task);
      setSendDecision((task) => task?.project_id === entry.id ? null : task);
    } catch (error) {
      report(error);
    } finally {
      setPending(false);
    }
  }

  function changeProjectPage(page: number) {
    setMenu(undefined);
    setProjectIndex((page - 1) * projectsPerPage);
  }

  return (
    <div className="grid min-h-0 flex-1 grid-rows-[clamp(11rem,27dvh,15rem)_minmax(0,1fr)] gap-4 md:grid-cols-[19rem_minmax(0,1fr)] md:grid-rows-[minmax(0,1fr)]">
      <aside ref={sidebar} className="flex min-h-0 min-w-0 flex-col gap-3 overflow-y-auto rounded-xl border border-line bg-panel p-3 sm:p-4">
        <div className="flex items-center justify-between gap-2">
          <h2 className="font-semibold">项目</h2>
          <div className="flex flex-wrap justify-end gap-2">
            <Tooltip text={showForm ? "取消新建项目" : "新建一个弹幕迁移项目"}><button className="secondary whitespace-nowrap py-1.5" disabled={loading || pending}
              onClick={() => setShowForm(!showForm)}>{showForm ? "取消" : "新建项目"}</button></Tooltip>
            <Tooltip text="视频已过审？提前准备好，在视频公开时自动开始迁移"><button className="secondary whitespace-nowrap py-1.5" disabled={loading || pending}
              onClick={() => void startPreload()}>{preloadLoading ? "正在读取" : "预装弹幕"}</button></Tooltip>
          </div>
        </div>
        {preloadParts && <PreloadPicker entries={preloadParts} pending={pending}
          onSelect={selectPreloadPart} onClose={() => setPreloadParts(undefined)} />}
        {error && <div data-backend-notice className="space-y-2">
          <p role="alert" className="text-sm text-danger">{error}</p>
          {projects.length === 0 && <button className="secondary" onClick={() => load()}>重新读取</button>}
        </div>}
        {showForm && <ProjectForm onSubmit={submit} pending={pending} onUnauthorized={onUnauthorized} />}
        {projects.length === 0 && <p data-backend-notice className="text-sm text-ink-muted">
          {loading ? "正在读取项目…" : "暂无项目"}
        </p>}
        <div ref={viewport} className="flex-1" style={{ minHeight: projects.length ? cardHeight : 0 }}>
          {visibleProjects.length > 0 && <ul className="grid h-full gap-2" style={{
            gridTemplateRows: visibleProjects.length === projectsPerPage
              ? `repeat(${projectsPerPage}, minmax(0, 1fr))`
              : `repeat(${visibleProjects.length}, ${cardHeight})`,
          }}>
            {visibleProjects.map((entry, index) => {
              const activity = liveActivity?.id === entry.id ? liveActivity.activity : entry.activity;
              const completionBlocked = !entry.completed && entry.activity.active;
              return <li key={entry.id} className="relative min-h-0"
                ref={menu === entry.id ? menuCard : undefined}>
                <button className="list-button project-card-select h-full flex-col flex-nowrap items-start justify-center gap-1 rounded-lg py-1.5"
                  aria-pressed={selected === entry.id} disabled={pending}
                  onClick={() => { setMenu(undefined); setOpenDelivery(undefined); setSelected(entry.id); setProjectIndex(pageStart + index); }}>
                  <ProjectName name={entry.name} lines={2} />
                  <ProjectCardStatus project={entry} activity={activity} />
                </button>
                <button type="button" className="project-card-menu" aria-label={`${entry.name}的项目操作`}
                  aria-expanded={menu === entry.id} title="项目操作" disabled={loading || pending}
                  onClick={(event) => {
                    const bounds = sidebar.current?.getBoundingClientRect();
                    const row = event.currentTarget.getBoundingClientRect();
                    const menuHeight = 7.5 * parseFloat(getComputedStyle(document.documentElement).fontSize);
                    setMenuAbove(!!bounds && bounds.bottom - row.bottom < menuHeight && row.top - bounds.top >= menuHeight);
                    setMenu((current) => current === entry.id ? undefined : entry.id);
                  }}>
                  <span aria-hidden="true">⋯</span>
                </button>
                {menu === entry.id && <div aria-label="项目操作"
                  className={`absolute right-0 z-20 grid w-36 rounded-md border border-line bg-panel p-1 shadow-lg ${menuAbove ? "bottom-full" : "top-full"}`}>
                  <button className="project-menu-item" onClick={() => runProjectAction(entry, rename)}>重命名</button>
                  <button className="project-menu-item text-danger" onClick={() => runProjectAction(entry, remove)}>删除</button>
                  <button className="project-menu-item" disabled={completionBlocked}
                    title={completionBlocked ? "请等待当前任务结束" : undefined}
                    onClick={() => runProjectAction(entry, changeCompletion)}>
                    {entry.completed ? "标记为未完成" : "标记为已完成"}
                  </button>
                </div>}
              </li>;
            })}
          </ul>}
        </div>
        {totalPages > 0 && <Pagination label="项目分页" page={projectPage} totalPages={totalPages}
          disabled={loading || pending} onPageChange={changeProjectPage} />}
      </aside>
      <div className="flex min-h-0 min-w-0 flex-col gap-3">
        {sendDecision && <div className="flex shrink-0 flex-wrap items-center gap-2 rounded border border-warning-line bg-warning-tint px-3 py-1.5 text-sm">
          <p role="status">队首投递有 {sendDecision.progress.failed} 条失败弹幕，后续批次正在等待。</p>
          <button className="secondary" disabled={pending || loading} onClick={() => openSendDecision(sendDecision)}>
            查看失败弹幕
          </button>
        </div>}
        {project && <ProjectWorkflow key={project.id} project={project} csrf={session.csrf_token}
          modelVersion={modelVersion} reviewRefresh={reviewRefresh} bilibiliVersion={bilibiliVersion} onUnauthorized={onUnauthorized}
          onActivity={updateActivity} sendRate={sendRate} onSendControl={onSendControl}
          openDelivery={openDelivery?.project_id === project.id ? openDelivery : undefined} />}
      </div>
    </div>
  );
}

