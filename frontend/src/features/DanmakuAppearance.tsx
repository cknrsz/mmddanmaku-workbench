const modes: Record<number, string> = {
  1: "普通滚动弹幕", 2: "普通滚动弹幕", 3: "普通滚动弹幕", 4: "底部弹幕",
  5: "顶部弹幕", 6: "逆向滚动弹幕", 7: "高级弹幕", 8: "代码弹幕", 9: "BAS 弹幕",
};

export function danmakuModeLabel(mode: number) {
  return modes[mode] ?? "其他弹幕";
}

export function danmakuColorLabel(rgb: number | null) {
  return rgb === null ? "特殊颜色" : `#${rgb.toString(16).padStart(6, "0")}`;
}

export function DanmakuText({ text, rgb, fontSize }: { text: string; rgb: number | null; fontSize: number }) {
  const brightness = rgb === null ? 255 :
    0.299 * (rgb >> 16) + 0.587 * ((rgb >> 8) & 255) + 0.114 * (rgb & 255);
  return <span className={`inline-block rounded bg-slate-800 px-2 py-0.5 ${fontSize === 18 ? "text-xs" : ""}`}
    style={{ color: rgb === null ? "white" : danmakuColorLabel(rgb), backgroundColor: brightness < 105 ? "#f1f5f9" : undefined }}>
    {text}
  </span>;
}
