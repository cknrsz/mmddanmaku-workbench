"use client";
/*
 * Galactrex UI Components: Pull Cord (free component)
 * Free to use in your own and client projects.
 * Do not resell or republish it as part of a component library or kit.
 * https://galactrex.com/ui/license/
 *
 * Yukkuri skin: copy your two original PNG folders into:
 *   frontend/public/yukkuri/ゆっくりチルノ/...
 *   frontend/public/yukkuri/ゆっくり大妖精/...
 * Preserve the original numbered folders and PNG filenames.
 *
 * onPull retains its original meaning: only a click / qualifying pull
 * invokes it. Every completed drag (including short ones) and click
 * generates a new random portrait. A theme change chooses the other character.
 * Pass theme="light" | "dark" from the parent for reliable synchronization.
 * If omitted, common html/body dark-mode attributes are observed.
 */
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { animate, motion, useMotionTemplate, useMotionValue, useTransform } from "motion/react";
import { cx } from "./motion/cx";
import { useReducedMotion } from "./motion/use-reduced-motion";

type ThemeMode = "light" | "dark";
type AssetGroup = "back" | "body" | "brows" | "eyes" | "mouths" | "tints" | "hair" | "extras";

export type PullCordProps = {
  /** As before: called once per successful pull / click. */
  onPull: () => void;
  label: string;
  /** Resting distance from the mount to the center of the portrait. */
  length?: number;
  threshold?: number;
  /** Keep optional custom-handle support. When omitted, show the Yukkuri face. */
  handle?: ReactNode;
  className?: string;
  /** Recommended: bind this to the theme state already used by your page. */
  theme?: ThemeMode;
  /** Alternative for projects that keep theme as a boolean. */
  isDark?: boolean;
  /** Yukkuri image width (default 58.8px, 30% below the previous 84px). */
  avatarWidth?: number;
};

