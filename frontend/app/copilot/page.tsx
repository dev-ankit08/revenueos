"use client";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import { post, Run, waitForRun } from "@/lib/api";

type Turn = { role: "user" | "bot"; text: string; run?: Run };

const EXAMPLES = [
  "Score all new leads and tell me which three to prioritize this week.",
  "Research Siemens and draft a 3-step sequence for their Chief Data Officer, with an A/B test on email 1.",
  "Give me a pipeline summary and the next best action for each A-tier lead.",
];

export default function Copilot() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);

  async function send(message: string) {
    if (!message.trim() || busy) return;
    setBusy(true);
    setInput("");
    // Each request is self-contained; include the last answer so follow-ups have context.
    const lastBot = [...turns].reverse().find((t) => t.role === "bot");
    const prompt = lastBot ? `Previous answer for context:\n${lastBot.text}\n\nNew request: ${message}` : message;
    setTurns((t) => [...t, { role: "user", text: message }, { role: "bot", text: "" }]);
    const update = (run: Run) =>
      setTurns((t) => [...t.slice(0, -1), {
        role: "bot", run,
        text: run.status === "failed" ? `**Failed:** ${run.error}` : run.output ?? "",
      }]);
    try {
      const { run_id } = await post<{ run_id: number }>("/api/copilot", { message: prompt });
      update(await waitForRun(run_id, update));
    } catch (e: any) {
      setTurns((t) => [...t.slice(0, -1), { role: "bot", text: `**Error:** ${e.message}` }]);
    }
    setBusy(false);
  }

  return (
    <>
      <h1>Revenue copilot</h1>
      <p className="sub">Ask in plain language. The orchestrator delegates to scoring, research and writing agents.</p>
      <div className="chat">
        {!turns.length && (
          <div className="stack">
            {EXAMPLES.map((e) => (
              <button key={e} style={{ textAlign: "left" }} onClick={() => send(e)}>{e}</button>
            ))}
          </div>
        )}
        {turns.map((t, i) => (
          <div key={i} className={`bubble ${t.role}`}>
            {t.role === "user" ? t.text : (
              <>
                {t.text ? <div className="md"><ReactMarkdown>{t.text}</ReactMarkdown></div> : <span className="muted">Working…</span>}
                {t.run?.children?.length ? (
                  <div className="steps">
                    Agents: {t.run.children.map((c) => `${c.agent} (${c.status})`).join(" → ")}
                    {" · "}cost ${[t.run, ...t.run.children].reduce((s, r) => s + (r.cost_usd || 0), 0).toFixed(3)}
                  </div>
                ) : null}
              </>
            )}
          </div>
        ))}
      </div>
      <form className="row" style={{ marginTop: 20 }} onSubmit={(e) => { e.preventDefault(); send(input); }}>
        <textarea rows={2} style={{ flex: 1 }} value={input} placeholder="e.g. Research Maersk and write a sequence for Elena"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input); } }} />
        <button className="primary" disabled={busy}>{busy ? "Working…" : "Send"}</button>
      </form>
    </>
  );
}
