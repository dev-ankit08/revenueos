"use client";
import { useEffect, useState } from "react";
import { api, post } from "@/lib/api";

const SOURCE: Record<string, string> = {
  slack: "Slack", call: "Call", email: "Email", calendar: "Meeting", crm: "CRM", note: "Note",
};

/** Everything the team knows about an account, from every source, newest first. */
export default function Timeline({ accountId, contactId }: { accountId: number; contactId?: number }) {
  const [events, setEvents] = useState<any[]>([]);
  const [note, setNote] = useState("");
  const [internal, setInternal] = useState(true);
  const [error, setError] = useState("");

  const load = () => api(`/api/accounts/${accountId}/timeline`).then(setEvents).catch((e) => setError(e.message));
  useEffect(() => { load(); }, [accountId]);

  async function addNote(e: React.FormEvent) {
    e.preventDefault();
    try {
      await post(`/api/accounts/${accountId}/notes`, { text: note, contact_id: contactId ?? null, internal });
      setNote("");
      load();
    } catch (err: any) { setError(err.message); }
  }

  return (
    <div className="stack">
      <form className="row" onSubmit={addNote}>
        <input style={{ flex: 1, minWidth: 240 }} required minLength={3} placeholder="Add a note the assistants should know"
          value={note} onChange={(e) => setNote(e.target.value)} />
        <label className="row muted" style={{ gap: 4 }}>
          <input type="checkbox" style={{ width: "auto" }} checked={internal} onChange={(e) => setInternal(e.target.checked)} />
          internal only
        </label>
        <button>Add note</button>
      </form>
      {error && <p className="error">{error}</p>}
      {events.map((e) => (
        <div key={e.id} className="card" style={{ padding: 12 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div>
              <span className="pill">{SOURCE[e.source] ?? e.source}</span>{" "}
              {e.internal && <span className="pill strength-medium" title="Never shown to the customer">internal</span>}{" "}
              <b>{e.title}</b>
            </div>
            <span className="muted">{new Date(e.occurred_at).toLocaleString()}</span>
          </div>
          <div style={{ marginTop: 6 }}>{e.summary}</div>
          {e.facts?.commitments?.length > 0 && (
            <div className="muted" style={{ marginTop: 4 }}><b>Commitments:</b> {e.facts.commitments.join("; ")}</div>
          )}
        </div>
      ))}
      {!events.length && <p className="muted">Nothing yet. Calls, Slack discussions, emails, meetings and notes about this account appear here.</p>}
    </div>
  );
}
