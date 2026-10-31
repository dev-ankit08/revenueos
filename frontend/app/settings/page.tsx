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
    ["smtp_password", "SMTP password"], ["daily_send_cap", "Daily send cap"], ["per_domain_daily_cap", "Per-company daily cap"]]],
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
