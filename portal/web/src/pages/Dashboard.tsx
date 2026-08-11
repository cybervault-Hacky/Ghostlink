import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { request } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Button, Card, GlassCard, StatusBadge } from "../components/ui";
import { ConnectDeviceModal } from "../components/ConnectDeviceModal";
import type { DashboardData, Project } from "../lib/types";

export default function Dashboard() {
  const { user } = useAuth();
  const [data, setData] = useState<DashboardData | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [devices, setDevices] = useState<any[]>([]);
  const [activity, setActivity] = useState<any[]>([]);
  const [showPairModal, setShowPairModal] = useState(false);

  async function reload() {
    const [dRes, pRes, devRes, actRes] = await Promise.all([
      request("GET", "/api/v1/dashboard"),
      request("GET", "/api/v1/projects"),
      request("GET", "/api/v1/devapi/devices"),
      request("GET", "/api/v1/devapi/activity"),
    ]);
    if (dRes.status === 200) setData(dRes.body);
    if (pRes.status === 200) setProjects(pRes.body.projects || []);
    if (devRes.status === 200) setDevices(devRes.body.devices || []);
    if (actRes.status === 200) setActivity(actRes.body.events || []);
  }

  useEffect(() => {
    reload();
  }, []);

  const activeProject = projects.length > 0 ? projects[0] : null;
  const activeDevice = devices.length > 0 ? devices[0] : null;

  return (
    <main className="container" style={{ paddingTop: 56, paddingBottom: 80 }}>
      {/* Header */}
      <div className="row between" style={{ alignItems: "flex-start", flexWrap: "wrap", gap: 16 }}>
        <div>
          <h1 className="section-title" style={{ fontSize: 28, margin: 0 }}>
            GhostLink Developer
          </h1>
          <p className="muted mono" style={{ marginTop: 4, fontSize: 14 }}>
            {user?.developerId}
          </p>
        </div>
        <div className="row gap-8">
          <Link to="/quickstart" className="btn btn-primary">
            Quick Start
          </Link>
          <Button variant="ghost" onClick={() => setShowPairModal(true)}>
            Connect Device
          </Button>
        </div>
      </div>

      {/* Command Center Grid */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
          gap: 16,
          marginTop: 24,
        }}
      >
        {/* Project Card */}
        <Link to="/projects" style={{ textDecoration: "none", color: "inherit" }}>
          <GlassCard style={{ padding: 20, height: "100%", display: "flex", flexDirection: "column", justifyContent: "space-between" }}>
            <div>
              <span className="muted" style={{ fontSize: 12, textTransform: "uppercase", letterSpacing: "0.05em" }}>
                Project
              </span>
              <div style={{ fontSize: 18, fontWeight: 650, marginTop: 8 }}>
                {activeProject ? activeProject.name : "No active project"}
              </div>
              <p className="muted mono" style={{ fontSize: 12, marginTop: 4 }}>
                {activeProject ? activeProject.slug : "Create a project to start"}
              </p>
            </div>
            <div style={{ marginTop: 12, fontSize: 12, color: "var(--accent, #6366f1)" }}>
              {projects.length} project(s) →
            </div>
          </GlassCard>
        </Link>

        {/* Device Card */}
        <Link to="/devices" style={{ textDecoration: "none", color: "inherit" }}>
          <GlassCard style={{ padding: 20, height: "100%", display: "flex", flexDirection: "column", justifyContent: "space-between" }}>
            <div>
              <div className="row between">
                <span className="muted" style={{ fontSize: 12, textTransform: "uppercase", letterSpacing: "0.05em" }}>
                  Device
                </span>
                {activeDevice && <StatusBadge status={activeDevice.status} />}
              </div>
              <div style={{ fontSize: 18, fontWeight: 650, marginTop: 8 }}>
                {activeDevice ? activeDevice.name : "No device connected"}
              </div>
              <p className="muted mono" style={{ fontSize: 12, marginTop: 4 }}>
                {activeDevice ? activeDevice.device_id : "Pair with Termux"}
              </p>
            </div>
            <div style={{ marginTop: 12, fontSize: 12, color: "var(--accent, #6366f1)" }}>
              {devices.length} device(s) registered →
            </div>
          </GlassCard>
        </Link>

        {/* API Card */}
        <Link to="/developer-api" style={{ textDecoration: "none", color: "inherit" }}>
          <GlassCard style={{ padding: 20, height: "100%", display: "flex", flexDirection: "column", justifyContent: "space-between" }}>
            <div>
              <span className="muted" style={{ fontSize: 12, textTransform: "uppercase", letterSpacing: "0.05em" }}>
                Developer API
              </span>
              <div style={{ fontSize: 18, fontWeight: 650, marginTop: 8 }}>
                Ready
              </div>
              <p className="muted mono" style={{ fontSize: 12, marginTop: 4 }}>
                /api/v1/developer/*
              </p>
            </div>
            <div style={{ marginTop: 12, fontSize: 12, color: "var(--accent, #6366f1)" }}>
              View API endpoints →
            </div>
          </GlassCard>
        </Link>

        {/* Security Card */}
        <Link to="/security" style={{ textDecoration: "none", color: "inherit" }}>
          <GlassCard style={{ padding: 20, height: "100%", display: "flex", flexDirection: "column", justifyContent: "space-between" }}>
            <div>
              <span className="muted" style={{ fontSize: 12, textTransform: "uppercase", letterSpacing: "0.05em" }}>
                Security
              </span>
              <div style={{ fontSize: 18, fontWeight: 650, marginTop: 8 }}>
                Protected
              </div>
              <p className="muted mono" style={{ fontSize: 12, marginTop: 4 }}>
                MFA: {data?.counts.mfaEnabled ? "Enabled" : "Off"}
              </p>
            </div>
            <div style={{ marginTop: 12, fontSize: 12, color: "var(--accent, #6366f1)" }}>
              Manage security →
            </div>
          </GlassCard>
        </Link>
      </div>

      {/* Quick Actions Bar */}
      <div style={{ marginTop: 28 }}>
        <h2 style={{ fontSize: 16, fontWeight: 650, marginBottom: 12 }}>
          Quick Actions
        </h2>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <Button variant="primary" onClick={() => setShowPairModal(true)}>
            Connect Device
          </Button>
          <Link to="/quickstart" className="btn">
            Quick Start
          </Link>
          <Link to="/projects" className="btn">
            Projects ({data?.counts.projects ?? 0})
          </Link>
          <Link to="/devices" className="btn">
            Devices ({devices.length})
          </Link>
          <Link to="/credentials" className="btn">
            Credentials ({data?.counts.credentials ?? 0})
          </Link>
          <Link to="/security" className="btn">
            Security Status
          </Link>
        </div>
      </div>

      {/* Recent API Activity */}
      <div style={{ marginTop: 32 }}>
        <div className="row between" style={{ marginBottom: 12 }}>
          <h2 style={{ fontSize: 16, fontWeight: 650, margin: 0 }}>
            Recent Activity
          </h2>
          <Link to="/api-activity" className="muted" style={{ fontSize: 13 }}>
            View all ({activity.length}) →
          </Link>
        </div>

        <Card style={{ padding: 0, overflow: "hidden" }}>
          {activity.length === 0 ? (
            <div style={{ padding: 24, textAlign: "center" }} className="muted">
              No recent activity recorded yet.
            </div>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ borderBottom: "1px solid var(--border)", background: "rgba(0,0,0,0.02)" }}>
                  <th style={{ textAlign: "left", padding: "10px 16px" }} className="muted">Endpoint</th>
                  <th style={{ textAlign: "left", padding: "10px 16px" }} className="muted">Category</th>
                  <th style={{ textAlign: "left", padding: "10px 16px" }} className="muted">Result</th>
                  <th style={{ textAlign: "right", padding: "10px 16px" }} className="muted">Time</th>
                </tr>
              </thead>
              <tbody>
                {activity.slice(0, 5).map((ev, idx) => (
                  <tr key={idx} style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "10px 16px" }} className="mono">{ev.endpoint}</td>
                    <td style={{ padding: "10px 16px" }}>{ev.category}</td>
                    <td style={{ padding: "10px 16px" }}>
                      <span style={{ color: ev.result === "ok" || ev.result === "completed" || ev.result === "approved" ? "var(--success, #34D399)" : "var(--muted)" }}>
                        {ev.result}
                      </span>
                    </td>
                    <td style={{ textAlign: "right", padding: "10px 16px" }} className="muted mono">
                      {new Date(ev.created_at).toLocaleTimeString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>

      <ConnectDeviceModal
        isOpen={showPairModal}
        onClose={() => setShowPairModal(false)}
        onConnected={() => reload()}
      />
    </main>
  );
}
