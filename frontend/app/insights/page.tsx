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
  const [quality, setQuality] = useState<any>(null);
  const [funnel, setFunnel] = useState<any>(null);
  const [versions, setVersions] = useState<any[]>([]);
  const [reps, setReps] = useState<any>(null);
  const [winLoss, setWinLoss] = useState<any>(null);
  const [mta, setMta] = useState<any>(null);
  const [mtaModel, setMtaModel] = useState("w_shaped");
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
    api("/api/insights/quality").then(setQuality).catch(() => {});
    api("/api/insights/funnel").then(setFunnel).catch(() => {});
    api("/api/insights/playbook/versions").then(setVersions).catch(() => {});
    api("/api/insights/reps").then(setReps).catch(() => {});
    api("/api/insights/win-loss").then(setWinLoss).catch(() => {});
    api("/api/insights/attribution/multi-touch").then(setMta).catch(() => {});
  };
  useEffect(load, []);

  async function playbookAction(path: string) {
    setMsg("");
    try { await post(path); load(); } catch (e: any) { setMsg(e.message); }
  }

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

      {funnel && (
        <>
          <h2>Funnel</h2>
          <p className="muted">MQL = score {funnel.mql_score}+ and engaged (inbound, reply or click). SLA: first human touch within {funnel.sla_hours} hours.</p>
          <div className="grid">
            {Object.entries(funnel.stages).map(([stage, n]: [string, any]) => <Stat key={stage} label={stage.toUpperCase()} value={n} />)}
            <Stat label="MQLs touched within SLA" value={pct(funnel.sla_met_pct)} />
            <Stat label="Waiting past SLA" value={funnel.waiting_past_sla} />
          </div>
        </>
      )}

      {quality && (
        <>
          <h2>Draft accuracy (last {quality.days} days)</h2>
          <p className="muted">How often reviewers send drafts exactly as written. Target: {quality.target_unedited_pct}%+ approved without edits.</p>
          <div className="grid">
            <Stat label="Approved without edits" value={pct(quality.approved_unedited_pct)} />
            <Stat label="Average edit size" value={pct(quality.avg_edit_pct)} />
            <Stat label="Reject rate" value={pct(quality.reject_rate)} />
            <Stat label="Drafts decided" value={quality.decided} />
          </div>
          {Object.keys(quality.by_agent).length > 0 && (
            <table style={{ marginTop: 10 }}>
              <thead><tr><th>Assistant</th><th>Decided</th><th>Approved without edits</th><th>Avg edit</th><th>Reject rate</th><th>Top reject reason</th></tr></thead>
              <tbody>
                {Object.entries(quality.by_agent).map(([agent, q]: [string, any]) => {
                  const top = Object.keys(q.reject_reasons)[0];
                  return (
                    <tr key={agent}><td>{agent}</td><td>{q.decided}</td><td>{pct(q.approved_unedited_pct)}</td>
                      <td>{pct(q.avg_edit_pct)}</td><td>{pct(q.reject_rate)}</td>
                      <td>{top ? `${quality.reason_labels[top] ?? top} (${q.reject_reasons[top]})` : "–"}</td></tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </>
      )}

      {quality?.grader_calibration && (
        <p className="muted">Grader check: {quality.grader_calibration.verdict}
          {quality.grader_calibration["80_plus"].drafts > 0 && <> · 80+ scores sent unedited {pct(quality.grader_calibration["80_plus"].approved_unedited_pct)}
            vs under 60: {pct(quality.grader_calibration.under_60.approved_unedited_pct)}</>}</p>
      )}

      {reps?.reps?.length > 0 && (
        <>
          <h2>Reps · {reps.period}</h2>
          <table>
            <thead><tr><th>Rep</th><th>Quota</th><th>Won</th><th>Attainment</th><th>Pipeline closing this quarter</th><th>Coverage</th><th>Win rate</th></tr></thead>
            <tbody>{reps.reps.map((r: any) => (
              <tr key={r.owner}><td>{r.owner}</td><td>{money(r.quota)}</td><td>{money(r.won)}</td><td>{pct(r.attainment_pct)}</td>
                <td>{money(r.pipeline)} ({r.open_deals})</td><td>{r.coverage ? `${r.coverage}x` : "–"}</td><td>{pct(r.win_rate_pct)}</td></tr>
            ))}</tbody>
          </table>
        </>
      )}

      {winLoss && (winLoss.won + winLoss.lost > 0) && (
        <>
          <h2>Win / loss</h2>
          <div className="grid">
            <Stat label="Win rate" value={pct(winLoss.win_rate_pct)} />
            <Stat label="Won" value={winLoss.won} /><Stat label="Lost" value={winLoss.lost} />
          </div>
          <p className="muted">Why we win: {Object.entries(winLoss.won_reasons).map(([k, n]) => `${k.replaceAll("_", " ")} (${n})`).join(", ") || "–"}
            <br />Why we lose: {Object.entries(winLoss.lost_reasons).map(([k, n]) => `${k.replaceAll("_", " ")} (${n})`).join(", ") || "–"}</p>
          {Object.keys(winLoss.by_competitor).length > 0 && (
            <table><thead><tr><th>Against</th><th>Won</th><th>Lost</th><th>Win rate</th><th>Why we lose</th></tr></thead>
              <tbody>{Object.entries(winLoss.by_competitor).map(([c, r]: [string, any]) => (
                <tr key={c}><td>{c}</td><td>{r.won}</td><td>{r.lost}</td><td>{pct(r.win_rate_pct)}</td>
                  <td>{Object.entries(r.why_we_lose).map(([k, n]) => `${k.replaceAll("_", " ")} (${n})`).join(", ") || "–"}</td></tr>
              ))}</tbody></table>
          )}
        </>
      )}

      {mta && mta.deals_with_touches > 0 && (
        <>
          <h2>Multi-touch attribution</h2>
          <div className="row" style={{ marginBottom: 8 }}>
            <select style={{ maxWidth: 220 }} value={mtaModel} onChange={(e) => setMtaModel(e.target.value)}>
              <option value="w_shaped">W-shaped (40/20/40)</option><option value="linear">Linear</option>
              <option value="first_touch">First touch</option><option value="last_touch">Last touch</option>
            </select>
            <span className="muted">{mta.deals_with_touches} of {mta.deals} deals had touches before they were created. {mta.note}</span>
          </div>
          <table><thead><tr><th>Source</th><th>Credited pipeline</th></tr></thead>
            <tbody>{Object.entries(mta.models[mtaModel]).map(([k, v]: [string, any]) => <tr key={k}><td>{k}</td><td>{money(v)}</td></tr>)}</tbody></table>
        </>
      )}

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
            <thead><tr><th>Campaign</th><th>Members</th><th>Sent</th><th>Reply rate</th><th>Meetings</th><th>Deals</th><th>Pipeline</th><th>Won</th><th>Spend</th><th>Cost / meeting</th><th>ROI</th></tr></thead>
            <tbody>
              {attr.by_campaign.map((r: any) => (
                <tr key={r.campaign_id}><td>{r.name}</td><td>{r.members}</td><td>{r.sent}</td><td>{pct(r.reply_rate)}</td>
                  <td>{r.meetings}</td><td>{r.opportunities}</td><td>{money(r.pipeline)}</td><td>{money(r.won)}</td>
                  <td>{money(r.spend)}</td><td>{r.cost_per_meeting != null ? money(r.cost_per_meeting) : "–"}</td>
                  <td>{r.roi != null ? `${r.roi}x` : "–"}</td></tr>
              ))}
              {!attr.by_campaign.length && <tr><td colSpan={11} className="muted">No campaigns yet.</td></tr>}
            </tbody>
          </table>
          <RateTable title="Outreach by agent" rows={attr.by_agent} keyName="Agent" />
          <RateTable title="Outreach by sequence step" rows={attr.by_sequence_step} keyName="Step" />
          <p className="muted">Attribution model: {attr.model}.</p>
        </>
      )}

      <h2>Learned playbook</h2>
      {versions.filter((v) => v.status === "draft").map((v) => (
        <div className="card md" key={v.id} style={{ marginBottom: 12 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div><span className="pill tier-C">proposal</span> {v.title} · confidence {v.confidence}
              {v.edits_used != null && <span className="muted"> · learned from {v.edits_used} edits and {v.rejections_used} rejections</span>}</div>
            <div className="row">
              <button className="primary" onClick={() => playbookAction(`/api/insights/playbook/${v.id}/approve`)}>Approve and publish</button>
              <button className="danger" onClick={() => playbookAction(`/api/insights/playbook/${v.id}/reject`)}>Reject</button>
            </div>
          </div>
          <ReactMarkdown>{v.content}</ReactMarkdown>
        </div>
      ))}
      {playbook?.content
        ? <div className="card md"><div className="row" style={{ justifyContent: "space-between" }}>
            <div className="muted">Live since {new Date(playbook.updated_at).toLocaleString()} · used by every writing agent</div>
            <button onClick={() => playbookAction("/api/insights/playbook/rollback")}>Roll back to previous</button></div>
            <ReactMarkdown>{playbook.content}</ReactMarkdown></div>
        : <p className="muted">No live playbook yet. It needs enough sent emails and replies (LEARN_MIN_SENT, LEARN_MIN_REPLIES), then <b>Update playbook</b> proposes one for an admin to approve.</p>}
    </>
  );
}
