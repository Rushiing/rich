import type { Metadata, Viewport } from "next";
import "./globals.css";
import ChangelogModal from "./_components/ChangelogModal";
import TopNav from "./_components/TopNav";

export const metadata: Metadata = {
  title: "RICH",
  description: "A股盯盘与深度解析（内部使用）",
  manifest: "/manifest.webmanifest",
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: "RICH",
  },
  icons: {
    icon: "/icon.svg",
    apple: "/icon.svg",
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // 6/3: light mode dropped — too many color contrast / layout gaps to fix
  // properly. App is dark-only now. status bar locked to dark too.
  themeColor: "#0a0a0a",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    // data-theme="dark" 写死: 兜底任何残留 localStorage.theme="light" 的
    // 老用户回访时 CSS 仍走 dark 调色板。
    <html lang="zh-CN" data-theme="dark">
      <body
        style={{
          fontFamily:
            "-apple-system, BlinkMacSystemFont, 'PingFang SC', 'Helvetica Neue', Arial, sans-serif",
          minHeight: "100vh",
        }}
      >
        <TopNav />
        {children}
        <ChangelogModal />
      </body>
    </html>
  );
}
