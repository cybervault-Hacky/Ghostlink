import { useEffect, useState } from "react";
import { request } from "../lib/api";
import { Button, Card, EmptyState, Field, GlassCard, StatusBadge } from "../components/ui";
import type { Project } from "../lib/types";

export default function Projects() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [showNew, setShowNew] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState("");

  async function load() {
    const res = await request("GET", "/api/v1/projects");
    if (res.status === 200) setProjects(res.body.projects);
  }
  useEffect(() => {
    load();
  }, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const res = await request("POST", "/api/v1/projects", { name });
    if (res.status !== 201) {
      setError(res.body?.error?.message || "Could not create project.");
      return;
    }
    setShowNew(false);
    setName("");
    load();
  }

  async function archive(id: number) {
    await request("POST", `/api/v1/projects/${id}`, { action: "archive" });
    load();
  }

  return (
    <main className="container" style={{ paddingTop: 56, paddingBottom: 80 }}>
      <div className="row between">
        <div>
          <h1 className="section-title" style={{ fontSize: 26, margin: 0 }}>Projects</h1>
          <p className="muted" style={{ marginTop: 4, fontSize: 14 }}>Organize your developer work.</p>
        </div>
        <Button variant="primary" onClick={() => setShowNew(true)}>Create project</Button>
      </div>

      {showNew && (
        <GlassCard strong style={{ padding: 28, marginTop: 24 }}>
          <form onSubmit={create}>
            <Field label="Project name">
              <input className="input" value={name} onChange={(e) => setName(e.target.value)} required />
            </Field>
            {error && <p className="error-text" role="alert">{error}</p>}
            <Button variant="primary">Create</Button>
          </form>
        </GlassCard>
      )}

      <div className="stack mt-24">
        {projects.length === 0 && !showNew && (
          <Card><EmptyState title="No projects yet." cta={<Button onClick={() => setShowNew(true)}>Create project</Button>} /></Card>
        )}
        {projects.map((p) => (
          <Card key={p.id}>
            <div className="row between">
              <div>
                <div className="row gap-8">
                  <span style={{ fontWeight: 600 }}>{p.name}</span>
                  <span className="mono muted">{p.slug}</span>
                  <StatusBadge status={p.status} />
                </div>
                <p className="muted mono" style={{ marginTop: 8, fontSize: 12 }}>Created {new Date(p.createdAt).toLocaleDateString()}</p>
              </div>
              {p.status === "active" && (
                <Button variant="ghost" onClick={() => archive(p.id)}>Archive</Button>
              )}
            </div>
          </Card>
        ))}
      </div>
    </main>
  );
}
