"use client";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import { api, downloadDocument, SalesDoc } from "@/lib/api";

const LABEL: Record<string, string> = {
  meeting_prep: "Meeting prep", call_summary: "Call summary", proposal: "Proposal",
  pitch_deck: "Pitch deck", reply_triage: "Reply triage", hygiene_report: "Hygiene report",
};

/** List of agent-produced documents with inline Markdown view and file download. */
export default function Documents({ docs }: { docs: SalesDoc[] }) {
  const [open, setOpen] = useState<Record<number, string>>({});
  const [error, setError] = useState("");

  async function toggle(id: number) {
    if (open[id] !== undefined) return setOpen((o) => { const n = { ...o }; delete n[id]; return n; });
    const d = await api<SalesDoc>(`/api/documents/${id}`);
    setOpen((o) => ({ ...o, [id]: d.content ?? "" }));
  }

  if (!docs.length) return <p className="muted">No documents yet.</p>;
  return (
    <div className="stack">
      {error && <p className="error">{error}</p>}
      {docs.map((d) => (
        <div className="card" key={d.id}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div>
              <span className="pill">{LABEL[d.kind] ?? d.kind}</span>{" "}
              <a href="#" onClick={(e) => { e.preventDefault(); toggle(d.id); }}><b>{d.title}</b></a>{" "}
              <span className="muted">{new Date(d.created_at).toLocaleString()}</span>
            </div>
            {d.file_name && (
              <button onClick={() => downloadDocument(d.id, d.file_name!).catch((e) => setError(e.message))}>
                Download {d.file_name.endsWith(".pptx") ? ".pptx" : ".docx"}
              </button>
            )}
          </div>
          {open[d.id] !== undefined && <div className="md" style={{ marginTop: 10 }}><ReactMarkdown>{open[d.id]}</ReactMarkdown></div>}
        </div>
      ))}
    </div>
  );
}
