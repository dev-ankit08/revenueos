"use client";
import { useEffect, useState } from "react";
import { api, post } from "@/lib/api";

export default function Workspaces() {
  const [list, setList] = useState<any[]>([]);
  const [form, setForm] = useState({ name: "", slug: "", admin_email: "", admin_name: "", admin_password: "" });
  const [msg, setMsg] = useState("");

  const load = () => api("/api/workspaces").then(setList).catch((e) => setMsg(e.message));
  useEffect(() => { load(); }, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    try {
      const r = await post("/api/workspaces", form);
      setMsg(`Created ${r.name}. Its admin can now sign in and work through the setup checklist.`);
      setForm({ name: "", slug: "", admin_email: "", admin_name: "", admin_password: "" });
      load();
    } catch (err: any) { setMsg(err.message); }
  }

  return (
    <>
      <h1>Client workspaces</h1>
      <p className="sub">Each client gets its own database, settings, CRM connection, knowledge base and people. Nothing is shared between them.</p>
      {msg && <p className="muted">{msg}</p>}
      <table>
        <thead><tr><th>Workspace</th><th>Slug (used in webhook URLs)</th><th>People</th><th>Created</th></tr></thead>
        <tbody>
          {list.map((w) => (
            <tr key={w.id}><td>{w.name}{w.is_default && <span className="pill" style={{ marginLeft: 6 }}>default</span>}</td>
              <td><code>{w.slug}</code></td><td>{w.users}</td><td>{new Date(w.created_at).toLocaleDateString()}</td></tr>
          ))}
        </tbody>
      </table>
      <h2>New client</h2>
      <form className="card grid" onSubmit={create}>
        <input required placeholder="Client name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <input required pattern="[a-z0-9][a-z0-9-]{1,38}" title="lowercase letters, digits, dashes" placeholder="slug, e.g. acme"
          value={form.slug} onChange={(e) => setForm({ ...form, slug: e.target.value })} />
        <input required type="email" placeholder="Client admin email" value={form.admin_email} onChange={(e) => setForm({ ...form, admin_email: e.target.value })} />
        <input placeholder="Client admin name" value={form.admin_name} onChange={(e) => setForm({ ...form, admin_name: e.target.value })} />
        <input required type="password" minLength={10} autoComplete="new-password" placeholder="Temporary password (10+)"
          value={form.admin_password} onChange={(e) => setForm({ ...form, admin_password: e.target.value })} />
        <button className="primary">Create workspace</button>
      </form>
    </>
  );
}
