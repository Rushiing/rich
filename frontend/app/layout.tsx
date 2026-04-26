import type { Metadata, Viewport } from "next";

export const metadata: Metadata = {
  title: "rich",
  description: "A股盯盘与深度解析（内部使用）",
  manifest: "/manifest.webmanifest",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#0a0a0a",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN">
      <body
        style={{
          margin: 0,
          fontFamily:
            "-apple-system, BlinkMacSystemFont, 'PingFang SC', 'Helvetica Neue', Arial, sans-serif",
          background: "#0a0a0a",
          color: "#e5e5e5",
          minHeight: "100vh",
        }}
      >
        {children}
      </body>
    </html>
  );
}
