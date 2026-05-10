"use client";

/**
 * Fallback SMS login — kept around so the 3 existing internal users who
 * registered via the dev-mode SMS path can still get in while they
 * migrate to a password. Once everyone has set a password, remove this
 * route + the /api/auth/sms/* endpoints.
 */

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

const PHONE_RE = /^1[3-9]\d{9}$/;
const RESEND_COOLDOWN_S = 60;

export default function SmsLoginPage() {
  const router = useRouter();
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [step, setStep] = useState<"phone" | "code">("phone");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const cooldownTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    return () => {
      if (cooldownTimer.current) clearInterval(cooldownTimer.current);
    };
  }, []);

  function startCooldown(seconds: number) {
    setCooldown(seconds);
    if (cooldownTimer.current) clearInterval(cooldownTimer.current);
    cooldownTimer.current = setInterval(() => {
      setCooldown((c) => {
        if (c <= 1) {
          if (cooldownTimer.current) clearInterval(cooldownTimer.current);
          return 0;
        }
        return c - 1;
      });
    }, 1000);
  }

  async function sendCode(e?: React.FormEvent) {
    e?.preventDefault();
    setError(null);
    if (!PHONE_RE.test(phone)) {
      setError("请输入 11 位手机号");
      return;
    }
    setBusy(true);
    try {
      const res = await fetch("/api/auth/sms/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body.detail || "发送失败");
        return;
      }
      const body = await res.json();
      if (body.dev_mode) {
        setError("（开发模式：验证码 8888）");
      }
      setStep("code");
      startCooldown(body.wait_s ?? RESEND_COOLDOWN_S);
    } catch {
      setError("网络错误");
    } finally {
      setBusy(false);
    }
  }

  async function verify(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (!/^\d{4,6}$/.test(code)) {
      setError("验证码格式错误");
      return;
    }
    setBusy(true);
    try {
      const res = await fetch("/api/auth/sms/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone, code }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body.detail || "验证失败");
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
      <form
        onSubmit={step === "phone" ? sendCode : verify}
        style={{
          width: "100%", maxWidth: 340,
          display: "flex", flexDirection: "column", gap: 12,
        }}
      >
        <h1 style={{ fontSize: 22, margin: 0 }}>rich</h1>
        <p style={{ margin: 0, color: "var(--text-muted)", fontSize: 13 }}>
          {step === "phone"
            ? "短信验证码登录（白名单内手机号）"
            : `验证码已发送至 ${phone}`}
        </p>

        {step === "phone" ? (
          <input
            type="tel"
            inputMode="numeric"
            value={phone}
            onChange={(e) => setPhone(e.target.value.replace(/\D/g, "").slice(0, 11))}
            placeholder="11 位手机号"
            autoFocus
            maxLength={11}
            style={inputStyle}
          />
        ) : (
          <>
            <input
              type="tel"
              inputMode="numeric"
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
              placeholder="4–6 位验证码"
              autoFocus
              maxLength={6}
              style={inputStyle}
            />
            <div style={{ display: "flex", gap: 8, fontSize: 12 }}>
              <button
                type="button"
                onClick={() => setStep("phone")}
                style={{
                  background: "transparent", border: "none",
                  color: "var(--text-soft)", padding: 0, cursor: "pointer",
                }}
              >
                ← 改手机号
              </button>
              <span style={{ flex: 1 }} />
              <button
                type="button"
                disabled={cooldown > 0 || busy}
                onClick={() => sendCode()}
                style={{
                  background: "transparent", border: "none",
                  color: cooldown > 0 ? "var(--text-faint)" : "var(--link)",
                  padding: 0, cursor: cooldown > 0 ? "not-allowed" : "pointer",
                }}
              >
                {cooldown > 0 ? `重发 (${cooldown}s)` : "重发验证码"}
              </button>
            </div>
          </>
        )}

        {error && (
          <div style={{
            color: error.startsWith("（开发模式") ? "#facc15" : "#ff6b6b",
            fontSize: 13,
          }}>
            {error}
          </div>
        )}

        <button
          type="submit"
          disabled={busy || (step === "phone" ? !phone : !code)}
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
          {busy
            ? (step === "phone" ? "发送中…" : "登录中…")
            : (step === "phone" ? "发送验证码" : "登录")
          }
        </button>

        <a href="/login" style={{
          color: "var(--text-faint)", fontSize: 12, textDecoration: "none",
          textAlign: "center", marginTop: 4,
        }}>
          ← 返回密码登录
        </a>
      </form>
    </main>
  );
}

const inputStyle: React.CSSProperties = {
  padding: "10px 12px",
  background: "var(--border-faint)",
  border: "1px solid var(--border-mid)",
  borderRadius: 6,
  color: "var(--text)",
  fontSize: 14,
  letterSpacing: 1,
};
