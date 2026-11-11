"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

const MINE_KEY = "revenueos.myday.mine";

function Section({ title, count, children }: { title: string; count: number; children: React.ReactNode }) {
  return (
    <>
      <h2>{title} <span className="muted" style={{ fontWeight: 400 }}>({count})</span></h2>
      {count ? children : <p className="muted">Nothing here.</p>}
    </>
  );
}

export default function MyDay() {
  const [data, setData] = useState<any>(null);
  const [mine, setMine] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => { try { setMine(localStorage.getItem(MINE_KEY) === "1"); } catch {} }, []);
  const load = () => api(`/api/my-day?mine=${mine}`).then(setData).catch((e) => setError(e.message));
  useEffect(() => { load(); }, [mine]);

  async function done(id: number) {
    try { await api(`/api/tasks/${id}`, { method: "PATCH", body: JSON.stringify({ status: "done" }) }); load(); }
    catch (e: any) { setError(e.message); }
  }

  if (!data) return <>{error ? <p className="error">{error}</p> : <p className="muted">Loading…</p>}</>;
  const s = data.summary;
  return (
    <>
      <h1>My day</h1>
      <p className="sub">What needs you today, most urgent first.</p>
      <label className="row" style={{ gap: 6, marginBottom: 12 }}>
        <input type="checkbox" style={{ width: "auto" }} checked={mine}
          onChange={(e) => { setMine(e.target.checked); try { localStorage.setItem(MINE_KEY, e.target.checked ? "1" : "0"); } catch {} }} />
        Only my leads and tasks
      </label>
      {error && <p className="error">{error}</p>}
      <div className="grid">
        <div className="card stat"><div className="label">Past response-time SLA</div><div className="value">{s.sla_breaches}</div></div>
        <div className="card stat"><div className="label">Hot replies</div><div className="value">{s.hot_replies}</div></div>
        <div className="card stat"><div className="label">Tasks due</div><div className="value">{s.tasks_due}</div></div>
        <div className="card stat"><div className="label">Drafts to review</div><div className="value">{s.approvals_waiting}</div></div>
        <div className="card stat"><div className="label">Meetings (48h)</div><div className="value">{s.meetings_48h}</div></div>
      </div>

      {s.failed_sends > 0 && (
        <div className="flag block" style={{ marginTop: 12 }}>
          {s.failed_sends} email(s) failed to send. <Link href="/approvals?status=failed">Check them</Link> before resending.
        </div>
      )}

      <Section title="Waiting past the response-time SLA" count={data.sla_breaches.length}>
        <table><thead><tr><th>Lead</th><th>Stage</th><th>Waiting</th><th>Owner</th></tr></thead><tbody>
          {data.sla_breaches.map((b: any) => (
            <tr key={b.contact_id}><td><Link href={`/pipeline/${b.contact_id}`}>{b.name}</Link> · {b.account}</td>
              <td>{b.lifecycle?.toUpperCase()}</td><td className="error">{b.hours_waiting} h</td><td>{b.owner ?? "–"}</td></tr>
          ))}
        </tbody></table>
      </Section>

      <Section title="Hot replies (last 7 days)" count={data.hot_replies.length}>
        <div className="stack">{data.hot_replies.map((r: any) => (
          <div className="card" key={r.contact_id}>
            <b><Link href={`/pipeline/${r.contact_id}`}>{r.name}</Link></b> <span className="muted">· {r.account} · {r.type.replace("reply_", "")}</span>
            <div>{r.detail}</div>
          </div>
        ))}</div>
      </Section>

      <Section title="Tasks due today and overdue" count={data.tasks.length}>
        <div className="stack">{data.tasks.map((t: any) => (
          <div className="card row" key={t.id} style={{ justifyContent: "space-between" }}>
            <div><span className="pill">{t.type}</span> <b>{t.title}</b>
              {t.contact && <> · <Link href={`/pipeline/${t.contact.id}`}>{t.contact.name}</Link></>}
              <div className="muted">due {new Date(t.due_at).toLocaleString()}</div></div>
            <button className="primary" onClick={() => done(t.id)}>Done</button>
          </div>
        ))}</div>
        <p><Link href="/tasks">All tasks</Link></p>
      </Section>

      <Section title="Drafts waiting for review" count={data.approvals.length}>
        <table><thead><tr><th>Draft</th><th>To</th><th>Waiting</th></tr></thead><tbody>
          {data.approvals.slice(0, 15).map((a: any) => (
            <tr key={a.id}><td>{a.blocked && <span className="pill tier-D">blocked</span>} {a.subject ?? a.kind}</td>
              <td>{a.contact ? `${a.contact} · ${a.account}` : "–"}</td><td>{a.waiting_hours} h</td></tr>
          ))}
        </tbody></table>
        <p><Link href="/approvals">Open Approvals</Link></p>
      </Section>

      <Section title="Customer meetings in the next 48 hours" count={data.meetings.length}>
        <table><thead><tr><th>When</th><th>Meeting</th><th>Prep brief</th></tr></thead><tbody>
          {data.meetings.map((m: any) => (
            <tr key={m.id}><td>{new Date(m.starts_at).toLocaleString()}</td>
              <td>{m.contact_id ? <Link href={`/pipeline/${m.contact_id}`}>{m.title}</Link> : m.title}</td>
              <td>{m.prep_status === "done" ? "ready" : m.prep_status}</td></tr>
          ))}
        </tbody></table>
      </Section>
    </>
  );
}
