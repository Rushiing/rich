import { NextRequest, NextResponse } from "next/server";

// Pages that don't require auth. /api/* is always passed through — the backend
// is the actual auth gate (returns 401 if needed).
const PUBLIC_PAGES = ["/login"];

export function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl;
  if (pathname.startsWith("/api/")) return NextResponse.next();
  if (PUBLIC_PAGES.some((p) => pathname.startsWith(p))) return NextResponse.next();

  const session = req.cookies.get("rich_session")?.value;
  if (!session) {
    const url = req.nextUrl.clone();
    url.pathname = "/login";
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|manifest.webmanifest).*)"],
};
