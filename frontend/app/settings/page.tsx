"use client";
import { useEffect, useState } from "react";
import { api, post } from "@/lib/api";

// Settings grouped as people think about them; keys match the backend's WORKSPACE_KEYS.
const GROUPS: [string, [string, string][]][] = [
  ["Company", [["company_name", "Company name"], ["physical_address", "Registered address (email footer)"],
    ["sender_email", "Send emails from"], ["brand_color", "Brand colour (hex, for decks)"]]],
  ["CRM", [["crm_provider", "CRM: local, salesforce, hubspot, dynamics, zoho, pipedrive"], ["crm_field_map", "Stage/status mapping (JSON)"],
    ["sf_username", "Salesforce username"], ["sf_password", "Salesforce password"], ["sf_security_token", "Salesforce security token"],
    ["sf_domain", "Salesforce domain (login / test)"], ["hubspot_token", "HubSpot private app token"],
    ["dynamics_url", "Dynamics URL"], ["dynamics_client_id", "Dynamics client id"], ["dynamics_client_secret", "Dynamics client secret"],
    ["zoho_client_id", "Zoho client id"], ["zoho_client_secret", "Zoho client secret"], ["zoho_refresh_token", "Zoho refresh token"],
    ["zoho_accounts_url", "Zoho accounts URL"], ["zoho_api_domain", "Zoho API domain"],
    ["pipedrive_token", "Pipedrive API token"], ["pipedrive_domain", "Pipedrive domain"]]],
  ["Email sending", [["smtp_host", "SMTP host (empty = dry run)"], ["smtp_port", "SMTP port"], ["smtp_user", "SMTP user"],
    ["smtp_password", "SMTP password"], ["daily_send_cap", "Daily send cap"], ["per_domain_daily_cap", "Per-company daily cap"],
    ["email_provider", "Sending service: smtp, sendgrid, postmark"], ["sendgrid_api_key", "SendGrid API key"],
    ["sendgrid_webhook_public_key", "SendGrid webhook public key"], ["postmark_server_token", "Postmark server token"],
    ["email_tracking", "Open/click tracking (true / false)"]]],
  ["Sequences and links", [["send_window_start_hour", "Send window start hour (0-23)"], ["send_window_end_hour", "Send window end hour (0-24)"],
    ["send_weekdays_only", "Weekdays only (true / false)"], ["default_timezone", "Default time zone (e.g. Europe/London)"],
    ["booking_url", "Meeting booking link (Calendly, Bookings...)"], ["utm_enabled", "UTM tags on our links (true / false)"],
    ["utm_domains", "Our website domains to tag (comma-separated)"]]],
  ["Website, intent, events, customers", [["intent_pages", "High-intent pages (comma-separated paths)"],
    ["intent_create_accounts", "Create accounts from strong intent (true / false)"],
    ["event_engaged_minutes", "Event: minutes attended to count as engaged"], ["renewal_lead_days", "Open renewal deals N days before end"],
    ["linkedin_access_token", "LinkedIn access token (publishing)"], ["linkedin_author_urn", "LinkedIn author (urn:li:organization:...)"]]],
  ["Notifications and privacy", [["digest_hour", "Daily digest hour, local time (-1 = off)"], ["alert_email", "Ops alerts email"],
    ["consent_required_countries", "Countries needing a lawful basis (comma-separated)"]]],
  ["Funnel and research", [["mql_score", "MQL score threshold (0-100)"], ["mql_sla_hours", "MQL first-touch SLA (hours)"],
    ["brief_max_age_days", "Refresh account research after (days)"], ["dossier_max_age_days", "Refresh person research after (days)"]]],
  ["Slack, calls and webhooks", [["slack_signing_secret", "Slack signing secret"], ["slack_bot_token", "Slack bot token"],
    ["slack_approvals_channel", "Slack approvals channel id"], ["zoom_webhook_secret", "Zoom webhook secret"],
    ["webhook_secret", "Webhook secret (X-Webhook-Secret)"], ["gong_access_key", "Gong access key"], ["gong_access_secret", "Gong access secret"]]],
  ["Pilot and automation", [["holdout_pct", "Holdout share (0-1)"], ["quarterly_target", "Quarterly target"],
    ["personalisation_threshold", "Personalisation threshold (0-100)"], ["signal_scan_interval_days", "Signal scan every N days (0 = off)"],
    ["review_interval_days", "Revenue review every N days (0 = off)"]]],
];

