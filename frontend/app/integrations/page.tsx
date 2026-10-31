"use client";
import { useEffect, useState } from "react";
import { api, post } from "@/lib/api";

export default function Integrations() {
  const [status, setStatus] = useState<any>(null);
  const [channels, setChannels] = useState<any[]>([]);
  const [accounts, setAccounts] = useState<any[]>([]);
  const [leads, setLeads] = useState<any[]>([]);
  const [unmatched, setUnmatched] = useState<any[]>([]);
  const [meetings, setMeetings] = useState<any[]>([]);
  const [connections, setConnections] = useState<any[]>([]);
  const [form, setForm] = useState({ channel_id: "", name: "", account_id: "", shared: false });
  const [assign, setAssign] = useState<Record<number, { account_id: string; contact_id: string }>>({});
  const [msg, setMsg] = useState("");

  const load = () => {
    api("/api/integrations/status").then(setStatus).catch((e) => setMsg(e.message));
    api("/api/integrations/slack/channels").then(setChannels).catch(() => {});
    api("/api/accounts").then(setAccounts).catch(() => {});
    api("/api/leads").then(setLeads).catch(() => {});
    api("/api/integrations/unmatched").then(setUnmatched).catch(() => {});
    api("/api/integrations/meetings").then(setMeetings).catch(() => {});
    api("/api/integrations/connections").then(setConnections).catch(() => {});
  };
  useEffect(() => {
    load();
    const q = new URLSearchParams(window.location.search);
    if (q.get("connected")) setMsg(`Connected ${q.get("connected")}. Mail and calendar sync every 15 minutes.`);
    if (q.get("error")) setMsg(`Connection failed: ${q.get("error")}`);
  }, []);

  async function connect(provider: string) {
    try {
      const { url } = await api(`/api/integrations/oauth/${provider}/start`);
      window.location.href = url;
    } catch (e: any) { setMsg(e.message); }
  }

  async function disconnect(id: number) {
    if (!confirm("Disconnect this account? Its stored tokens are deleted.")) return;
    await api(`/api/integrations/connections/${id}`, { method: "DELETE" });
    load();
  }

  async function syncNow() {
    try {
      await post("/api/integrations/sync");
      setMsg("Sync started. New emails, meetings and call transcripts will appear on customer timelines.");
    } catch (e: any) { setMsg(e.message); }
  }

  async function optIn(e: React.FormEvent) {
    e.preventDefault();
    try {
      await post("/api/integrations/slack/channels", {
        channel_id: form.channel_id.trim(), name: form.name || null,
        account_id: form.account_id ? Number(form.account_id) : null, shared: form.shared,
      });
      setForm({ channel_id: "", name: "", account_id: "", shared: false });
      load();
    } catch (err: any) { setMsg(err.message); }
  }

  async function optOut(channelId: string) {
    await api(`/api/integrations/slack/channels/${channelId}`, { method: "DELETE" });
    load();
  }

  async function doAssign(id: number) {
    const a = assign[id];
    if (!a?.account_id) return setMsg("Pick an account first.");
    try {
      await post(`/api/integrations/events/${id}/assign`, {
        account_id: Number(a.account_id), contact_id: a.contact_id ? Number(a.contact_id) : null,
      });
      setMsg("Assigned. Calls are analysed in the background.");
      load();
    } catch (err: any) { setMsg(err.message); }
  }

  const Row = ({ label, ok, children }: { label: string; ok?: boolean; children: React.ReactNode }) => (
    <tr><td><b>{label}</b></td><td>{ok === undefined ? "" : ok ? <span className="pill tier-A">connected</span> : <span className="pill tier-C">not set up</span>}</td><td>{children}</td></tr>
  );

  return (
    <>
      <h1>Integrations</h1>
      <p className="sub">Everything the team knows about a customer (Slack, calls, meetings, emails) flows into one timeline that every assistant reads.</p>
      {msg && <p className="muted">{msg}</p>}

      {status && (
        <table>
          <thead><tr><th>Source</th><th>Status</th><th>Setup (give this to IT)</th></tr></thead>
          <tbody>
            <Row label="Slack" ok={status.slack.configured}>Events URL: <code>{status.slack.events_url}</code> · {status.slack.channels} channels opted in</Row>
            <Row label={`CRM: ${status.crm.provider}`} ok={status.crm.provider !== "local"}>
              {status.crm.provider === "local" ? "Using RevenueOS as the CRM. Set CRM_PROVIDER to salesforce, hubspot or dynamics." :
                <>New open leads and deal changes sync every 15 minutes{status.crm.last_pull && <> (last: {new Date(status.crm.last_pull).toLocaleString()})</>}.{" "}
                  <button onClick={async () => { try { const r = await post("/api/crm/sync"); setMsg(`CRM sync: ${JSON.stringify(r)}`); } catch (e: any) { setMsg(e.message); } }}>Sync CRM now</button></>}
            </Row>
            <Row label="Google / Microsoft" ok={status.google.configured || status.microsoft.configured}>OAuth redirect URIs: <code>{status.google.redirect_uri}</code> · <code>{status.microsoft.redirect_uri}</code></Row>
            <Row label="Slack approvals and /revenueos" ok={status.slack_bot.configured}>Interactivity: <code>{status.slack_bot.interactions_url}</code> · Slash command: <code>{status.slack_bot.commands_url}</code>{status.slack_bot.channel && <> · channel {status.slack_bot.channel}</>}</Row>
            <Row label="Gong" ok={status.gong.configured}>API key and secret on the server; calls are imported every 15 minutes</Row>
            <Row label="Zoom" ok={status.zoom.configured}>Webhook URL: <code>{status.zoom.webhook_url}</code> (event: recording.transcript_completed)</Row>
            <Row label="Teams, Meet, Gong, uploads">POST transcripts to <code>{status.transcripts.url}</code> · auth: {status.transcripts.auth}</Row>
            <Row label="Calendar">POST meetings to <code>{status.calendar.url}</code> · auth: {status.calendar.auth}</Row>
            <Row label="Privacy">Raw text deleted after {status.retention_days} days; summaries kept. Drafts under {status.personalisation_threshold}/100 personalisation are sent back once.</Row>
          </tbody>
        </table>
      )}

      <h2>Connected mailboxes and calendars</h2>
      <p className="muted">Each rep connects their own account (read-only). Only emails with known contacts are read; customer meetings get prep briefs, and Teams/Meet transcripts are analysed after the call.</p>
      <div className="row" style={{ marginBottom: 10 }}>
        <button disabled={!status?.google?.configured || !status?.secret_key_set} onClick={() => connect("google")}>Connect Google (Gmail, Calendar, Meet)</button>
        <button disabled={!status?.microsoft?.configured || !status?.secret_key_set} onClick={() => connect("microsoft")}>Connect Microsoft 365 (Outlook, Teams)</button>
        <button onClick={syncNow}>Sync now</button>
        {status && !status.secret_key_set && <span className="error">Set SECRET_KEY on the server first.</span>}
      </div>
      <table>
        <thead><tr><th>Account</th><th>Provider</th><th>Status</th><th>Last synced</th><th></th></tr></thead>
        <tbody>
          {connections.map((c) => (
            <tr key={c.id}><td>{c.account_email}</td><td>{c.provider}</td>
              <td>{c.status === "error" ? <span className="error" title={c.last_error}>error: {String(c.last_error).slice(0, 80)}</span> : c.status}</td>
              <td className="muted">{c.cursors?.mail ? new Date(c.cursors.mail).toLocaleString() : c.cursors?.gong ? new Date(c.cursors.gong).toLocaleString() : "not yet"}</td>
              <td>{c.provider !== "gong" && <button className="danger" onClick={() => disconnect(c.id)}>Disconnect</button>}</td></tr>
          ))}
          {!connections.length && <tr><td colSpan={5} className="muted">No accounts connected.</td></tr>}
        </tbody>
      </table>

      <h2>Slack channels (opt-in)</h2>
      <p className="muted">RevenueOS reads only the channels listed here. Internal channels inform strategy but are never quoted to customers; shared (Slack Connect) channels are the customer's own words.</p>
      <form className="card row" onSubmit={optIn}>
        <input style={{ width: 150 }} required placeholder="Channel ID (C0123…)" value={form.channel_id} onChange={(e) => setForm({ ...form, channel_id: e.target.value })} />
        <input style={{ width: 160 }} placeholder="#name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <select style={{ width: 200 }} value={form.account_id} onChange={(e) => setForm({ ...form, account_id: e.target.value })}>
          <option value="">Account: detect from text</option>
          {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        <label className="row muted" style={{ gap: 4 }}>
          <input type="checkbox" style={{ width: "auto" }} checked={form.shared} onChange={(e) => setForm({ ...form, shared: e.target.checked })} />
          shared with customer
        </label>
        <button className="primary">Opt in</button>
      </form>
      <table style={{ marginTop: 10 }}>
        <thead><tr><th>Channel</th><th>Account</th><th>Type</th><th></th></tr></thead>
        <tbody>
          {channels.filter((c) => c.enabled).map((c) => (
            <tr key={c.id}><td>{c.name ?? c.channel_id}<div className="muted">{c.channel_id}</div></td>
              <td>{c.account ?? <span className="muted">detected from text</span>}</td>
              <td>{c.shared ? "shared with customer" : "internal"}</td>
              <td><button className="danger" onClick={() => optOut(c.channel_id)}>Opt out</button></td></tr>
          ))}
          {!channels.some((c) => c.enabled) && <tr><td colSpan={4} className="muted">No channels opted in.</td></tr>}
        </tbody>
      </table>

      <h2>Needs a home</h2>
      <p className="muted">Calls and messages RevenueOS couldn't link to an account. Assign them; calls are then analysed automatically.</p>
      <div className="stack">
        {unmatched.map((e) => (
          <div className="card" key={e.id}>
            <div><span className="pill">{e.source}</span> <b>{e.title}</b> <span className="muted">{new Date(e.occurred_at).toLocaleString()}</span></div>
            <div className="muted" style={{ margin: "6px 0" }}>{e.preview}…</div>
            <div className="row">
              <select style={{ width: 200 }} value={assign[e.id]?.account_id ?? ""}
                onChange={(ev) => setAssign({ ...assign, [e.id]: { account_id: ev.target.value, contact_id: "" } })}>
                <option value="">Account…</option>
                {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
              {e.source === "call" && (
                <select style={{ width: 200 }} value={assign[e.id]?.contact_id ?? ""}
                  onChange={(ev) => setAssign({ ...assign, [e.id]: { ...assign[e.id], contact_id: ev.target.value } })}>
                  <option value="">Who was the call with?</option>
                  {leads.filter((l) => String(l.account_id) === assign[e.id]?.account_id)
                    .map((l) => <option key={l.contact_id} value={l.contact_id}>{l.name}</option>)}
                </select>
              )}
              <button onClick={() => doAssign(e.id)}>Assign</button>
            </div>
          </div>
        ))}
        {!unmatched.length && <p className="muted">Nothing waiting.</p>}
      </div>

      <h2>Customer meetings</h2>
      <table>
        <thead><tr><th>Meeting</th><th>When</th><th>Prep brief</th></tr></thead>
        <tbody>
          {meetings.map((m) => (
            <tr key={m.id}><td>{m.title}</td><td>{new Date(m.starts_at).toLocaleString()}</td>
              <td>{m.prep_status === "done" ? <a href={`/pipeline/${m.contact_id}`}>ready</a> : m.prep_status}</td></tr>
          ))}
          {!meetings.length && <tr><td colSpan={3} className="muted">No customer meetings received yet.</td></tr>}
        </tbody>
      </table>
    </>
  );
}
