export function formatDuration(milliseconds: number) {
  const seconds = Math.ceil(milliseconds / 1000);
  return `${Math.floor(seconds / 60)}分${seconds % 60}秒`;
}
