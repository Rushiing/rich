export default async function StockDetailPage({
  params,
}: {
  params: Promise<{ code: string }>;
}) {
  const { code } = await params;
  return (
    <main style={{ padding: 20, maxWidth: 880, margin: "0 auto" }}>
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <h1 style={{ fontSize: 18, margin: 0, fontFamily: "monospace" }}>{code}</h1>
        <a href="/stocks" style={{ color: "#9ca3af", fontSize: 13, textDecoration: "none" }}>← 返回盯盘</a>
      </header>
      <p style={{ color: "#888", fontSize: 13, marginTop: 16 }}>
        Phase 3 将在此渲染：关键表 + 500 字深度解析 + 重新生成按钮。
      </p>
    </main>
  );
}
