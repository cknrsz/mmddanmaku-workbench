import { ApiError, getTask, Task, terminal } from "./workbench";

export type Connection = "connecting" | "live" | "polling" | "finished";

export type AnalysisConsoleUpdate = { replace: boolean; reasoning: string; output: string };
export type ConsoleConnection = "connecting" | "live" | "disconnected" | "finished";

export function watchAnalysisConsole(
  taskId: string,
  onUpdate: (update: AnalysisConsoleUpdate) => void,
  onConnection: (connection: ConsoleConnection) => void,
): () => void {
  const source = new EventSource(`/api/v1/tasks/${taskId}/events?analysis_console=true`);
  source.onopen = () => onConnection("live");
  source.onerror = () => onConnection("disconnected");
  source.addEventListener("analysis.console", (event) => {
    const update: AnalysisConsoleUpdate = JSON.parse((event as MessageEvent<string>).data);
    onUpdate(update);
  });
  source.addEventListener("analysis.console.end", () => {
    source.close();
    onConnection("finished");
  });
  return () => source.close();
}

export function watchTask(
  initial: Task,
  onTask: (task: Task) => void,
  onConnection: (connection: Connection) => void,
  onUnauthorized: () => void,
): () => void {
  if (terminal(initial)) {
    onConnection("finished");
    return () => {};
  }

  const abort = new AbortController();
  const source = new EventSource(`/api/v1/tasks/${initial.id}/events?after=${initial.version}`);
  let version = initial.version;
  let fallback: ReturnType<typeof setInterval> | undefined;
  let polling = false;

  function stopPolling() {
    clearInterval(fallback);
    fallback = undefined;
  }

  function close() {
    source.close();
    abort.abort();
    stopPolling();
  }

  function accept(task: Task) {
    if (abort.signal.aborted || task.id !== initial.id || task.version <= version) return;
    version = task.version;
    onTask(task);
    if (terminal(task)) {
      close();
      onConnection("finished");
    }
  }

  async function poll() {
    if (polling) return;
    polling = true;
    try {
      accept(await getTask(initial.id, abort.signal));
    } catch (error) {
      if (!abort.signal.aborted && error instanceof ApiError && error.status === 401) {
        close();
        onUnauthorized();
      }
    } finally {
      polling = false;
    }
  }

  function receive(event: Event) {
    try {
      accept(JSON.parse((event as MessageEvent<string>).data) as Task);
    } catch (error) {
      if (!(error instanceof SyntaxError)) throw error;
      startPolling();
    }
  }

  function startPolling() {
    if (abort.signal.aborted) return;
    onConnection("polling");
    if (fallback === undefined) {
      fallback = setInterval(poll, 2000);
      void poll();
    }
  }

  source.addEventListener("task.updated", receive);
  source.addEventListener("reset", receive);
  source.onopen = () => {
    stopPolling();
    onConnection("live");
  };
  source.onerror = startPolling;
  onConnection("connecting");
  return close;
}

