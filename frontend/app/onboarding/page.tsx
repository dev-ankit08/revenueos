"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, post, waitForRun } from "@/lib/api";

const CRMS = ["local", "salesforce", "hubspot", "dynamics", "zoho", "pipedrive"];
const CRM_FIELDS: Record<string, [string, string][]> = {
  salesforce: [["sf_username", "Username"], ["sf_password", "Password"], ["sf_security_token", "Security token"], ["sf_domain", "Domain (login or test)"]],
  hubspot: [["hubspot_token", "Private app token"]],
  dynamics: [["dynamics_url", "Environment URL"], ["dynamics_client_id", "Client id"], ["dynamics_client_secret", "Client secret"]],
  zoho: [["zoho_client_id", "Client id"], ["zoho_client_secret", "Client secret"], ["zoho_refresh_token", "Refresh token"]],
  pipedrive: [["pipedrive_token", "API token"], ["pipedrive_domain", "Domain (yourcompany.pipedrive.com)"]],
  local: [],
};
const SECRETS = new Set(["sf_password", "sf_security_token", "hubspot_token", "dynamics_client_secret", "zoho_client_secret", "zoho_refresh_token", "pipedrive_token"]);

export default function Onboarding() {
  const [status, setStatus] = useState<any>(null);
  const [company, setCompany] = useState({ company_name: "", physical_address: "", sender_email: "", brand_color: "" });
  const [crm, setCrm] = useState<Record<string, string>>({ crm_provider: "local" });
  const [kb, setKb] = useState({ offering: "", icp: "", case_study: "", brand_voice: "", remove_samples: true });
  const [msg, setMsg] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState("");

  const load = () => api("/api/onboarding/status").then(setStatus).catch((e) => setMsg({ top: e.message }));
  useEffect(() => { load(); }, []);
  const say = (key: string, text: string) => setMsg((m) => ({ ...m, [key]: text }));

  async function saveSettings(key: string, values: Record<string, any>) {
    const clean = Object.fromEntries(Object.entries(values).filter(([, v]) => v !== ""));
    try {
      await api("/api/workspace/settings", { method: "PUT", body: JSON.stringify(clean) });
      say(key, "Saved.");
      load();
    } catch (e: any) { say(key, e.message); }
  }

  async function action(key: string, label: string, fn: () => Promise<string>) {
    setBusy(key);
    try { say(key, await fn()); } catch (e: any) { say(key, e.message); }
    setBusy("");
    load();
  }

  if (!status) return <p className="muted">{msg.top || "Loading…"}</p>;
  const done = (k: string) => status.steps.find((s: any) => s.key === k)?.done;
  const step = (k: string) => status.steps.find((s: any) => s.key === k);
  const Head = ({ k, n }: { k: string; n: number }) => (
    <h2>{done(k) ? "✓" : `${n}.`} {step(k).title} <span className="muted" style={{ fontWeight: 400, fontSize: 13 }}>· {step(k).detail}</span></h2>
  );

  return (
    <>
      <h1>Setup checklist</h1>
      <p className="sub">{status.complete ? "Ready. Every required step is done." : "Six steps to a working pilot. Each one can be revisited any time."}</p>

      <Head k="company" n={1} />
      <form className="card grid" onSubmit={(e) => { e.preventDefault(); saveSettings("company", company); }}>
        <input placeholder="Company name" value={company.company_name} onChange={(e) => setCompany({ ...company, company_name: e.target.value })} />
        <input placeholder="Registered address (required in email footers)" value={company.physical_address} onChange={(e) => setCompany({ ...company, physical_address: e.target.value })} />
        <input type="email" placeholder="Send emails from" value={company.sender_email} onChange={(e) => setCompany({ ...company, sender_email: e.target.value })} />
        <input placeholder="Brand colour, e.g. 3B5BDB" value={company.brand_color} onChange={(e) => setCompany({ ...company, brand_color: e.target.value })} />
        <button className="primary">Save</button>
        {msg.company && <span className="muted">{msg.company}</span>}
      </form>

      <Head k="crm" n={2} />
      <form className="card stack" onSubmit={(e) => { e.preventDefault(); saveSettings("crm", crm); }}>
        <select style={{ maxWidth: 220 }} value={crm.crm_provider} onChange={(e) => setCrm({ crm_provider: e.target.value })}>
          {CRMS.map((c) => <option key={c} value={c}>{c === "local" ? "No CRM (use RevenueOS)" : c}</option>)}
        </select>
        <div className="grid">
          {CRM_FIELDS[crm.crm_provider].map(([k, label]) => (
            <input key={k} type={SECRETS.has(k) ? "password" : "text"} autoComplete="off" placeholder={label}
              value={crm[k] ?? ""} onChange={(e) => setCrm({ ...crm, [k]: e.target.value })} />
          ))}
        </div>
        <div className="row">
          <button className="primary">Save CRM</button>
          <button type="button" disabled={busy === "crm"} onClick={() => action("crm", "Test", async () => {
            const r = await post("/api/onboarding/test-crm");
            return `✓ ${r.detail}`;
          })}>Test connection</button>
          {msg.crm && <span className="muted">{msg.crm}</span>}
        </div>
      </form>

      <Head k="knowledge" n={3} />
      <form className="card stack" onSubmit={(e) => { e.preventDefault(); action("kb", "Save", async () => {
        const r = await post("/api/onboarding/knowledge", kb);
        return `Added ${r.added.length} documents, removed ${r.samples_removed} samples.`;
      }); }}>
        <textarea rows={3} placeholder="What you sell and the outcomes it brings" value={kb.offering} onChange={(e) => setKb({ ...kb, offering: e.target.value })} />
        <textarea rows={3} placeholder="Ideal customer: industries, company size, regions, buying signals" value={kb.icp} onChange={(e) => setKb({ ...kb, icp: e.target.value })} />
        <textarea rows={3} placeholder="One real, approved customer result (with numbers)" value={kb.case_study} onChange={(e) => setKb({ ...kb, case_study: e.target.value })} />
        <textarea rows={2} placeholder="How your company writes" value={kb.brand_voice} onChange={(e) => setKb({ ...kb, brand_voice: e.target.value })} />
        <label className="row muted" style={{ gap: 6 }}>
          <input type="checkbox" style={{ width: "auto" }} checked={kb.remove_samples} onChange={(e) => setKb({ ...kb, remove_samples: e.target.checked })} />
          Remove the sample documents
        </label>
        <div className="row"><button className="primary" disabled={busy === "kb"}>Save knowledge</button>
          <Link href="/knowledge">Full knowledge base →</Link>{msg.kb && <span className="muted">{msg.kb}</span>}</div>
      </form>

      <Head k="leads" n={4} />
      <div className="card row">
        <button disabled={busy === "leads"} onClick={() => action("leads", "Import", async () => {
          const r = await post("/api/crm/import");
          return `Imported ${r.imported} open leads from ${r.crm}.`;
        })}>Import open leads from the CRM</button>
        <Link href="/intel">Import a CSV (Apollo, ZoomInfo, Sales Navigator) →</Link>
        <Link href="/pipeline">Add leads by hand →</Link>
        {msg.leads && <span className="muted">{msg.leads}</span>}
      </div>

      <Head k="integrations" n={5} />
      <div className="card row"><Link href="/integrations">Connect Gmail/Outlook, calendar, Slack, Zoom, Gong →</Link></div>

      <Head k="pilot" n={6} />
      <div className="card stack">
        <div className="muted">Runs outbound on up to 50 new leads. A random share is held back with no AI outreach, so the Insights page can show the difference with your own numbers.</div>
        <div className="row">
          <button className="primary" disabled={busy === "pilot"} onClick={() => action("pilot", "Pilot", async () => {
            const r = await post("/api/onboarding/pilot", { max_leads: 50, steps: 3 });
            const run = await waitForRun(r.run_id);
            return run.status === "failed" ? `Failed: ${run.error}` : run.output ?? "Started.";
          })}>{busy === "pilot" ? "Running…" : "Start the pilot"}</button>
          <Link href="/evaluate">Set up a blind test →</Link>
          {msg.pilot && <span className="muted">{msg.pilot}</span>}
        </div>
      </div>
    </>
  );
}
