import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { request } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Button, Card, GlassCard, Field } from "../components/ui";
import { ConnectDeviceModal } from "../components/ConnectDeviceModal";
import type { DashboardData, Project } from "../lib/types";

export default function QuickStart() {
  const { user } = useAuth();
  const [data, setData] = useState<DashboardData | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [devices, setDevices] = useState<any[]>([]);
  const [showPairModal, setShowPairModal] = useState(false);
  const [newProjectName, setNewProjectName] = useState("My GhostLink Project");
  const [creatingProject, setCreatingProject] = useState(false);
  const [copiedCmd, setCopiedCmd] = useState<string | null>(null);

  async function reload() {
    const [dRes, pRes, devRes] = await Promise.all([
      request("GET", "/api/v1/dashboard"),
      request("GET", "/api/v1/projects"),
      request("GET", "/api/v1/devapi/devices"),
    ]);
    if (dRes.status === 200) setData(dRes.body);
    if (pRes.status === 200) setProjects(pRes.body.projects || []);
    if (devRes.status === 200) setDevices(devRes.body.devices || []);
  }

  useEffect(() => {
    reload();
  }, []);

  async function handleCreateFirstProject(e: React.FormEvent) {
    e.preventDefault();
    setCreatingProject(true);
    try {
      await request("POST", "/api/v1/projects", { name: newProjectName });
      await reload();
    } finally {
      setCreatingProject(false);
    }
  }

  function copyCmd(cmd: string) {
    navigator.clipboard?.writeText(cmd);
    setCopiedCmd(cmd);
    setTimeout(() => setCopiedCmd(null), 2000);
  }

  const hasProject = projects.length > 0;
  const hasDevice = devices.length > 0;
  const allReady = Boolean(user && hasProject && hasDevice);

  const steps = [
    {
      id: "account",
      title: "Account",
      done: Boolean(user),
      detail: user ? `Developer: ${user.developerId}` : "Sign in required",
    },
    {
      id: "project",
      title: "Project",
      done: hasProject,
      detail: hasProject ? `${projects[0].name} (${projects[0].slug})` : "No project created",
    },
    {
      id: "device",
      title: "Device",
      done: hasDevice,
      detail: hasDevice ? `${devices[0].name} (${devices[0].device_id})` : "Not connected",
    },
    {
      id: "auth",
      title: "Authentication",
      done: hasDevice,
      detail: hasDevice ? "Scoped tokens active (0600)" : "Pending device pairing",
    },
    {
      id: "api",
      title: "API Access",
      done: Boolean(data),
      detail: "Ready (localhost / live)",
    },
  ];

  return (
    <main className="container" style={{ paddingTop: 56, paddingBottom: 80 }}>
      <div style={{ maxWidth: 840, margin: "0 auto" }}>
        <h1 className="section-title" style={{ fontSize: 28, margin: 0 }}>
          Developer Quick Start
        </h1>
        <p className="muted" style={{ marginTop: 6, fontSize: 15 }}>
          Go from installation to running developer commands in minutes.
        </p>

        {/* Progress Card */}
        <div style={{ marginTop: 28 }}>
          <GlassCard strong style={{ padding: 28 }}>
            <h2 style={{ fontSize: 18, fontWeight: 650, margin: "0 0 20px" }}>
              Setup Progress
            </h2>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))",
                gap: 12,
              }}
            >
              {steps.map((s) => (
                <div
                  key={s.id}
                  style={{
                    background: s.done ? "rgba(52, 211, 153, 0.08)" : "var(--card-bg, #fff)",
                    border: `1px solid ${s.done ? "rgba(52, 211, 153, 0.4)" : "var(--border)"}`,
                    borderRadius: 8,
                    padding: "12px 14px",
                  }}
                >
                  <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 14, fontWeight: 600 }}>
                    <span style={{ color: s.done ? "var(--success, #34D399)" : "var(--muted)" }}>
                      {s.done ? "✓" : "○"}
                    </span>
                    <span>{s.title}</span>
                  </div>
                  <p className="muted mono" style={{ fontSize: 11, marginTop: 6, lineHeight: 1.3 }}>
                    {s.detail}
                  </p>
                </div>
              ))}
            </div>
          </GlassCard>
        </div>

        {/* Step 1: Create Project if none */}
        {!hasProject && (
          <div style={{ marginTop: 24 }}>
            <Card style={{ padding: 24 }}>
              <h3 style={{ fontSize: 16, fontWeight: 650, margin: "0 0 8px" }}>
                1. Create your first project
              </h3>
              <p className="muted" style={{ fontSize: 14, marginBottom: 16 }}>
                Projects organize your API credentials, permissions, and device bindings.
              </p>
              <form onSubmit={handleCreateFirstProject} style={{ maxWidth: 420 }}>
                <Field label="Project name">
                  <input
                    className="input"
                    value={newProjectName}
                    onChange={(e) => setNewProjectName(e.target.value)}
                    required
                  />
                </Field>
                <Button variant="primary" disabled={creatingProject} style={{ marginTop: 12 }}>
                  {creatingProject ? "Creating…" : "Create & Continue"}
                </Button>
              </form>
            </Card>
          </div>
        )}

        {/* Step 2: Connect Device if none */}
        {hasProject && !hasDevice && (
          <div style={{ marginTop: 24 }}>
            <Card style={{ padding: 24 }}>
              <div className="row between" style={{ flexWrap: "wrap", gap: 16 }}>
                <div>
                  <h3 style={{ fontSize: 16, fontWeight: 650, margin: "0 0 4px" }}>
                    2. Connect your Termux device
                  </h3>
                  <p className="muted" style={{ fontSize: 14, margin: 0 }}>
                    Pair your installation using a short-lived, single-use pairing code.
                  </p>
                </div>
                <Button variant="primary" onClick={() => setShowPairModal(true)}>
                  Connect Device
                </Button>
              </div>
            </Card>
          </div>
        )}

        {/* Step 3: Verified CLI Commands */}
        <div style={{ marginTop: 24 }}>
          <Card style={{ padding: 24 }}>
            <h3 style={{ fontSize: 16, fontWeight: 650, margin: "0 0 6px" }}>
              {allReady ? "Your environment is ready" : "Developer CLI Commands"}
            </h3>
            <p className="muted" style={{ fontSize: 14, marginBottom: 20 }}>
              Use these commands from your Termux terminal to manage projects and devices:
            </p>

            <div className="stack" style={{ gap: 14 }}>
              {[
                {
                  cmd: "ghostlink developer whoami",
                  desc: "Display authenticated developer identity and scopes",
                },
                {
                  cmd: "ghostlink developer device list",
                  desc: "List active Termux installations bound to your account",
                },
                {
                  cmd: "ghostlink developer project list",
                  desc: "List projects available for developer tooling",
                },
                {
                  cmd: "ghostlink developer doctor",
                  desc: "Run comprehensive environment and connectivity diagnostics",
                },
              ].map((c) => (
                <div
                  key={c.cmd}
                  style={{
                    background: "rgba(0,0,0,0.03)",
                    border: "1px solid var(--border)",
                    borderRadius: 6,
                    padding: "10px 14px",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 12,
                    flexWrap: "wrap",
                  }}
                >
                  <div>
                    <code className="mono" style={{ fontSize: 14, fontWeight: 600 }}>
                      {c.cmd}
                    </code>
                    <p className="muted" style={{ fontSize: 12, marginTop: 3 }}>
                      {c.desc}
                    </p>
                  </div>
                  <Button
                    variant="ghost"
                    onClick={() => copyCmd(c.cmd)}
                    style={{ fontSize: 12, padding: "4px 8px" }}
                  >
                    {copiedCmd === c.cmd ? "Copied" : "Copy"}
                  </Button>
                </div>
              ))}
            </div>

            <div style={{ marginTop: 24, display: "flex", gap: 12, flexWrap: "wrap" }}>
              <Link to="/dashboard" className="btn btn-primary">
                Developer Dashboard
              </Link>
              <Button variant="ghost" onClick={() => setShowPairModal(true)}>
                Connect Another Device
              </Button>
            </div>
          </Card>
        </div>
      </div>

      <ConnectDeviceModal
        isOpen={showPairModal}
        onClose={() => setShowPairModal(false)}
        onConnected={() => reload()}
      />
    </main>
  );
}
