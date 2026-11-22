import { createContext, ReactNode, useCallback, useContext, useLayoutEffect, useRef, useState } from "react";

type Question = {
  title: string;
  message?: string;
  confirmLabel: string;
  alternativeLabel?: string;
  cancelLabel?: string | null;
  initialValue?: string;
  multiline?: boolean;
};

type Request = Question & { resolve: (answer: string | null) => void };
const QuestionContext = createContext<((question: Question) => Promise<string | null>) | null>(null);

function QuestionDialog({ question, onAnswer }: { question: Question; onAnswer: Request["resolve"] }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const [value, setValue] = useState(question.initialValue ?? "");

  useLayoutEffect(() => {
    const element = dialog.current!;
    element.showModal();
    const editor = textarea.current ?? input.current;
    if (editor) {
      editor.focus();
      editor.select();
    } else {
      cancel.current?.focus();
    }
    return () => element.close();
  }, []);

  return <dialog ref={dialog} aria-labelledby="question-title"
    aria-describedby={question.message ? "question-message" : undefined}
    onKeyDown={(event) => { if (event.key === "Escape") event.stopPropagation(); }}
    onCancel={(event) => { event.preventDefault(); onAnswer(null); }}
    className="w-[calc(100%_-_2rem)] max-w-sm rounded-lg border border-line bg-panel p-5 text-ink shadow-xl backdrop:bg-scrim">
    <form onSubmit={(event) => {
      event.preventDefault();
      onAnswer(question.initialValue !== undefined ? value : "confirm");
    }}>
      <h2 id="question-title" className="break-words font-semibold">{question.title}</h2>
      {question.message && <p id="question-message" className="mt-3 whitespace-pre-wrap break-words text-sm text-ink-body">
        {question.message}
      </p>}
      {question.initialValue !== undefined && (question.multiline
        ? <textarea ref={textarea} aria-labelledby="question-title" rows={3}
          className="mt-3 w-full rounded-md border border-line-strong bg-canvas px-3 py-2 text-sm text-ink"
          value={value} onChange={(event) => setValue(event.target.value)} />
        : <input ref={input} aria-labelledby="question-title"
        className="mt-3 w-full rounded-md border border-line-strong bg-canvas px-3 py-2 text-sm text-ink"
        value={value} onChange={(event) => setValue(event.target.value)} />)}
      <div className="mt-5 flex flex-wrap justify-end gap-2">
        {question.cancelLabel !== null && <button ref={cancel} type="button" className="secondary"
          onClick={() => onAnswer(null)}>{question.cancelLabel ?? "取消"}</button>}
        {question.alternativeLabel && <button type="button" className="secondary"
          onClick={() => onAnswer("alternative")}>{question.alternativeLabel}</button>}
        <button type="submit" className="primary">{question.confirmLabel}</button>
      </div>
    </form>
  </dialog>;
}

export function QuestionProvider({ children }: { children: ReactNode }) {
  const [question, setQuestion] = useState<Request>();
  const active = useRef<Request>();

  const ask = useCallback((options: Question) => {
    if (active.current) return Promise.resolve(null);
    return new Promise<string | null>((resolve) => {
      active.current = { ...options, resolve };
      setQuestion(active.current);
    });
  }, []);

  function answer(value: string | null) {
    const request = active.current;
    active.current = undefined;
    setQuestion(undefined);
    request?.resolve(value);
  }

  return <QuestionContext.Provider value={ask}>
    {children}
    {question && <QuestionDialog question={question} onAnswer={answer} />}
  </QuestionContext.Provider>;
}

export function useQuestion() {
  const ask = useContext(QuestionContext);
  if (!ask) throw new Error("QuestionProvider is required");
  const confirm = useCallback(async (question: Question) => await ask(question) === "confirm", [ask]);
  const choose = useCallback(async (question: Question & { alternativeLabel: string }) => {
    const answer = await ask(question);
    return answer === null ? null : answer === "confirm";
  }, [ask]);
  return { confirm, choose, prompt: ask };
}
