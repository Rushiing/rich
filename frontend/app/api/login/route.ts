import { NextRequest, NextResponse } from "next/server";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

// Proxies the login to the backend so the browser receives the auth cookie
// from the same origin as the Next.js app. Avoids cross-site cookie config.
export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  const upstream = await fetch(`${API_BASE}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: body.password ?? "" }),
  });

  if (!upstream.ok) {
    return NextResponse.json({ ok: false }, { status: upstream.status });
  }

  const setCookie = upstream.headers.get("set-cookie");
  const res = NextResponse.json({ ok: true });
  if (setCookie) {
    res.headers.set("set-cookie", setCookie);
  }
  return res;
}
