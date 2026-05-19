"use client";

/**
 * Global top navigation bar. Mounted once in layout.tsx so every page
 * shares the same brand + links + theme toggle + user chip. Page-level
 * headers keep only their title and page-specific actions.
 *
 * Hidden on the auth pages (/login, /register, /login/sms) — pre-auth
 * users have nothing to navigate to.
 */

import { usePathname } from "next/navigation";
import UserChip from "./UserChip";
import ThemeToggle from "./ThemeToggle";

const LINKS: { href: string; label: string }[] = [
  { href: "/",          label: "首页" },
  { href: "/stocks",    label: "盯盘" },
  { href: "/sectors",   label: "板块" },
  { href: "/watchlist", label: "自选池" },
  { href: "/changelog", label: "更新日志" },
];

function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  // /stocks should stay active on /stocks/[code] too.
  return pathname === href || pathname.startsWith(href + "/");
}

export default function TopNav() {
  const pathname = usePathname() || "/";

  // Auth pages have no nav.
  if (pathname === "/login" || pathname === "/register"
      || pathname.startsWith("/login/")) {
    return null;
  }

  return (
    <nav
      style={{
        display: "flex",
        alignItems: "center",
        gap: 4,
        padding: "8px 16px",
        borderBottom: "1px solid var(--border-faint)",
        background: "var(--surface-alt)",
        position: "sticky",
        top: 0,
        zIndex: 40,
        flexWrap: "wrap",
      }}
    >
      <a
        href="/"
        style={{
          fontSize: 16,
          fontWeight: 700,
          color: "var(--text)",
          textDecoration: "none",
          marginRight: 8,
          letterSpacing: 0.5,
        }}
      >
        RICH
      </a>

      <div style={{ display: "flex", gap: 2, flex: 1, flexWrap: "wrap" }}>
        {LINKS.map((l) => {
          const active = isActive(pathname, l.href);
          return (
            <a
              key={l.href}
              href={l.href}
              style={{
                padding: "5px 10px",
                fontSize: 13,
                borderRadius: 6,
                textDecoration: "none",
                color: active ? "var(--text)" : "var(--text-muted)",
                background: active ? "var(--surface)" : "transparent",
                border: active
                  ? "1px solid var(--border)"
                  : "1px solid transparent",
                fontWeight: active ? 600 : 400,
              }}
            >
              {l.label}
            </a>
          );
        })}
      </div>

      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <ThemeToggle />
        <UserChip />
      </div>
    </nav>
  );
}
