import { useLayoutEffect, useRef, useState } from "react";
import { PreloadPart, VideoPart } from "../api/workbench";

export function PreloadPicker({ entries, pending, onSelect, onClose }: {
  entries: PreloadPart[]; pending: boolean;
  onSelect: (part: VideoPart) => Promise<void>; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [selected, setSelected] = useState<number>();
  const [error, setError] = useState("");
  const allCreated = entries.every((entry) => entry.already_created);

  useLayoutEffect(() => {
    const element = dialog.current!;
    element.showModal();
    return () => element.close();
  }, []);

  return <dialog ref={dialog} aria-labelledby="preload-title"
    onCancel={(event) => { event.preventDefault(); if (!pending) onClose(); }}
    className="w-[calc(100%_-_2rem)] max-w-lg rounded-lg border border-line bg-panel p-5 text-ink shadow-xl backdrop:bg-scrim">
    <form onSubmit={(event) => {
      event.preventDefault();
      if (selected === undefined || pending || entries[selected].already_created) return;
      setError("");
      void onSelect(entries[selected].part).catch((reason: unknown) =>
        setError(reason instanceof Error ? reason.message : "创建预装项目失败"));
    }}>
      <h2 id="preload-title" className="font-semibold">选择稿件和分 P</h2>
      {allCreated && <p className="mt-2 text-sm text-ink-muted">项目已创建</p>}
      {error && <p role="alert" className="mt-2 text-sm text-danger">{error}</p>}
      <div className="mt-3 max-h-[55dvh] space-y-2 overflow-y-auto">
        {entries.map(({ part, publish_at, already_created }, index) =>
          <label key={`${part.bvid}:${part.page}`} className={`flex items-start gap-2 rounded-md border border-line p-2
            ${already_created ? "opacity-50" : "cursor-pointer"}`}>
            <input type="radio" name="preload-part" className="mt-1" checked={selected === index}
              disabled={already_created || pending} onChange={() => setSelected(index)} />
            <span className="min-w-0 break-words text-sm">
              <span className="block font-medium">{part.video_title || part.bvid}</span>
              <span className="block">P{part.page} {part.title}</span>
              <span className="block text-xs text-ink-muted">{already_created ? "项目已创建"
                : new Date(publish_at * 1000).toLocaleString("zh-CN")}</span>
            </span>
          </label>)}
      </div>
      <div className="mt-4 flex justify-end gap-2">
        <button type="button" className="secondary" disabled={pending} onClick={onClose}>{allCreated ? "关闭" : "取消"}</button>
        {!allCreated && <button type="submit" className="primary"
          disabled={selected === undefined || pending || entries[selected].already_created}>创建项目</button>}
      </div>
    </form>
  </dialog>;
}
