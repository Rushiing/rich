"use client";

/**
 * Self-service registration with invite code.
 * Form: phone + password + invite_code → POST /api/auth/register → /stocks.
 */

import { useState } from "react";
import { useRouter } from "next/navigation";

const PHONE_RE = /^1[3-9]\d{9}$/;

export default function RegisterPage() {
  const router = useRouter();
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [inviteCode, setInviteCode] = useState("");
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
    if (password !== confirmPassword) {
      setError("两次密码不一致");
      return;
    }
    if (!inviteCode.trim()) {
      setError("请填写邀请码");
      return;
    }
    setBusy(true);
    try {
      const res = await fetch("/api/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          phone,
          password,
          invite_code: inviteCode.trim().toUpperCase(),
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body.detail || "注册失败");
        return;
      }
      router.push("/stocks");
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
        <h1 style={{ fontSize: 22, margin: 0 }}>注册 rich 账号</h1>
        <p style={{ margin: 0, color: "var(--text-muted)", fontSize: 13 }}>
          凭邀请码注册。手机号当账号 ID。
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
          placeholder="密码（至少 6 位）"
          autoComplete="new-password"
          maxLength={64}
          style={inputStyle}
        />

        <input
          type="password"
          value={confirmPassword}
          onChange={(e) => setConfirmPassword(e.target.value)}
          placeholder="再次输入密码"
          autoComplete="new-password"
          maxLength={64}
          style={inputStyle}
        />

        <input
          type="text"
          value={inviteCode}
          onChange={(e) => setInviteCode(e.target.value.toUpperCase().slice(0, 32))}
          placeholder="邀请码"
          autoCapitalize="characters"
          maxLength={32}
          style={{ ...inputStyle, fontFamily: "monospace", letterSpacing: 2 }}
        />

        {error && (
          <div style={{ color: "#ff6b6b", fontSize: 13 }}>{error}</div>
        )}

        <button
          type="submit"
          disabled={busy || !phone || !password || !confirmPassword || !inviteCode}
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
          {busy ? "注册中…" : "注册并登录"}
        </button>

        <a href="/login" style={{
          color: "var(--text-faint)", fontSize: 12, textDecoration: "none",
          textAlign: "center", marginTop: 4,
        }}>
          ← 已有账号，去登录
        </a>
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
