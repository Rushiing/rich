"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

export default function LoginPage() {
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      if (!res.ok) {
        setError("密码错误");
        return;
      }
      router.push("/stocks");
    } catch {
      setError("网络错误");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        minHeight: "100vh",
        padding: 16,
      }}
    >
      <form
        onSubmit={onSubmit}
        style={{
          width: "100%",
          maxWidth: 320,
          display: "flex",
          flexDirection: "column",
          gap: 12,
        }}
      >
        <h1 style={{ fontSize: 20, margin: 0 }}>rich</h1>
        <p style={{ margin: 0, color: "#888", fontSize: 13 }}>内部使用，请输入访问密码</p>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="密码"
          autoFocus
          style={{
            padding: "10px 12px",
            background: "#1a1a1a",
            border: "1px solid #333",
            borderRadius: 6,
            color: "#e5e5e5",
            fontSize: 14,
          }}
        />
        {error && <div style={{ color: "#ff6b6b", fontSize: 13 }}>{error}</div>}
        <button
          type="submit"
          disabled={loading || !password}
          style={{
            padding: "10px 12px",
            background: loading ? "#444" : "#3b82f6",
            color: "white",
            border: "none",
            borderRadius: 6,
            fontSize: 14,
            cursor: loading ? "not-allowed" : "pointer",
          }}
        >
          {loading ? "登录中..." : "登录"}
        </button>
      </form>
    </main>
  );
}
