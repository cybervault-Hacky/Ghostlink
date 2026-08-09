import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { request } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Card, EmptyState } from "../components/ui";
import type { DashboardData } from "../lib/types";

export default function Dashboard() {
  const { user } = useAuth();
  const [data, setData] = useState<DashboardData | null>(null);

  useEffect(() => {
    request("GET", "/api/v1/dashboard").then((res) => {
      if (res.status === 200) setData(res.body);
    });
  }, []);

  const cards = [
    { label: "Developer keys", value: data?.counts.credentials ?? 0, to: "/credentials" },
    { label: "Projects", value: data?.counts.projects ?? 0, to: "/projects" },
    { label: "Active sessions", value: data?.counts.sessions ?? 0, to: "/sessions" },
    { label: "MFA", value: data?.counts.mfaEnabled ? "On" : "Off", to: "/security" },
  ];

  return (
    <main className="container" style={{ paddingTop: 64, paddingBottom: 80 }}>
      <h1 className="section-title" style={{ fontSize: 28 }}>Welcome back</h1>
      <p className="muted mono" style={{ marginTop: 4 }}>
        {user?.developerId}
      </p>
      <div className="grid-3 mt-24">
        {cards.map((c) => (
          <Link key={c.label} to={c.to} style={{ textDecoration: "none", color: "inherit" }}>
            <Card style={{ height: 120, display: "flex", flexDirection: "column", justifyContent: "space-between" }}>
              <span className="muted" style={{ fontSize: 13 }}>{c.label}</span>
              <span style={{ fontSize: 32, fontWeight: 650 }}>{c.value}</span>
            </Card>
          </Link>
        ))}
      </div>
      {data && data.counts.projects === 0 && (
        <div className="card mt-24">
          <EmptyState
            title="No projects yet."
            cta={<Link to="/projects" className="btn btn-primary">Create project</Link>}
          />
        </div>
      )}
    </main>
  );
}
