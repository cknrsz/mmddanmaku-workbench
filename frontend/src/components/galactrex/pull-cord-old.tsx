"use client";
/*
 * Galactrex UI Components: Pull Cord (free component)
 * Free to use in your own and client projects. Do not resell or republish it as part of a component library or kit.
 * https://galactrex.com/ui/license/
 */
import { useRef, type ReactNode } from "react";
import { animate, motion, useMotionTemplate, useMotionValue, useTransform } from "motion/react";
import { cx } from "./motion/cx";
import { useReducedMotion } from "./motion/use-reduced-motion";

export type PullCordProps = {
  /** called once per pull past the threshold */
  onPull: () => void;
  /** what a pull does, for assistive tech, e.g. "Switch to dark theme" */
  label: string;
  /** resting cord length in px */
  length?: number;
  /** how far past rest, in px, counts as a pull */
  threshold?: number;
  /** the handle; defaults to a small bead */
  handle?: ReactNode;
  className?: string;
};

/** A cord that hangs from the top of its parent. Drag the handle down and let go: past the threshold it pulls
    (a click goes through, a light switches, the theme flips), then springs back with a little sway. A click or
    Space/Enter on the handle performs the same pull with the animation, so it never needs dragging. */
export function PullCord({ onPull, label, length = 96, threshold = 44, handle, className }: PullCordProps) {
  const reduced = useReducedMotion();
  const x = useMotionValue(0);
  const y = useMotionValue(0);
  const pulled = useRef(false);
  const dragged = useRef(false);
  const tipX = useTransform(x, (v) => 24 + v);
  const tipY = useTransform(y, (v) => length + v);
  const bendX = useTransform(x, (v) => 24 + v * 0.35);
  const bendY = useTransform(y, (v) => (length + v) * 0.55);
  const path = useMotionTemplate`M 24 0 Q ${bendX} ${bendY} ${tipX} ${tipY}`;

  const release = () => {
    const spring = { type: "spring" as const, stiffness: 260, damping: reduced ? 40 : 9, mass: 0.8 };
    animate(x, 0, spring);
    animate(y, 0, spring);
  };
  const check = () => {
    if (!pulled.current && y.get() > threshold) {
      pulled.current = true;
      onPull();
    }
  };
  const tug = () => {
    pulled.current = false;
    if (reduced) {
      onPull();
      return;
    }
    animate(y, threshold + 12, { duration: 0.12, ease: "easeIn", onComplete: () => { check(); release(); } });
  };

  return (
    <div className={cx("relative inline-block w-12 select-none", className)} style={{ height: length + 64 }}>
      <svg aria-hidden className="pointer-events-none absolute left-0 top-0 overflow-visible" width={48} height={length + 64}>
        <motion.path d={path} fill="none" stroke="var(--gxui-muted)" strokeWidth={1.5} strokeLinecap="round" />
      </svg>
      <motion.button
        type="button"
        aria-label={label}
        drag
        dragMomentum={false}
        dragConstraints={{ left: -28, right: 28, top: 0, bottom: threshold + 40 }}
        dragElastic={0.12}
        onPointerDown={() => { dragged.current = false; }}
        onDragStart={() => { pulled.current = false; dragged.current = true; }}
        onDragEnd={() => {
          check();
          release();
        }}
        onClick={(event) => { if (event.detail === 0 || !dragged.current) tug(); }}
        style={{ x, y, top: length - 10, left: 14 }}
        className="pointer-events-auto absolute grid size-5 cursor-grab touch-none place-items-center rounded-full outline-none focus-visible:ring-2 focus-visible:ring-accent-focus focus-visible:ring-offset-2 focus-visible:ring-offset-canvas active:cursor-grabbing"
      >
        {handle ?? <span aria-hidden className="block size-5 rounded-full border border-ink-quiet bg-gxui-surface" />}
      </motion.button>
    </div>
  );
}
