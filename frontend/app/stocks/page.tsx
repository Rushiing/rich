export default function StocksPage() {
  return (
    <main style={{ padding: 20, maxWidth: 880, margin: "0 auto" }}>
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <h1 style={{ fontSize: 18, margin: 0 }}>盯盘</h1>
        <a
          href="/watchlist"
          style={{ color: "#9ca3af", fontSize: 13, textDecoration: "none", padding: "6px 10px" }}
        >
          自选池
        </a>
      </header>
      <p style={{ color: "#888", fontSize: 13 }}>
        Phase 2 将在此渲染自选池每小时快照与异动信号。
      </p>
    </main>
  );
}
