"use client";
import { useEffect, useState } from "react";
import { api, post } from "@/lib/api";

export default function Events() {
  const [events, setEvents] = useState<any[]>([]);
  const [flows, setFlows] = useState<any[]>([]);
  const [form, setForm] = useState({ name: "", kind: "webinar", starts_at: "", url: "", external_id: "", attended_flow_id: "", noshow_flow_id: "" });
  const [reg, setReg] = useState<Record<number, string>>({});
  const [att, setAtt] = useState<Record<number, string>>({});
  const [msg, setMsg] = useState("");

  const load = () => {
    api<any[]>("/api/events").then(setEvents).catch((e) => setMsg(e.message));
    api<any[]>("/api/nurture").then(setFlows).catch(() => {});
  };
  useEffect(load, []);

  async function act(fn: () => Promise<any>, ok: (r: any) => string) {
    setMsg("");
    try { const r = await fn(); setMsg(ok(r)); load(); } catch (e: any) { setMsg(e.message); }
  }

  const flowSelect = (value: string, onChange: (v: string) => void, label: string) => (
    <select style={{ maxWidth: 220 }} value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">{label}: none</option>
      {flows.map((f) => <option key={f.id} value={f.id}>{label}: {f.name}</option>)}
    </select>
  );

  return (
    <>
      <h1>Events and webinars</h1>
      <p className="sub">Registrants become leads and count as engaged. After the event, attendees and no-shows go into different
        nurture series, and anyone who stayed long or asked a question gets a follow-up task for their owner.</p>
      {msg && <p className="muted">{msg}</p>}
      <div className="card row" style={{ flexWrap: "wrap", marginBottom: 16 }}>
        <input style={{ maxWidth: 260 }} placeholder="Event name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <select style={{ maxWidth: 140 }} value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
          <option>webinar</option><option>conference</option><option>meetup</option><option>other</option>
        </select>
        <input style={{ maxWidth: 220 }} type="datetime-local" value={form.starts_at} onChange={(e) => setForm({ ...form, starts_at: e.target.value })} />
        <input style={{ maxWidth: 200 }} placeholder="Platform event id (optional)" value={form.external_id} onChange={(e) => setForm({ ...form, external_id: e.target.value })} />
        {flowSelect(form.attended_flow_id, (v) => setForm({ ...form, attended_flow_id: v }), "Attended")}
        {flowSelect(form.noshow_flow_id, (v) => setForm({ ...form, noshow_flow_id: v }), "No-show")}
        <button className="primary" disabled={!form.name} onClick={() => act(() => post("/api/events", {
          name: form.name, kind: form.kind, starts_at: form.starts_at ? new Date(form.starts_at).toISOString() : null,
          external_id: form.external_id || null, attended_flow_id: form.attended_flow_id ? Number(form.attended_flow_id) : null,
          noshow_flow_id: form.noshow_flow_id ? Number(form.noshow_flow_id) : null,
        }), () => "Event created.")}>Create event</button>
      </div>
      <div className="stack">
        {events.map((e) => (
          <div className="card stack" key={e.id}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <div><b>{e.name}</b> <span className="pill">{e.kind}</span> <span className="muted">
                {e.starts_at ? new Date(e.starts_at).toLocaleString() : "no date"} · {e.total} registered
                {Object.entries(e.registrations).filter(([k]) => k !== "registered").map(([k, n]) => ` · ${n} ${k.replace("_", "-")}`).join("")}</span></div>
              <span className={`pill ${e.status === "completed" ? "tier-A" : ""}`}>{e.status}</span>
            </div>
            <div className="row">
              <input placeholder="Add registrant by email" value={reg[e.id] ?? ""} onChange={(x) => setReg({ ...reg, [e.id]: x.target.value })} />
              <button disabled={!reg[e.id]} onClick={() => act(() => post(`/api/events/${e.id}/registrations`, { email: reg[e.id] }),
                () => { setReg({ ...reg, [e.id]: "" }); return "Registered."; })}>Register</button>
            </div>
            <textarea rows={3} placeholder={"Attendance: one per line, email[, minutes][, question]\npriya@siemens.com, 52, How long does an assessment take?"}
              value={att[e.id] ?? ""} onChange={(x) => setAtt({ ...att, [e.id]: x.target.value })} />
            <div className="row">
              <button disabled={!att[e.id]} onClick={() => act(() => post(`/api/events/${e.id}/attendance`, {
                attendees: (att[e.id] ?? "").split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
                  const [email, minutes, ...q] = l.split(",").map((s) => s.trim());
                  return { email, minutes: minutes ? Number(minutes) : null, questions: q.join(", ") || null };
                }),
              }), (r) => `${r.attended} attended, ${r.no_show} no-shows.`)}>Save attendance</button>
              <button className="primary" disabled={e.status !== "completed"} onClick={() => act(() => post(`/api/events/${e.id}/follow-up`),
                (r) => `${r.attended_enrolled} attendees and ${r.no_show_enrolled} no-shows enrolled in nurture; ${r.tasks} follow-up tasks.`)}>Run follow-up</button>
            </div>
          </div>
        ))}
        {!events.length && <p className="muted">No events yet.</p>}
      </div>
    </>
  );
}
