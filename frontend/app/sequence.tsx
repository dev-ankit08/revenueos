"use client";
import { useEffect, useState } from "react";
import { api, post } from "@/lib/api";

const when = (d: string | null) => (d ? new Date(d).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "");

/** Where this contact is in their outreach sequence, with pause / resume / stop and their time zone. */
export default function Sequence({ contactId, timezone }: { contactId: number; timezone?: string | null }) {
  const [items, setItems] = useState<any[]>([]);
  const [tz, setTz] = useState(timezone ?? "");
  const [msg, setMsg] = useState("");

  const load = () => api<any[]>(`/api/enrollments?contact_id=${contactId}`).then(setItems).catch((e) => setMsg(e.message));
  useEffect(() => { load(); }, [contactId]);

  async function act(id: number, action: string) {
    setMsg("");
    try { await post(`/api/enrollments/${id}/${action}`, {}); load(); } catch (e: any) { setMsg(e.message); }
  }

  async function saveTz() {
    setMsg("");
    try {
      await api(`/api/leads/${contactId}/timezone`, { method: "PUT", body: JSON.stringify({ timezone: tz || null }) });
      setMsg("Time zone saved. Follow-ups go out in business hours there.");
    } catch (e: any) { setMsg(e.message); }
  }

  return (
    <div className="stack">
      <div className="row">
        <input style={{ maxWidth: 260 }} placeholder="Time zone, e.g. Europe/Berlin" value={tz}
          onChange={(e) => setTz(e.target.value)} />
        <button onClick={saveTz}>Save time zone</button>
        {msg && <span className="muted">{msg}</span>}
      </div>
      {items.map((e) => (
        <div className="card" key={e.id}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div>
              <span className={`pill ${e.status === "active" ? "tier-A" : e.status === "paused" ? "tier-C" : ""}`}>{e.status}</span>
              {e.paused_reason && <span className="muted"> · {e.paused_reason}</span>}
              {e.next_due_at && <span className="muted"> · next step {when(e.next_due_at)}</span>}
            </div>
            <div className="row">
              {e.status === "active" && <button onClick={() => act(e.id, "pause")}>Pause</button>}
              {e.status === "paused" && <button onClick={() => act(e.id, "resume")}>Resume</button>}
              {(e.status === "active" || e.status === "paused") && <button className="danger" onClick={() => act(e.id, "stop")}>Stop</button>}
            </div>
          </div>
          <table style={{ marginTop: 8 }}>
            <thead><tr><th>Step</th><th>Type</th><th>What</th><th>Status</th><th>When</th></tr></thead>
            <tbody>
              {e.steps.map((s: any, i: number) => (
                <tr key={i}>
                  <td>{s.step}{s.variant === "B" ? "B" : ""} · day {s.send_after_days ?? 0}</td>
                  <td>{s.type.replace("_", " ")}</td>
                  <td>{s.subject ?? s.title ?? ""}</td>
                  <td>{s.status}</td>
                  <td className="muted">{s.sent_at ? `done ${when(s.sent_at)}` : s.scheduled_for ? when(s.scheduled_for) : "waits for earlier steps"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
      {!items.length && <p className="muted">Not in a sequence yet. Write sequence on the Pipeline page starts one.</p>}
    </div>
  );
}
