"use client";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import { api, API_URL, authToken, post, waitForRun } from "@/lib/api";

const money = (x: number | null | undefined) => (x ? x.toLocaleString(undefined, { maximumFractionDigits: 0 }) : "–");
const pct = (x: number | null | undefined) => (x === null || x === undefined ? "–" : `${x}%`);

function Stat({ label, value }: { label: string; value: string | number }) {
  return <div className="card stat"><div className="label">{label}</div><div className="value">{value}</div></div>;
}

function RateTable({ title, rows, keyName }: { title: string; rows: any[]; keyName: string }) {
  return (
    <>
      <h2>{title}</h2>
      <table>
        <thead><tr><th>{keyName}</th><th>Sent</th><th>Replies</th><th>Reply rate</th><th>Positive rate</th></tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}><td>{r.agent ?? r.step}</td><td>{r.sent}</td><td>{r.replies}</td>
              <td>{pct(r.reply_rate)}</td><td>{pct(r.positive_rate)}</td></tr>
          ))}
          {!rows.length && <tr><td colSpan={5} className="muted">No emails sent yet.</td></tr>}
        </tbody>
      </table>
    </>
  );
}

export default function Insights() {
  const [target, setTarget] = useState("");
  const [nums, setNums] = useState<any>(null);
  const [report, setReport] = useState<any>(null);
  const [attr, setAttr] = useState<any>(null);
  const [ab, setAb] = useState<any[]>([]);
  const [playbook, setPlaybook] = useState<any>(null);
  const [lift, setLift] = useState<any>(null);
  const [roi, setRoi] = useState<any>(null);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");

  const load = () => {
    api(`/api/forecast/numbers${target ? `?target=${Number(target)}` : ""}`).then(setNums).catch((e) => setMsg(e.message));
    api("/api/forecast/latest").then(setReport).catch(() => {});
    api("/api/insights/attribution").then(setAttr).catch(() => {});
    api("/api/insights/ab-tests").then(setAb).catch(() => {});
    api("/api/insights/playbook").then(setPlaybook).catch(() => {});
    api("/api/insights/lift").then(setLift).catch(() => {});
    api("/api/insights/roi").then(setRoi).catch(() => {});
  };
  useEffect(load, []);

  async function run(label: string, path: string, body?: unknown) {
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

  return (
    <>
      <h1>Revenue insights</h1>
      <p className="sub">Forecast, what's driving pipeline, which messages win, and the playbook the agents learn from it.</p>
      <div className="row" style={{ marginBottom: 12 }}>
        <input style={{ maxWidth: 200 }} placeholder="Quarterly target" inputMode="numeric" value={target}
          onChange={(e) => setTarget(e.target.value)} onBlur={load} />
        <button disabled={!!busy} onClick={() => run("Reviewing every open deal", "/api/deals/review-all")}>Review all deals</button>
        <button className="primary" disabled={!!busy}
          onClick={() => run("Building forecast", "/api/forecast", { target: target ? Number(target) : null })}>Run forecast</button>
        <button disabled={!!busy} onClick={() => run("Learning from results", "/api/insights/learn")}>Update playbook</button>
        {(busy || msg) && <span className="muted">{busy ? `${busy}…` : msg}</span>}
      </div>

      {nums && (
        <>
          <h2>Forecast {nums.quarter}</h2>
          <div className="grid">
            <Stat label="Closed won (quarter)" value={money(nums.closed_won_this_quarter)} />
            <Stat label="Commit" value={money(nums.commit)} />
            <Stat label="Best case" value={money(nums.best_case)} />
            <Stat label="Weighted" value={money(nums.weighted)} />
            <Stat label="Quarter pipeline" value={money(nums.quarter_pipeline)} />
            {nums.target && <Stat label="Projected attainment" value={pct(nums.projected_attainment)} />}
            {nums.target && <Stat label="Coverage" value={nums.coverage_ratio ? `${nums.coverage_ratio}x` : "–"} />}
            <Stat label="Historical win rate" value={pct(nums.historical_win_rate)} />
          </div>
          {(nums.slipped_deals.length > 0 || nums.deals_missing_amount > 0 || nums.deals_missing_close_date > 0) && (
            <div className="flag warn" style={{ marginTop: 10 }}>
              {nums.slipped_deals.length > 0 && <>Slipped close dates: {nums.slipped_deals.join(", ")}. </>}
              {nums.deals_missing_amount > 0 && <>{nums.deals_missing_amount} deals have no amount. </>}
              {nums.deals_missing_close_date > 0 && <>{nums.deals_missing_close_date} deals have no close date.</>}
            </div>
          )}
        </>
      )}
      {report?.content && <div className="card md" style={{ marginTop: 12 }}>
        <div className="muted">{new Date(report.created_at).toLocaleString()}</div>
        <ReactMarkdown>{report.content}</ReactMarkdown>
      </div>}

      {roi && (
        <>
          <h2>Return on AI spend</h2>
          <div className="grid">
            <Stat label="AI spend" value={`$${roi.roi.ai_spend_usd.toLocaleString()}`} />
            <Stat label="Cost per meeting" value={roi.roi.cost_per_meeting != null ? `$${roi.roi.cost_per_meeting}` : "–"} />
            <Stat label="AI-influenced deals" value={roi.roi.influenced_opportunities} />
            <Stat label="Influenced pipeline" value={money(roi.roi.influenced_pipeline)} />
            <Stat label="Influenced won" value={money(roi.roi.influenced_won)} />
            <Stat label="Won per $1 of AI" value={roi.roi.won_per_dollar_of_ai ?? "–"} />
          </div>
          <p className="muted">{roi.roi.definition}{" "}
            <a href="#" onClick={async (e) => {
              e.preventDefault();
              const token = authToken();
              const res = await fetch(`${API_URL}/api/pilot/report.md`, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
              const url = URL.createObjectURL(await res.blob());
              Object.assign(document.createElement("a"), { href: url, download: "pilot-report.md" }).click();
            }}>Download the pilot report</a></p>
          <h2>Does personalisation pay?</h2>
          <table>
            <thead><tr><th>Personalisation score</th><th>Emails sent</th><th>Positive replies</th><th>Positive reply rate</th></tr></thead>
            <tbody>{roi.personalisation_vs_replies.map((b: any) => (
              <tr key={b.score}><td>{b.score}</td><td>{b.sent}</td><td>{b.positive_replies}</td><td>{pct(b.positive_rate)}</td></tr>
            ))}</tbody>
          </table>
        </>
      )}

      {lift && (
        <>
          <h2>Does the AI outreach work? (holdout test)</h2>
          <p className="muted">Eligible leads in outbound batches are randomly split: most get AI outreach, a control
            group gets none. The difference is what the outreach adds.</p>
          <table>
            <thead><tr><th>Group</th><th>Contacts</th><th>Meetings</th><th>Meeting rate</th><th>Deals</th><th>Pipeline</th><th>Won</th></tr></thead>
            <tbody>
              {(["treatment", "holdout"] as const).map((g) => (
                <tr key={g}><td>{g === "treatment" ? "AI outreach" : "Control (no outreach)"}</td><td>{lift[g].contacts}</td>
                  <td>{lift[g].meetings}</td><td>{pct(lift[g].meeting_rate)}</td><td>{lift[g].opportunities}</td>
                  <td>{money(lift[g].pipeline)}</td><td>{money(lift[g].won)}</td></tr>
              ))}
            </tbody>
          </table>
          <p><b>{lift.verdict}</b></p>
        </>
      )}

      <h2>A/B tests</h2>
      <p className="muted">Each contact is randomly assigned one variant; a winner needs 30+ sends per variant and p &lt; 0.05 on positive replies.</p>
      <table>
        <thead><tr><th>Experiment</th><th>A</th><th>B</th><th>Result</th></tr></thead>
        <tbody>
          {ab.map((t, i) => (
            <tr key={i}>
              <td>{t.agent} · step {t.step}</td>
              <td>{t.A.positive_replies}/{t.A.sent} ({pct(t.A.rate)})<div className="muted">{t.A.example_subject}</div></td>
              <td>{t.B.positive_replies}/{t.B.sent} ({pct(t.B.rate)})<div className="muted">{t.B.example_subject}</div></td>
              <td>{t.winner ? <b>{t.verdict}</b> : t.verdict}</td>
            </tr>
          ))}
          {!ab.length && <tr><td colSpan={4} className="muted">No A/B tests with sends yet. Ask for "A/B test on email 1" when writing sequences.</td></tr>}
        </tbody>
      </table>

      {attr && (
        <>
          <h2>Pipeline by source</h2>
          <table>
            <thead><tr><th>Source</th><th>Contacts</th><th>Meetings</th><th>Deals</th><th>Open pipeline</th><th>Won</th></tr></thead>
            <tbody>{attr.by_source.map((r: any) => (
              <tr key={r.source}><td>{r.source}</td><td>{r.contacts}</td><td>{r.meetings}</td><td>{r.opportunities}</td>
                <td>{money(r.pipeline)}</td><td>{money(r.won)}</td></tr>
            ))}</tbody>
          </table>
          <h2>Campaign results</h2>
          <table>
            <thead><tr><th>Campaign</th><th>Members</th><th>Sent</th><th>Reply rate</th><th>Meetings</th><th>Deals</th><th>Pipeline</th><th>Won</th></tr></thead>
            <tbody>
              {attr.by_campaign.map((r: any) => (
                <tr key={r.campaign_id}><td>{r.name}</td><td>{r.members}</td><td>{r.sent}</td><td>{pct(r.reply_rate)}</td>
                  <td>{r.meetings}</td><td>{r.opportunities}</td><td>{money(r.pipeline)}</td><td>{money(r.won)}</td></tr>
              ))}
              {!attr.by_campaign.length && <tr><td colSpan={8} className="muted">No campaigns yet.</td></tr>}
            </tbody>
          </table>
          <RateTable title="Outreach by agent" rows={attr.by_agent} keyName="Agent" />
          <RateTable title="Outreach by sequence step" rows={attr.by_sequence_step} keyName="Step" />
          <p className="muted">Attribution model: {attr.model}.</p>
        </>
      )}

      <h2>Learned playbook</h2>
      {playbook?.content
        ? <div className="card md"><div className="muted">Updated {new Date(playbook.updated_at).toLocaleString()} · used by every writing agent</div>
            <ReactMarkdown>{playbook.content}</ReactMarkdown></div>
        : <p className="muted">No playbook yet. It needs at least 20 sent emails and 3 replies, then <b>Update playbook</b> (or set REVIEW_INTERVAL_DAYS).</p>}
    </>
  );
}
