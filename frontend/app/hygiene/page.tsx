"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import { api, post, waitForRun } from "@/lib/api";

export default function Hygiene() {
  const [report, setReport] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  const load = () => api("/api/hygiene/latest").then(setReport).catch((e) => setMsg(e.message));
  useEffect(() => { load(); }, []);

  async function run(enrich: boolean) {
    setBusy(true);
    setMsg("");
    try {
      const { run_id } = await post("/api/hygiene/run", { enrich });
      const r = await waitForRun(run_id);
      if (r.status === "failed") setMsg(`Failed: ${r.error}`);
    } catch (e: any) { setMsg(e.message); }
    setBusy(false);
    load();
  }

  async function merge(keep_id: number, merge_id: number) {
    if (!confirm(`Merge contact #${merge_id} into #${keep_id}? History moves over and #${merge_id} is deleted.`)) return;
    try {
      await post("/api/hygiene/merge-contacts", { keep_id, merge_id });
      await run(false);
    } catch (e: any) { setMsg(e.message); }
  }

  const dups = report?.data?.duplicate_contacts ?? [];
  return (
    <>
      <h1>CRM hygiene</h1>
      <p className="sub">Duplicates, missing data, quiet leads and deals needing attention. A rule-based report runs daily at no AI cost.</p>
      <div className="row" style={{ marginBottom: 16 }}>
        <button className="primary" disabled={busy} onClick={() => run(true)}>Run now + fill missing firmographics</button>
        <button disabled={busy} onClick={() => run(false)}>Run rules only</button>
        {busy && <span className="muted">Running…</span>}
        {msg && <span className="error">{msg}</span>}
      </div>
      {dups.length > 0 && (
        <>
          <h2>Duplicate contacts</h2>
          <div className="stack">
            {dups.map((d: any) => d.merge_ids.map((m: number) => (
              <div className="card row" key={`${d.keep_id}-${m}`} style={{ justifyContent: "space-between" }}>
                <span>
                  {d.name} {d.email && <span className="muted">({d.email})</span>}: keep{" "}
                  <Link href={`/pipeline/${d.keep_id}`}>#{d.keep_id}</Link>, duplicate <Link href={`/pipeline/${m}`}>#{m}</Link>
                </span>
                <button onClick={() => merge(d.keep_id, m)}>Merge</button>
              </div>
            )))}
          </div>
        </>
      )}
      {report && (
        <div className="card md" style={{ marginTop: 16 }}>
          <div className="muted">{report.title}</div>
          <ReactMarkdown>{report.content}</ReactMarkdown>
        </div>
      )}
    </>
  );
}
