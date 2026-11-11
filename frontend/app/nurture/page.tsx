"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, post } from "@/lib/api";

export default function Nurture() {
  const [flows, setFlows] = useState<any[]>([]);
  const [campaigns, setCampaigns] = useState<any[]>([]);
  const [name, setName] = useState("");
  const [campaignId, setCampaignId] = useState("");
  const [step, setStep] = useState<Record<number, { subject: string; body: string; delay: string }>>({});
  const [msg, setMsg] = useState("");

  const load = () => {
    api<any[]>("/api/nurture").then(setFlows).catch((e) => setMsg(e.message));
    api<any[]>("/api/campaigns").then(setCampaigns).catch(() => {});
  };
  useEffect(load, []);

  async function act(fn: () => Promise<any>, ok: string) {
    setMsg("");
    try { await fn(); setMsg(ok); load(); } catch (e: any) { setMsg(e.message); }
  }

  return (
    <>
      <h1>Nurture</h1>
      <p className="sub">Email series for leads who aren't ready to talk yet. Each step is approved once in Approvals, then sent to every
        member on schedule, in business hours. Anyone who becomes an MQL, replies or opts out leaves the series automatically.</p>
      {msg && <p className="muted">{msg}</p>}
      <div className="row" style={{ marginBottom: 16 }}>
        <input style={{ maxWidth: 280 }} placeholder="Series name, e.g. Data maturity series" value={name} onChange={(e) => setName(e.target.value)} />
        <select style={{ maxWidth: 240 }} value={campaignId} onChange={(e) => setCampaignId(e.target.value)}>
          <option value="">No campaign</option>
          {campaigns.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <button className="primary" disabled={!name} onClick={() => act(() => post("/api/nurture", {
          name, campaign_id: campaignId ? Number(campaignId) : null }), "Series created.")}>Create series</button>
      </div>
      <div className="stack">
        {flows.map((f) => {
          const s = step[f.id] ?? { subject: "", body: "", delay: "7" };
          return (
            <div className="card stack" key={f.id}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <div><b>{f.name}</b> <span className={`pill ${f.status === "active" ? "tier-A" : "tier-C"}`}>{f.status}</span>
                  <span className="muted"> · members: {Object.entries(f.members).map(([k, n]) => `${n} ${k}`).join(", ") || "none"}
                    · {f.exited_to_sales} handed to sales</span></div>
                <div className="row">
                  {f.campaign_id && <button onClick={() => act(() => post(`/api/nurture/${f.id}/enroll`, { from_campaign: true }), "Campaign members enrolled.")}>Enrol campaign members</button>}
                  <button onClick={() => act(() => post(`/api/nurture/${f.id}/${f.status === "active" ? "pause" : "resume"}`), "Updated.")}>
                    {f.status === "active" ? "Pause" : "Resume"}</button>
                </div>
              </div>
              <table><thead><tr><th>Step</th><th>Subject</th><th>Sent after</th><th>Template</th></tr></thead>
                <tbody>{f.steps.map((x: any) => (
                  <tr key={x.position}><td>{x.position}</td><td>{x.subject}</td><td>{x.delay_days} days</td>
                    <td>{x.template_status === "approved" ? "approved" : <Link href="/approvals?kind=nurture_email">{x.template_status}: review</Link>}</td></tr>
                ))}</tbody></table>
              <div className="stack">
                <input placeholder="Subject (merge fields: {{first_name}}, {{company}})" value={s.subject}
                  onChange={(e) => setStep({ ...step, [f.id]: { ...s, subject: e.target.value } })} />
                <textarea rows={4} placeholder="Body. Use {{first_name}}, {{last_name}}, {{company}}, {{title}}." value={s.body}
                  onChange={(e) => setStep({ ...step, [f.id]: { ...s, body: e.target.value } })} />
                <div className="row">
                  <input style={{ maxWidth: 160 }} placeholder="Days after previous" value={s.delay}
                    onChange={(e) => setStep({ ...step, [f.id]: { ...s, delay: e.target.value } })} />
                  <button className="primary" disabled={!s.subject || !s.body} onClick={() => act(async () => {
                    await post(`/api/nurture/${f.id}/steps`, { subject: s.subject, body: s.body, delay_days: Number(s.delay) || 0 });
                    setStep({ ...step, [f.id]: { subject: "", body: "", delay: "7" } });
                  }, "Step added; approve its template in Approvals.")}>Add step</button>
                </div>
              </div>
            </div>
          );
        })}
        {!flows.length && <p className="muted">No series yet.</p>}
      </div>
    </>
  );
}
