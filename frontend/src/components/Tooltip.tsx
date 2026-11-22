import { ReactNode, useId, useState } from "react";
import { createPortal } from "react-dom";

export function Tooltip({ text, children }: { text: string; children: ReactNode }) {
  const id = useId();
  const [position, setPosition] = useState<{ left: number; top: number }>();
  function show(element: HTMLElement) {
    const rect = element.getBoundingClientRect();
    setPosition({ left: Math.min(rect.left, window.innerWidth - Math.min(300, window.innerWidth - 16) - 8), top: rect.bottom + 6 });
  }
  return <span className="inline-block" aria-describedby={position ? id : undefined}
    onMouseEnter={(event) => show(event.currentTarget)} onMouseLeave={() => setPosition(undefined)}
    onFocus={(event) => show(event.currentTarget)} onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) setPosition(undefined);
    }}>
    {children}
    {position && createPortal(<span id={id} role="tooltip" style={{ position: "fixed", left: position.left, top: position.top, zIndex: 1000 }}
      className="max-w-[min(300px,calc(100vw-16px))] rounded border border-slate-300 bg-white px-2 py-1 text-left text-xs font-normal text-slate-900 shadow-md">
      {text}
    </span>, document.body)}
  </span>;
}
