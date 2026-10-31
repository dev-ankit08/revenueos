"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import { api, post, waitForRun } from "@/lib/api";

export default function Intel() {
  const [signals, setSignals] = useState<any[]>([]);
  const [cards, setCards] = useState<any[]>([]);
  const [sourced, setSourced] = useState<any[]>([]);
  const [competitor, setCompetitor] = useState("");
  const [sourceReq, setSourceReq] = useState({ request: "", count: 10 });
  const [openCard, setOpenCard] = useState<number | null>(null);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");

  const load = () => {
    api("/api/signals").then(setSignals).catch((e) => setMsg(e.message));
    api("/api/intel/battlecards").then(setCards).catch(() => {});
    api("/api/accounts?source=sourced").then(setSourced).catch(() => {});
  };
  useEffect(load, []);

  async function run(label: string, path: string, body: unknown) {
    setBusy(label);
    setMsg("");
    try {
      const { run_id } = await post(path, body);
      const r = await waitForRun(run_id);
      setMsg(r.status === "failed" ? `Failed: ${r.error}` : r.output ?? "Done");
    } catch (e: any) { setMsg(e.message); }
    setBusy("");
    load();
  }

  async function setSignal(id: number, status: string) {
    await api(`/api/signals/${id}`, { method: "PATCH", body: JSON.stringify({ status }) });
    load();
  }

  async function importCsv(file: File) {
    const fd = new FormData();
    fd.append("file", file);
    try {
      const r = await api("/api/leads/import-csv", { method: "POST", body: fd });
      setMsg(`Imported ${r.imported} contacts (${r.skipped} skipped). Columns: ${Object.values(r.columns_used).join(", ")}`);
    } catch (e: any) { setMsg(e.message); }
  }

  return (
    <>
      <h1>Market intel</h1>
      <p className="sub">Buying signals on your accounts, competitor battlecards, and new ICP-fit companies.</p>
      {(busy || msg) && <p className="muted">{busy ? `${busy}… (agents researching the web, 1-4 min)` : msg}</p>}

      <h2>Buying signals</h2>
      <div className="row" style={{ marginBottom: 10 }}>
        <button className="primary" disabled={!!busy} onClick={() => run("Scanning for signals", "/api/intel/signals", { days: 90, limit: 10 })}>
          Scan top accounts (last 90 days)
        </button>
      </div>
      <div className="stack">
        {signals.map((s) => (
          <div className="card" key={s.id}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <div>
                <span className={`pill strength-${s.strength}`}>{s.strength}</span>{" "}
                <span className="pill">{s.type.replace("_", " ")}</span>{" "}
                <b>{s.account}</b>: {s.headline}
                {s.event_date && <span className="muted"> · {s.event_date}</span>}
              </div>
              <div className="row">
                <button onClick={() => setSignal(s.id, "actioned")}>Done</button>
                <button className="danger" onClick={() => setSignal(s.id, "dismissed")}>Dismiss</button>
              </div>
            </div>
            {s.detail && <div style={{ marginTop: 6 }}>{s.detail}</div>}
            <div style={{ marginTop: 6 }}><b>Do this:</b> {s.recommended_action}</div>
            <div className="muted" style={{ marginTop: 4 }}>
              <a href={s.url} target="_blank" rel="noreferrer">Source</a>
              {s.contacts.length > 0 && <> · Contacts: {s.contacts.map((c: any, i: number) => (
                <span key={c.contact_id}>{i > 0 && ", "}<Link href={`/pipeline/${c.contact_id}`}>{c.name}</Link></span>
              ))}</>}
            </div>
          </div>
        ))}
        {!signals.length && <p className="muted">No open signals. Run a scan, or set SIGNAL_SCAN_INTERVAL_DAYS for automatic scans.</p>}
      </div>

      <h2>Competitor battlecards</h2>
      <form className="row" style={{ marginBottom: 10 }} onSubmit={(e) => { e.preventDefault(); run(`Researching ${competitor}`, "/api/intel/battlecard", { competitor }); }}>
        <input style={{ maxWidth: 320 }} required placeholder="Competitor name" value={competitor} onChange={(e) => setCompetitor(e.target.value)} />
        <button disabled={!!busy}>Build / refresh battlecard</button>
      </form>
      <div className="stack">
        {cards.map((c) => (
          <div className="card" key={c.id}>
            <a href="#" onClick={(e) => { e.preventDefault(); setOpenCard(openCard === c.id ? null : c.id); }}><b>{c.title}</b></a>{" "}
            <span className="muted">{new Date(c.created_at).toLocaleDateString()}</span>
            {openCard === c.id && <div className="md" style={{ marginTop: 8 }}><ReactMarkdown>{c.content}</ReactMarkdown></div>}
          </div>
        ))}
      </div>

      <h2>Find new accounts</h2>
      <form className="card row" onSubmit={(e) => { e.preventDefault(); run("Sourcing accounts", "/api/intel/source", sourceReq); }}>
        <input style={{ flex: 1, minWidth: 260 }} required minLength={5} placeholder="e.g. Mid-size European logistics companies modernizing their data platform"
          value={sourceReq.request} onChange={(e) => setSourceReq({ ...sourceReq, request: e.target.value })} />
        <input style={{ width: 80 }} type="number" min={1} max={25} value={sourceReq.count}
          onChange={(e) => setSourceReq({ ...sourceReq, count: Number(e.target.value) })} />
        <button disabled={!!busy}>Find</button>
      </form>
      <p className="muted">
        The sourcer adds companies, not people: it never invents names or emails. Add contacts from your data provider (Apollo, ZoomInfo, Sales Navigator) via CSV:{" "}
        <label className="btn">Import contacts CSV<input type="file" accept=".csv" hidden onChange={(e) => e.target.files?.[0] && importCsv(e.target.files[0])} /></label>
      </p>
      <table>
        <thead><tr><th>Company</th><th>Industry / size</th><th>Why it fits</th><th>Contacts</th></tr></thead>
        <tbody>
          {sourced.map((a) => (
            <tr key={a.id}>
              <td><b>{a.name}</b><div className="muted">{a.domain}</div></td>
              <td>{a.industry}<div className="muted">{a.employees ? `${a.employees.toLocaleString()} employees` : ""} {a.country}</div></td>
              <td>{a.fit_reason}<div className="muted">{a.notes}</div></td>
              <td>{a.contacts}</td>
            </tr>
          ))}
          {!sourced.length && <tr><td colSpan={4} className="muted">No sourced accounts yet.</td></tr>}
        </tbody>
      </table>
    </>
  );
}
