"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api, post, TOKEN_KEY } from "@/lib/api";

const LINKS = [
  ["/", "Dashboard"],
  ["/today", "My day"],
  ["/copilot", "Copilot"],
  ["/pipeline", "Pipeline"],
  ["/approvals", "Approvals"],
  ["/tasks", "Tasks"],
  ["/deals", "Deals"],
  ["/insights", "Insights"],
  ["/evaluate", "Blind test"],
  ["/campaigns", "Campaigns"],
  ["/nurture", "Nurture"],
  ["/events", "Events"],
  ["/customers", "Customers"],
  ["/intel", "Market intel"],
  ["/hygiene", "CRM hygiene"],
  ["/knowledge", "Knowledge base"],
  ["/integrations", "Integrations"],
  ["/governance", "AI governance"],
  ["/runs", "Activity log"],
  ["/salesops", "Sales setup"],
  ["/onboarding", "Setup checklist"],
  ["/settings", "Settings"],
];

export default function Nav() {
  const path = usePathname();
  const [me, setMe] = useState<any>(null);
  useEffect(() => {
    if (!path.startsWith("/embed") && !path.startsWith("/login")) api("/api/auth/me").then(setMe).catch(() => {});
  }, [path]);

  if (path.startsWith("/embed") || path.startsWith("/login")) return null; // no app chrome there

  async function signOut() {
    try { await post("/api/auth/logout"); } catch {}
    try { localStorage.removeItem(TOKEN_KEY); } catch {}
    window.location.href = "/login";
  }

  return (
    <nav className="nav">
      <div className="brand">RevenueOS
        {me?.workspace && <div className="muted" style={{ fontSize: 12, fontWeight: 400 }}>{me.workspace.name}</div>}
      </div>
      {LINKS.map(([href, label]) => (
        <Link key={href} href={href} className={(href === "/" ? path === "/" : path.startsWith(href)) ? "active" : ""}>
          {label}
        </Link>
      ))}
      {me?.role === "owner" && <Link href="/workspaces" className={path.startsWith("/workspaces") ? "active" : ""}>Client workspaces</Link>}
      {me?.user && me.user !== "service" && (
        <div style={{ marginTop: "auto", padding: "12px 10px 0", fontSize: 12 }} className="muted">
          {me.user.email}<br /><a href="#" onClick={(e) => { e.preventDefault(); signOut(); }}>Sign out</a>
        </div>
      )}
    </nav>
  );
}
