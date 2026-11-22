import { useLayoutEffect, useRef, useState } from "react";

// The row height includes spacing between rows. A list with no gap after the
// final row can use that space for the last card.
export function useRowsPerPage(
  rowHeightRem: number, { limit = Infinity, trailingGapRem = 0 } = {},
) {
  const viewport = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState(1);
  useLayoutEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const measure = () => {
      const rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
      const height = element.getBoundingClientRect().height;
      const rows = Math.floor((height + trailingGapRem * rem) / (rowHeightRem * rem));
      setSize(Math.min(limit, Math.max(1, rows)));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [rowHeightRem, limit, trailingGapRem]);
  return { viewport, size, rowHeight: `${rowHeightRem}rem` };
}
