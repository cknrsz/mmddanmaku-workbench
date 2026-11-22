import { useLayoutEffect, useRef, useState } from "react";

const graphemes = new Intl.Segmenter(undefined, { granularity: "grapheme" });

// Keep the longest suffix that fits the rendered width and line limit.
// Measure after layout and whenever the title box changes size.
export function ProjectName({ name, lines }: { name: string; lines: 1 | 2 }) {
  const title = useRef<HTMLSpanElement>(null);
  const [label, setLabel] = useState(name);

  useLayoutEffect(() => {
    const element = title.current;
    const parent = element?.parentElement;
    if (!element || !parent) return;

    const measure = () => {
      const width = element.getBoundingClientRect().width;
      if (!width) return;
      const maxHeight = parseFloat(getComputedStyle(element).lineHeight) * lines;
      const probe = document.createElement("span");
      probe.className = element.className;
      Object.assign(probe.style, {
        position: "fixed",
        visibility: "hidden",
        display: "block",
        top: "0",
        left: "0",
        width: `${width}px`,
        WebkitLineClamp: "unset",
      });
      probe.setAttribute("aria-hidden", "true");
      parent.appendChild(probe);

      const fits = (value: string) => {
        probe.textContent = value;
        return probe.scrollWidth <= probe.clientWidth && probe.scrollHeight <= Math.ceil(maxHeight);
      };

      let visible = name;
      if (!fits(name)) {
        const offsets = Array.from(graphemes.segment(name), ({ index }) => index);
        let start = 0;
        let end = offsets.length;
        while (start < end) {
          const middle = Math.floor((start + end) / 2);
          const suffix = `…${name.slice(offsets[middle])}`;
          if (fits(suffix)) end = middle;
          else start = middle + 1;
        }
        visible = `…${name.slice(offsets[start] ?? name.length)}`;
      }
      probe.remove();
      setLabel(visible);
    };

    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [name, lines]);

  return <span ref={title} title={name}
    className={lines === 2 ? "line-clamp-2 w-full break-words font-medium leading-5" : "block truncate"}>
    <span aria-hidden="true">{label}</span>
    <span className="sr-only">{name}</span>
  </span>;
}
