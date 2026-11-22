import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { AnalysisConsoleUpdate, ConsoleConnection, watchAnalysisConsole } from "../api/taskEvents";
import { analysisStopped, Task, taskActive } from "../api/workbench";

const empty = { reasoning: "", output: "" };

function statusText(task: Task | undefined, text: typeof empty) {
  if (analysisStopped(task)) return "已停止";
  switch (task?.state) {
    case "queued": return "排队中";
    case "running": return text.output ? "接收输出" : text.reasoning ? "模型思考中" : "等待模型响应";
    case "backoff": return "等待重试";
    case "pausing": return "正在暂停";
    case "paused": return "已暂停";
    case "succeeded": return "分析完成";
    case "failed": return "分析失败";
    default: return "正在提交";
  }
}

export function AnalysisTerminal({ task }: { task?: Task }) {
  const [text, setText] = useState(empty);
  const [connection, setConnection] = useState<ConsoleConnection>("connecting");
  const [follow, setFollow] = useState(true);
  const scroll = useRef<HTMLDivElement>(null);
  const running = !task || taskActive(task);
  const hasText = !!(text.reasoning || text.output);
  const taskId = task?.id;

  useEffect(() => {
    if (!taskId) return;
    return watchAnalysisConsole(taskId, (update: AnalysisConsoleUpdate) => {
      setText((previous) => ({
        reasoning: (update.replace ? "" : previous.reasoning) + update.reasoning,
        output: (update.replace ? "" : previous.output) + update.output,
      }));
    }, setConnection);
  }, [taskId]);

  useLayoutEffect(() => {
    const viewport = scroll.current;
    if (follow && viewport) viewport.scrollTop = viewport.scrollHeight;
  }, [text, follow]);

  return <section aria-label="分析终端" className="analysis-terminal mt-4">
    <header className="analysis-terminal-bar">
      <h3 className="text-sm font-semibold text-ink">分析终端</h3>
      <p role="status" className="flex shrink-0 items-center gap-2 text-xs text-ink-muted">
        <span aria-hidden="true" className={`size-2 rounded-full ${task?.state === "failed" && !analysisStopped(task) ? "bg-danger-dot" :
          task?.state === "succeeded" ? "bg-success-dot" : running && connection !== "disconnected" ?
          "bg-accent-dot" : "bg-idle"}`} />
        {statusText(task, text)}
      </p>
    </header>
    <div ref={scroll} tabIndex={0} aria-label="模型返回文本" className="analysis-terminal-content"
      onScroll={(event) => {
        const view = event.currentTarget;
        setFollow(view.scrollHeight - view.clientHeight - view.scrollTop < 24);
      }}>
      {text.reasoning && <div>
        <h4 className="analysis-terminal-label">思考文本</h4>
        <div className="analysis-terminal-text text-ink-secondary">{text.reasoning}</div>
      </div>}
      {text.output && <div>
        <h4 className="analysis-terminal-label">输出文本</h4>
        <pre className="analysis-terminal-text rounded-md border border-line bg-canvas p-3 font-mono text-ink-body">
          {text.output}
        </pre>
      </div>}
      {!hasText && <p className="text-sm text-ink-muted">
        {running ? "等待模型返回文本…" : "没有可显示的模型文本。"}
      </p>}
    </div>
    {!follow && <footer className="flex min-h-9 items-center justify-end gap-3
      border-t border-line bg-canvas px-4 text-xs text-ink-muted">
      {!follow && <button type="button" className="rounded px-2 py-1 text-accent hover:bg-accent-tint"
        onClick={() => setFollow(true)}>回到底部 ↓</button>}
    </footer>}
  </section>;
}