// Exact filenames from the uploaded 191-PNG metadata inventory.
// All source PNGs are 1500x1300 transparent canvases, so overlay them
// at identical coordinates. Do NOT crop each PNG separately.
const YUKKURI_FILES: Record<ThemeMode, Record<AssetGroup, readonly string[]>> = {
  light: {
    back: ["001_後ろ姿.png", "002_リボン.png"],
    body: ["001_00.png"],
    brows: ["001_06.png", "002_05.png", "003_04.png", "004_03.png", "005_02.png", "006_01.png", "007_00.png"],
    eyes: [
      "001_うるるん.png", "002_I I.png", "003_きゅるん.png", "004_うへえ.png", "005_びっくり.png", "006_猫目.png",
      "007_びっくり.png", "008_ふえ(怒).png", "009_ふえ.png", "010_。。.png", "011_・・.png", "012_ぬーん.png",
      "013_／　＼.png", "014_＼　／.png", "015_点(縦).png", "016_点(横).png", "017___2.png", "018_〉　〈.png",
      "019_ーー.png", "020_すやあ.png", "021_(^^).png", "022_ギクリ.png", "023_ウインク(闇).png", "024_ウインク.png",
      "025_白目.png", "026_え？.png", "027_うるうる.png", "028_わお(闇).png", "029_わお.png", "030_ぐるぐる.png",
      "031_♡.png", "032_キラキラ.png", "033_下目(闇).png", "034_下目.png", "035_右向き(闇).png", "036_右向き.png",
      "037_キリッ2(闇).png", "038_キリッ2.png", "039_キリッ(闇).png", "040_キリッ.png", "041_01.png", "042_01.1.png",
      "043_01.0.png", "044_00.png", "045_00.1.png", "046_00.0.png",
    ],
    mouths: [
      "001_19.png", "002_18.png", "003_17.png", "004_16.png", "005_15.png", "006_14.png",
      "007_13.png", "008_12.png", "009_11.png", "010_10.png", "011_09.png", "012_08.png",
      "013_07.png", "014_06.png", "015_06.0.png", "016_05.png", "017_05.0.png", "018_04.png",
      "019_04.0.png", "020_03.png", "021_03.0.png", "022_02.png", "023_02.0.png", "024_01.png",
      "025_01.0.png", "026_00.png", "027_00.0.png",
    ],
    tints: ["001_悪意.png", "002_赤らめ2.png", "003_赤らめ.png", "004_黒影.png", "005_青ざめ.png"],
    hair: ["001_00.png"],
    extras: [
      "001_たんこぶ.png", "002_( _｀ω´).png", "003_(^_^;).png", "004_涙.png", "005_猫耳.png", "006_サングラス.png",
      "007_眼鏡.png",
    ],
  },
  dark: {
    back: ["001_後ろ姿.png"],
    body: ["001_00.png"],
    brows: ["001_06.png", "002_05.png", "003_04.png", "004_03.png", "005_02.png", "006_01.png", "007_00.png"],
    eyes: [
      "001_うるるん.png", "002_I I.png", "003_きゅるん.png", "004_うへえ.png", "005_びっくり.png", "006_猫目.png",
      "007_びっくり.png", "008_ふえ(怒).png", "009_ふえ.png", "010_。。.png", "011_・・.png", "012_ぬーん.png",
      "013_／　＼.png", "014_＼　／.png", "015_点(縦).png", "016_点(横).png", "017___2.png", "018_〉　〈.png",
      "019_ーー.png", "020_すやあ.png", "021_(^^).png", "022_ギクリ.png", "023_ウインク(闇).png", "024_ウインク.png",
      "025_白目.png", "026_え？.png", "027_うるうる.png", "028_わお(闇).png", "029_わお.png", "030_ぐるぐる.png",
      "031_♡.png", "032_キラキラ.png", "033_下目（闇）.png", "034_下目.png", "035_右向き(闇).png", "036_右向き.png",
      "037_キリッ2（闇）.png", "038_キリッ2.png", "039_キリッ(闇).png", "040_キリッ.png", "041_01.png", "042_01.1.png",
      "043_01.0.png", "044_00.png", "045_00.1.png", "046_00.0.png",
    ],
    mouths: [
      "001_19.png", "002_18.png", "003_17.png", "004_16.png", "005_15.png", "006_14.png",
      "007_13.png", "008_12.png", "009_11.png", "010_10.png", "011_09.png", "012_08.png",
      "013_07.png", "014_06.png", "015_06.0.png", "016_05.png", "017_05.0.png", "018_04.png",
      "019_04.0.png", "020_03.png", "021_03.0.png", "022_02.png", "023_02.0.png", "024_01.png",
      "025_01.0.png", "026_00.png", "027_00.0.png",
    ],
    tints: ["001_悪意.png", "002_赤らめ2.png", "003_赤らめ.png", "004_黒影.png", "005_青ざめ.png"],
    hair: ["001_00.png"],
    extras: [
      "001_たんこぶ.png", "002_( _｀ω´).png", "003_(^_^;).png", "004_涙.png", "005_猫耳.png", "006_サングラス.png",
      "007_眼鏡.png",
    ],
  },
};

const CHARACTER_FOLDER: Record<ThemeMode, string> = {
  light: "ゆっくりチルノ",
  dark: "ゆっくり大妖精",
};

const GROUP_FOLDER: Record<AssetGroup, string> = {
  back: "001_後",
  body: "002_体",
  brows: "003_眉",
  eyes: "004_目",
  mouths: "005_口",
  tints: "006_顔色",
  hair: "007_髪",
  extras: "008_他",
};

function pngUrl(mode: ThemeMode, group: AssetGroup, filename: string): string {
  return ["", "yukkuri", CHARACTER_FOLDER[mode], GROUP_FOLDER[group], filename]
    .map((segment) => encodeURIComponent(segment))
    .join("/");
}

export async function yukkuriAssetsReady(): Promise<boolean> {
  const urls = (Object.keys(YUKKURI_FILES) as ThemeMode[]).flatMap((mode) =>
    (Object.keys(YUKKURI_FILES[mode]) as AssetGroup[]).flatMap((group) =>
      YUKKURI_FILES[mode][group].map((file) => pngUrl(mode, group, file))));
  try {
    const results = await Promise.all(urls.map(async (url) => {
      const response = await fetch(url, { cache: "no-store" });
      return response.ok && (response.headers.get("content-type") ?? "").startsWith("image/png");
    }));
    return results.every(Boolean);
  } catch { return false; }
}

type Portrait = {
  mode: ThemeMode;
  brow: string;
  eye: string;
  mouth: string;
  tint: string | null;
  extra: string | null;
};

function oneOf<T,>(items: readonly T[]): T {
  return items[Math.floor(Math.random() * items.length)]!;
}

