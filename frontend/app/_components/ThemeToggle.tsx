"use client";

/**
 * Three-state theme toggle: system (跟随系统) → light → dark → system.
 *
 * State persists in localStorage.theme as one of "system" | "light" | "dark".
 * Default is "system". When set to system we *remove* the data-theme attribute
 * on <html> so the @media (prefers-color-scheme) rule in globals.css takes
 * over; for explicit light/dark we set data-theme to override.
 *
 * The same logic must run synchronously on first paint — see the boot script
 * injected in layout.tsx. This component handles user-driven toggling only.
 */

import { useEffect, useState } from "react";

type ThemeMode = "system" | "light" | "dark";

const STORAGE_KEY = "theme";

function applyTheme(mode: ThemeMode) {
  const html = document.documentElement;
  if (mode === "system") {
    html.removeAttribute("data-theme");
  } else {
    html.setAttribute("data-theme", mode);
  }
}

function readStoredTheme(): ThemeMode {
  if (typeof window === "undefined") return "system";
  const v = window.localStorage.getItem(STORAGE_KEY);
  if (v === "light" || v === "dark" || v === "system") return v;
  return "system";
}

const ICON: Record<ThemeMode, string> = {
  system: "🖥",
  light: "☀",
  dark: "🌙",
};

const LABEL: Record<ThemeMode, string> = {
  system: "跟随系统",
  light: "浅色",
  dark: "深色",
};

const NEXT: Record<ThemeMode, ThemeMode> = {
  system: "light",
  light: "dark",
  dark: "system",
};

export default function ThemeToggle() {
  // mounted-gate prevents hydration mismatch — server has no localStorage.
  const [mode, setMode] = useState<ThemeMode>("system");
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMode(readStoredTheme());
    setMounted(true);
  }, []);

  function cycle() {
    const next = NEXT[mode];
    setMode(next);
    window.localStorage.setItem(STORAGE_KEY, next);
    applyTheme(next);
  }

  // While unmounted, render an invisible placeholder of matching size so the
  // header layout doesn't shift on first paint.
  if (!mounted) {
    return <span style={{ display: "inline-block", width: 32, height: 28 }} />;
  }

  return (
    <button
      type="button"
      onClick={cycle}
      title={`主题：${LABEL[mode]}（点击切换）`}
      aria-label={`主题：${LABEL[mode]}`}
      style={{
        background: "transparent",
        border: "1px solid var(--border-mid)",
        borderRadius: 6,
        color: "var(--text-soft)",
        fontSize: 14,
        cursor: "pointer",
        padding: "4px 8px",
        lineHeight: 1,
        minHeight: 28,
      }}
    >
      {ICON[mode]}
    </button>
  );
}