export default function Settings() {
  const [current, setCurrent] = useState<any>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [users, setUsers] = useState<any[]>([]);
  const [newUser, setNewUser] = useState({ email: "", name: "", password: "", role: "member" });
  const [msg, setMsg] = useState("");

  const load = () => {
    api("/api/workspace/settings").then(setCurrent).catch((e) => setMsg(e.message));
    api("/api/workspace/users").then(setUsers).catch(() => {});
  };
  useEffect(load, []);

  async function save() {
    try {
      setCurrent(await api("/api/workspace/settings", { method: "PUT", body: JSON.stringify(edits) }));
      setEdits({});
      setMsg("Saved.");
    } catch (e: any) { setMsg(e.message); }
  }

  async function addUser(e: React.FormEvent) {
    e.preventDefault();
    try {
      await post("/api/workspace/users", newUser);
      setNewUser({ email: "", name: "", password: "", role: "member" });
      load();
    } catch (err: any) { setMsg(err.message); }
  }

  if (!current) return <p className="muted">{msg || "Loading…"}</p>;
  return (
    <>
      <h1>Settings</h1>
      <p className="sub">This workspace only. Secrets are stored encrypted and never shown again; leave a field empty to keep it.</p>
      {msg && <p className="muted">{msg}</p>}
      {GROUPS.map(([group, fields]) => (
        <div key={group}>
          <h2>{group}</h2>
          <div className="card grid">
            {fields.map(([key, label]) => {
              const c = current[key] ?? {};
              return (
                <label key={key} className="stack" style={{ gap: 4 }}>
                  <span className="muted">{label}</span>
                  {c.secret
                    ? <input type="password" autoComplete="off" placeholder={c.set ? "•••••• set" : "not set"}
                        value={edits[key] ?? ""} onChange={(e) => setEdits({ ...edits, [key]: e.target.value })} />
                    : <input value={edits[key] ?? (c.value ?? "")} onChange={(e) => setEdits({ ...edits, [key]: e.target.value })} />}
                </label>
              );
            })}
          </div>
        </div>
      ))}
      <div className="row" style={{ marginTop: 12 }}>
        <button className="primary" disabled={!Object.keys(edits).length} onClick={save}>Save settings</button>
      </div>

      <Security />
      <Privacy />

      <h2>People</h2>
      <table>
        <thead><tr><th>Email</th><th>Name</th><th>Role</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id}><td>{u.email}</td><td>{u.name}</td><td>{u.role}</td><td>{u.active ? "active" : "deactivated"}</td>
              <td>{u.active && <button className="danger" onClick={async () => { await api(`/api/workspace/users/${u.id}`, { method: "DELETE" }); load(); }}>Deactivate</button>}</td></tr>
          ))}
          {!users.length && <tr><td colSpan={5} className="muted">No accounts yet: the app is open. Create the owner account on the sign-in page to require sign-in.</td></tr>}
        </tbody>
      </table>
      <form className="card row" style={{ marginTop: 10 }} onSubmit={addUser}>
        <input style={{ width: 200 }} type="email" required placeholder="Email" value={newUser.email} onChange={(e) => setNewUser({ ...newUser, email: e.target.value })} />
        <input style={{ width: 150 }} placeholder="Name" value={newUser.name} onChange={(e) => setNewUser({ ...newUser, name: e.target.value })} />
        <input style={{ width: 200 }} type="password" required minLength={10} autoComplete="new-password" placeholder="Temporary password (10+)"
          value={newUser.password} onChange={(e) => setNewUser({ ...newUser, password: e.target.value })} />
        <select style={{ width: 120 }} value={newUser.role} onChange={(e) => setNewUser({ ...newUser, role: e.target.value })}>
          <option value="member">member</option><option value="admin">admin</option>
        </select>
        <button>Add person</button>
      </form>
    </>
  );
}


