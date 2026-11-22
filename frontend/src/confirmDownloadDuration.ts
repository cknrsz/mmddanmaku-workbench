import { formatDuration } from "./formatDuration";

export function confirmDownloadDuration<T extends boolean | Promise<boolean>>(
  sourceMs: number, targetMs: number, confirm: (message: string) => T,
) {
  const differenceMs = Math.abs(sourceMs - targetMs);
  if (differenceMs <= 1000) return true;

  return confirm(`源视频分 P 时长 ${formatDuration(sourceMs)}，目标视频分 P 时长 ${formatDuration(targetMs)}，` +
    `相差 ${differenceMs / 1000} 秒。\n是否继续下载弹幕？`);
}
