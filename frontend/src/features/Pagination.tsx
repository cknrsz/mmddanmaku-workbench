import { useState } from "react";

interface PaginationProps {
  label: string;
  page: number;
  totalPages: number;
  disabled: boolean;
  onPageChange: (page: number) => void;
}

export function Pagination({ label, page, totalPages, disabled, onPageChange }: PaginationProps) {
  const [draft, setDraft] = useState<string | null>(null);
  const buttonClass = "secondary inline-grid size-8 place-items-center p-0";
  const symbolClass = "-translate-y-[3px] text-2xl leading-none";
  const pageClass = "relative -top-px inline-block h-5 rounded border px-0.5 text-center align-middle font-semibold leading-none tabular-nums";
  const pageStyle = { width: `calc(${Math.max(2, String(totalPages).length)}ch + 0.5rem)` };

  return (
    <nav aria-label={label} className="flex shrink-0 flex-wrap items-center justify-center gap-x-2 gap-y-1 text-xs">
      <div className="flex gap-1">
        <button type="button" className={buttonClass} disabled={disabled || page === 1}
          aria-label="首页" title="首页" onClick={() => onPageChange(1)}><span className={symbolClass} aria-hidden="true">«</span></button>
        <button type="button" className={buttonClass} disabled={disabled || page === 1}
          aria-label="上一页" title="上一页" onClick={() => onPageChange(page - 1)}><span className={symbolClass} aria-hidden="true">‹</span></button>
      </div>
      <span aria-live="polite" className="whitespace-nowrap text-ink-muted">
        第 {draft === null ? (
          <button type="button" disabled={disabled} title="单击编辑页码，点击其他位置确认"
            aria-label={`第 ${page} 页，编辑页码`}
            className={`${pageClass} cursor-text border-dashed border-current hover:bg-white/10 disabled:cursor-default`}
            style={pageStyle}
            onClick={() => setDraft(String(page))}>{page}</button>
        ) : (
          <input type="text" inputMode="numeric" aria-label={`跳转页码，范围 1 至 ${totalPages}`}
            className={`${pageClass} border-current bg-panel`}
            style={pageStyle}
            autoFocus disabled={disabled} value={draft}
            onFocus={(event) => event.currentTarget.select()}
            onChange={(event) => setDraft(event.target.value)}
            onBlur={() => {
              const value = draft.trim();
              const next = Number(value);
              setDraft(null);
              if (/^\d+$/.test(value) && next >= 1 && next <= totalPages && next !== page) onPageChange(next);
            }}
            onKeyDown={(event) => {
              if (event.key === "Escape") setDraft(null);
              if (event.key !== "Enter" || event.nativeEvent.isComposing) return;
              event.preventDefault();
              event.currentTarget.blur();
            }} />
        )} 页 / 共 {totalPages} 页
      </span>
      <div className="flex gap-1">
        <button type="button" className={buttonClass} disabled={disabled || page === totalPages}
          aria-label="下一页" title="下一页" onClick={() => onPageChange(page + 1)}><span className={symbolClass} aria-hidden="true">›</span></button>
        <button type="button" className={buttonClass} disabled={disabled || page === totalPages}
          aria-label="末页" title="末页" onClick={() => onPageChange(totalPages)}><span className={symbolClass} aria-hidden="true">»</span></button>
      </div>
    </nav>
  );
}