function nextPortrait(mode: ThemeMode, previous?: Portrait): Portrait {
  const list = YUKKURI_FILES[mode];
  const generate = (): Portrait => ({
    mode,
    brow: oneOf(list.brows),
    eye: oneOf(list.eyes),
    mouth: oneOf(list.mouths),
    // Effects/props are optional; other facial features are always randomized.
    tint: Math.random() < 0.30 ? oneOf(list.tints) : null,
    extra: Math.random() < 0.22 ? oneOf(list.extras) : null,
  });
  let result = generate();
  if (!previous || previous.mode !== mode) return result;

  // A click must actually change something, even on a rare identical draw.
  for (let attempt = 0; attempt < 10 && isSamePortrait(result, previous); attempt++) {
    result = generate();
  }
  if (isSamePortrait(result, previous)) {
    const index = list.eyes.indexOf(previous.eye);
    result = { ...result, eye: list.eyes[(index + 1) % list.eyes.length]! };
  }
  return result;
}

function isSamePortrait(a: Portrait, b: Portrait): boolean {
  return a.mode === b.mode && a.brow === b.brow &&
    a.eye === b.eye && a.mouth === b.mouth &&
    a.tint === b.tint && a.extra === b.extra;
}

function readDomTheme(): ThemeMode {
  if (typeof document === "undefined") return "light";
  const root = document.documentElement;
  const body = document.body;
  for (const element of [root, body]) {
    if (!element) continue;
    const declared = element.getAttribute("data-theme") ??
      element.getAttribute("data-mode") ?? element.getAttribute("data-color-scheme");
    if (declared === "dark" || declared === "night") return "dark";
    if (declared === "light" || declared === "day") return "light";
    if (element.classList.contains("dark")) return "dark";
    if (element.classList.contains("light")) return "light";
  }
  if (typeof window !== "undefined" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches) return "dark";
  return "light";
}

function PortraitLayer({
  mode, group, filename,
}: { mode: ThemeMode; group: AssetGroup; filename: string }) {
  return (
    <img
      aria-hidden="true"
      alt=""
      draggable={false}
      loading="eager"
      decoding="async"
      width={1500}
      height={1300}
      src={pngUrl(mode, group, filename)}
      className="pointer-events-none absolute inset-0 block h-full w-full select-none"
      style={{ objectFit: "fill" }}
    />
  );
}

function YukkuriPortrait({ portrait }: { portrait: Portrait }) {
  const { mode } = portrait;
  const list = YUKKURI_FILES[mode];
  return (
    <span aria-hidden="true" className="pointer-events-none absolute inset-0 block">
      {/* Never combine PNGs from different characters. */}
      {/* Cirno's bow sits behind the front face; the rear-facing head
          is deliberately not combined with front-facing expressions. */}
      {mode === "light" && (
        <PortraitLayer mode={mode} group="back" filename={list.back[1]!} />
      )}
      <PortraitLayer mode={mode} group="body" filename={list.body[0]!} />
      {portrait.tint && <PortraitLayer mode={mode} group="tints" filename={portrait.tint} />}
      <PortraitLayer mode={mode} group="brows" filename={portrait.brow} />
      <PortraitLayer mode={mode} group="eyes" filename={portrait.eye} />
      <PortraitLayer mode={mode} group="mouths" filename={portrait.mouth} />
      <PortraitLayer mode={mode} group="hair" filename={list.hair[0]!} />
      {portrait.extra && <PortraitLayer mode={mode} group="extras" filename={portrait.extra} />}
    </span>
  );
}

/**
 * Original physics are unchanged: same x/y MotionValues, drag limits,
 * threshold check, click tug, and spring parameters. Only the visible
 * rope stroke, its contact point with the larger portrait, and the handle
 * artwork differ.
 */
