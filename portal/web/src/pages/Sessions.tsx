import { useEffect, useState } from "react";
import { request } from "../lib/api";
import { Badge, Button, Card } from "../components/ui";
import type { Session } from "../lib/types";

export default function Sessions() {
  const [sessions, setSessions] = useState<Session[]>([]);

  async function load() {
    const res = await request("GET", "/api/v1/sessions");
    if (res.status === 200) setSessions(res.body.sessions);
  }
  useEffect(() => {
    load();
  }, []);

  async function revoke(id: number) {
    await request("POST", `/api/v1/sessions/${id}/revoke`);
    load();
  }
  async function revokeOthers() {
    await request("POST", "/api/v1/sessions/revoke-others");
    load();
  }

  return (
    <main className="container" style={{ paddingTop: 56, paddingBottom: 80 }}>
      <div className="row between">
        <div>
          <h1 className="section-title" style={{ fontSize: 26, margin: 0 }}>Sessions & devices</h1>
          <p className="muted" style={{ marginTop: 4, fontSize: 14 }}>Review and revoke active sessions.</p>
        </div>
        <Button variant="danger" onClick={revokeOthers}>Sign out all other sessions</Button>
      </div>

      <div className="stack mt-24">
        {sessions.map((s) => (
          <Card key={s.id}>
            <div className="row between">
              <div>
                <div className="row gap-8">
                  <span style={{ fontWeight: 600 }}>{s.device || "Unknown device"}</span>
                  {s.current && <Badge tone="active">Current</Badge>}
                  {s.revokedAt && <Badge tone="revoked">Revoked</Badge>}
                </div>
                <p className="muted mono" style={{ marginTop: 8, fontSize: 12 }}>
                  {s.browser || "—"} · Last active {s.lastActiveAt ? new Date(s.lastActiveAt).toLocaleString() : "—"}
                </p>
              </div>
              {!s.revokedAt && (
                <Button variant="danger" onClick={() => revoke(s.id)}>Sign out</Button>
              )}
            </div>
          </Card>
        ))}
      </div>
    </main>
  );
}
