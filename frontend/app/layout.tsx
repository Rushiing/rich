import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "rich",
  description: "A股盯盘与深度解析（内部使用）",
  manifest: "/manifest.webmanifest",
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: "rich",
  },
  icons: {
    icon: "/icon.svg",
    apple: "/icon.svg",
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // themeColor is light/dark adaptive — the browser chrome (PWA status bar)
  // matches the user's pick.
  themeColor: [
    { media: "(prefers-color-scheme: dark)",  color: "#0a0a0a" },
    { media: "(prefers-color-scheme: light)", color: "#fafafa" },
  ],
};

// Synchronous boot script — runs before paint to set data-theme so we don't
// flash the wrong palette on hard refresh. Mirrors ThemeToggle's storage
// contract: localStorage.theme = "system" | "light" | "dark", default "system".
const BOOT_THEME = `
(function() {
  try {
    var t = localStorage.getItem("theme");
    if (t === "light" || t === "dark") {
      document.documentElement.setAttribute("data-theme", t);
    }
    // "system" or unset → leave attribute off; CSS @media handles it.
  } catch (e) {}
})();
`;

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN">
      <head>
        <script dangerouslySetInnerHTML={{ __html: BOOT_THEME }} />
      </head>
      <body
        style={{
          fontFamily:
            "-apple-system, BlinkMacSystemFont, 'PingFang SC', 'Helvetica Neue', Arial, sans-serif",
          minHeight: "100vh",
        }}
      >
        {children}
      </body>
    </html>
  );
}