export function PullCord({
  onPull,
  label,
  length = 200,
  threshold = 44,
  handle,
  className,
  theme,
  isDark,
  avatarWidth = 58.8,
}: PullCordProps) {
  const reduced = useReducedMotion();
  const x = useMotionValue(0);
  const y = useMotionValue(0);
  const pulled = useRef(false);
  const dragged = useRef(false);
  const patternId = useId().replace(/:/g, "");
  const [domTheme, setDomTheme] = useState<ThemeMode>("light");
  const activeMode: ThemeMode = theme ?? (isDark == null ? domTheme : isDark ? "dark" : "light");
  const [portrait, setPortrait] = useState<Portrait>(() => nextPortrait(theme ?? (isDark ? "dark" : "light")));

  useEffect(() => {
    if (theme != null || isDark != null) return;
    const sync = () => setDomTheme(readDomTheme());
    sync();
    const observer = new MutationObserver(sync);
    observer.observe(document.documentElement, {
      attributes: true, attributeFilter: ["class", "style", "data-theme", "data-mode", "data-color-scheme"],
    });
    if (document.body) {
      observer.observe(document.body, {
        attributes: true, attributeFilter: ["class", "style", "data-theme", "data-mode", "data-color-scheme"],
      });
    }
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    media.addEventListener?.("change", sync);
    return () => {
      observer.disconnect();
      media.removeEventListener?.("change", sync);
    };
  }, [theme, isDark]);

  useEffect(() => {
    // Covers real theme changes from onPull and from outside this component.
    setPortrait((previous) =>
      previous.mode === activeMode ? previous : nextPortrait(activeMode, previous)
    );
  }, [activeMode]);

  const shufflePortrait = () => {
    setPortrait((previous) => nextPortrait(activeMode, previous));
  };

  // The MotionValues, constraints and animation constants below are original.
  // Only cosmetic rope endpoint is shifted up to meet the portrait's hair.
  const restingLength = length;
  const faceHeight = avatarWidth * 1300 / 1500;
  const attachOffset = faceHeight / 2 - faceHeight * 0.09;
  const tipX = useTransform(x, (v) => 24 + v);
  const tipY = useTransform(y, (v) => restingLength + v - attachOffset);
  const bendX = useTransform(x, (v) => 24 + v * 0.35);
  const bendY = useTransform(y, (v) => (restingLength + v - attachOffset) * 0.55);
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
    shufflePortrait(); // Every click counts, before theme switching.
    if (reduced) {
      onPull();
      return;
    }
    animate(y, threshold + 12, {
      duration: 0.12,
      ease: "easeIn",
      onComplete: () => { check(); release(); },
    });
  };

  return (
    <div
      className={cx("relative inline-block w-12 select-none", className)}
      style={{ height: restingLength + 64 }}
    >
      <svg
        aria-hidden="true"
        className="pointer-events-none absolute left-0 top-0 overflow-visible"
        width={48}
        height={restingLength + 64}
      >
        <defs>
          <pattern
            id={`${patternId}-hemp`}
            width="9.1"
            height="9.1"
            patternUnits="userSpaceOnUse"
            patternTransform="rotate(40)"
          >
            <rect width="9.1" height="9.1" fill="#d0a373" />
            <path d="M 0 0 V 9.1" stroke="#795337" strokeWidth="1.82" opacity="0.72" />
            <path d="M 2.8 0 V 9.1" stroke="#f0c99a" strokeWidth="1.26" opacity="0.92" />
            <path d="M 6.3 0 V 9.1" stroke="#ac7c52" strokeWidth="0.77" opacity="0.65" />
          </pattern>
        </defs>
        {/* Thick outlined, diagonally wound hemp rope (not a flat line). */}
        <motion.path d={path} fill="none" stroke="#32251d" strokeWidth={7.35} strokeLinecap="round" />
        <motion.path d={path} fill="none" stroke="#bc8959" strokeWidth={5.81} strokeLinecap="round" />
        <motion.path d={path} fill="none" stroke={`url(#${patternId}-hemp)`} strokeWidth={5.18} strokeLinecap="round" />
        <motion.path d={path} fill="none" stroke="#f5cda0" strokeWidth={0.7} strokeLinecap="round" opacity={0.22} />
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
          shufflePortrait(); // Includes sub-threshold drag: portrait changes, theme does not.
          check();
          release();
        }}
        onClick={(event) => { if (event.detail === 0 || !dragged.current) tug(); }}
        style={{ x, y, top: restingLength - 10, left: 14 }}
        className="pointer-events-auto absolute grid size-5 cursor-grab touch-none place-items-center rounded-full outline-none focus-visible:ring-2 focus-visible:ring-accent-focus focus-visible:ring-offset-2 focus-visible:ring-offset-canvas active:cursor-grabbing"
      >
        {handle ?? (
          <span
            className="pointer-events-auto absolute left-1/2 top-1/2 block cursor-grab active:cursor-grabbing"
            style={{
              width: avatarWidth,
              height: faceHeight,
              transform: "translate(-50%, -50%)",
              touchAction: "none",
            }}
          >
            <YukkuriPortrait portrait={portrait} />
          </span>
        )}
      </motion.button>
    </div>
  );
}
