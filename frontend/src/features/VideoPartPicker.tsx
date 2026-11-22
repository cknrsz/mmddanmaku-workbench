import { useState } from "react";
import { getVideoParts, VideoPart } from "../api/workbench";

export function VideoPartPicker({ label, part, initialBvid = "", initialPage, disabled, onPart, onError, showLabels = true }: {
  label: string; part?: VideoPart; initialBvid?: string; initialPage?: number | null; disabled: boolean;
  onPart: (part?: VideoPart) => void; onError: (error: unknown) => void; showLabels?: boolean;
}) {
  const [bvid, setBvid] = useState(initialBvid);
  const [parts, setParts] = useState<VideoPart[]>([]);
  const [lookingUp, setLookingUp] = useState(false);

  async function lookup() {
    setLookingUp(true);
    setParts([]);
    onPart();
    try {
      const result = await getVideoParts(bvid, "bilibili");
      if (!result.length) throw new Error("这个视频没有可选择的分 P");
      setParts(result);
      onPart(result.find((entry) => bvid === initialBvid && entry.page === initialPage) ?? result[0]);
    } catch (error) {
      onError(error);
    } finally {
      setLookingUp(false);
    }
  }

  return <>
    <label className="field"><span className={showLabels ? "" : "sr-only"}>{label} BV 号</span>
      <input value={bvid} placeholder="BV…" maxLength={12} disabled={disabled || lookingUp}
        onChange={(event) => {
          setBvid(event.currentTarget.value.trim());
          setParts([]);
          onPart();
        }} />
    </label>
    <button type="button" className="secondary whitespace-nowrap"
      disabled={disabled || lookingUp || !/^BV[0-9A-Za-z]{10}$/.test(bvid)}
      onClick={() => void lookup()}>{lookingUp ? "读取中…" : "读取分 P"}</button>
    <label className="field"><span className={showLabels ? "" : "sr-only"}>{label}分 P</span>
      <select value={part?.page ?? ""} disabled={disabled || lookingUp || !parts.length}
        onChange={(event) => onPart(parts.find((entry) => entry.page === Number(event.currentTarget.value)))}>
        {!parts.length && <option value="">请先读取分 P</option>}
        {parts.map((entry) => <option key={entry.page} value={entry.page}>P{entry.page} {entry.title}</option>)}
      </select>
    </label>
  </>;
}
