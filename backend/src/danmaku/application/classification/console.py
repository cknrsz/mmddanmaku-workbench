from io import StringIO

from pydantic import BaseModel

from danmaku.application.classification.model import ModelText

RECENT_RUNS = 8
MAX_TEXT_CHARS = 2 * 1024 * 1024


class ConsoleUpdate(BaseModel):
    replace: bool
    reasoning: str
    output: str


class ConsoleRun:
    """One attempt's provisional model text. Accessed only on the worker's event loop."""

    def __init__(self):
        self.reasoning = StringIO()
        self.output = StringIO()
        self.finished = False

    def publish(self, section: ModelText, text: str) -> None:
        remaining = MAX_TEXT_CHARS - self.reasoning.tell() - self.output.tell()
        if remaining > 0:
            target = self.reasoning if section == "reasoning" else self.output
            target.write(text[:remaining])

    @property
    def position(self) -> tuple[int, int]:
        return self.reasoning.tell(), self.output.tell()

    def update(self, after: tuple[int, int] | None) -> ConsoleUpdate | None:
        if after == self.position:
            return None
        reasoning_start, output_start = after or (0, 0)
        self.reasoning.seek(reasoning_start)
        self.output.seek(output_start)
        return ConsoleUpdate(replace=after is None, reasoning=self.reasoning.read(), output=self.output.read())


class AnalysisConsoles:
    """Keep active and recent attempts in memory. A retry replaces the previous attempt."""

    def __init__(self):
        self.runs: dict[str, ConsoleRun] = {}

    def begin(self, task_id: str) -> ConsoleRun:
        run = ConsoleRun()
        self.runs[task_id] = run
        return run

    def finish(self, task_id: str) -> None:
        run = self.runs.pop(task_id)
        run.finished = True
        self.runs[task_id] = run
        completed = [key for key, value in self.runs.items() if value.finished]
        for key in completed[:-RECENT_RUNS]:
            del self.runs[key]