function Security() {
  const [me, setMe] = useState<any>(null);
  const [setup, setSetup] = useState<any>(null);
  const [code, setCode] = useState("");
  const [msg, setMsg] = useState("");
  const load = () => api("/api/auth/me").then(setMe).catch(() => {});
  useEffect(() => { load(); }, []);
  if (!me?.user || me.user === "service") return null;

  async function act(fn: () => Promise<any>, ok: string) {
    setMsg("");
    try { await fn(); setMsg(ok); setCode(""); load(); } catch (e: any) { setMsg(e.message); }
  }

  return (
    <>
      <h2>Your sign-in security</h2>
      <div className="card stack" style={{ maxWidth: 560 }}>
        <div>Two-factor sign-in: <b>{me.mfa_enabled ? "on" : "off"}</b></div>
        {!me.mfa_enabled && !setup && <button onClick={() => act(async () => setSetup(await post("/api/auth/mfa/setup")), "")}>Set up two-factor sign-in</button>}
        {setup && !me.mfa_enabled && (
          <>
            <div className="muted">In your authenticator app, add an account with this key (or open the link on your phone):</div>
            <code style={{ wordBreak: "break-all" }}>{setup.secret}</code>
            <a href={setup.otpauth_uri} className="muted" style={{ wordBreak: "break-all" }}>{setup.otpauth_uri}</a>
            <div className="row"><input style={{ maxWidth: 200 }} inputMode="numeric" placeholder="6-digit code" value={code} onChange={(e) => setCode(e.target.value)} />
              <button className="primary" onClick={() => act(async () => { await post("/api/auth/mfa/enable", { code }); setSetup(null); }, "Two-factor sign-in is on.")}>Turn on</button></div>
          </>
        )}
        {me.mfa_enabled && (
          <div className="row"><input style={{ maxWidth: 200 }} inputMode="numeric" placeholder="Current code" value={code} onChange={(e) => setCode(e.target.value)} />
            <button onClick={() => act(() => post("/api/auth/mfa/disable", { code }), "Two-factor sign-in is off.")}>Turn off</button></div>
        )}
        {msg && <div className="muted">{msg}</div>}
      </div>
    </>
  );
}

function Privacy() {
  const [email, setEmail] = useState("");
  const [msg, setMsg] = useState("");

  async function exportData() {
    setMsg("");
    try {
      const data = await post("/api/privacy/export", { email });
      if (!data.found) return setMsg("Nothing held for that address.");
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `data-export-${email}.json`;
      a.click();
      setMsg("Export downloaded.");
    } catch (e: any) { setMsg(e.message); }
  }

  async function erase() {
    if (!confirm(`Permanently delete everything about ${email}? They will also never be contacted again.`)) return;
    try {
      const r = await post("/api/privacy/erase", { email });
      setMsg(`Erased ${r.erased_contacts} contact(s). The address is on the do-not-contact list.`);
    } catch (e: any) { setMsg(e.message); }
  }

  return (
    <>
      <h2>Data requests (admins)</h2>
      <p className="muted">When someone asks what you hold about them, or asks to be deleted.</p>
      <div className="row">
        <input style={{ maxWidth: 300 }} type="email" placeholder="Their email address" value={email} onChange={(e) => setEmail(e.target.value)} />
        <button disabled={!email} onClick={exportData}>Export their data</button>
        <button className="danger" disabled={!email} onClick={erase}>Erase their data</button>
        {msg && <span className="muted">{msg}</span>}
      </div>
    </>
  );
}
