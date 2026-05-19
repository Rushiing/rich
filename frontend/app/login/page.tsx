"use client";

/**
 * Phone + password login. Replaces the SMS-flow login (which is now
 * fallback-only at /login/sms while existing users migrate over).
 *
 * Form: phone + password → POST /api/auth/login → redirect /stocks.
 * Link to /register for new users with an invite code.
 */

import { useState } from "react";
import { useRouter } from "next/navigation";

const PHONE_RE = /^1[3-9]\d{9}$/;

export default function LoginPage() {
  const router = useRouter();
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (!PHONE_RE.test(phone)) {
      setError("请输入 11 位手机号");
      return;
    }
    if (password.length < 6) {
      setError("密码至少 6 位");
      return;
    }
    setBusy(true);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone, password }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body.detail || "登录失败");
        return;
      }
      router.push("/");
    } catch {
      setError("网络错误");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main style={{
      display: "flex", alignItems: "center", justifyContent: "center",
      minHeight: "100vh", padding: 16,
    }}>
      <form onSubmit={submit} style={formStyle}>
        <h1 style={{ fontSize: 22, margin: 0 }}>rich</h1>
        <p style={{ margin: 0, color: "var(--text-muted)", fontSize: 13 }}>
          手机号 + 密码登录
        </p>

        <input
          type="tel"
          inputMode="numeric"
          value={phone}
          onChange={(e) => setPhone(e.target.value.replace(/\D/g, "").slice(0, 11))}
          placeholder="11 位手机号"
          autoComplete="tel"
          autoFocus
          maxLength={11}
          style={inputStyle}
        />

        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="密码"
          autoComplete="current-password"
          maxLength={64}
          style={inputStyle}
        />

        {error && (
          <div style={{ color: "#ff6b6b", fontSize: 13 }}>{error}</div>
        )}

        <button
          type="submit"
          disabled={busy || !phone || !password}
          style={{
            padding: "10px 12px",
            background: busy ? "var(--text-dim)" : "var(--link)",
            color: "white",
            border: "none",
            borderRadius: 6,
            fontSize: 14,
            cursor: busy ? "not-allowed" : "pointer",
          }}
        >
          {busy ? "登录中…" : "登录"}
        </button>

        <div style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: 12,
          color: "var(--text-muted)",
          marginTop: 4,
        }}>
          <a href="/register" style={{ color: "var(--link)", textDecoration: "none" }}>
            没有账号？凭邀请码注册 →
          </a>
          <a href="/login/sms" style={{ color: "var(--text-faint)", textDecoration: "none" }}>
            短信验证码登录
          </a>
        </div>
      </form>
    </main>
  );
}

const formStyle: React.CSSProperties = {
  width: "100%", maxWidth: 340,
  display: "flex", flexDirection: "column", gap: 12,
};

const inputStyle: React.CSSProperties = {
  padding: "10px 12px",
  background: "var(--border-faint)",
  border: "1px solid var(--border-mid)",
  borderRadius: 6,
  color: "var(--text)",
  fontSize: 14,
  letterSpacing: 1,
};
