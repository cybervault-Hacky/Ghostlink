import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { request } from "../lib/api";
import { Badge, Button, Card, EmptyState, GlassCard, StatusBadge } from "../components/ui";

interface ApiCredential {
  credential_id: string;
  name: string;
  scopes: string[];
  status: string;
  created_at: string;
  last_used_at: string | null;
  rotated_at: string | null;
  revoked_at: string | null;
}

const SCOPE_LABELS: Record<string, string> = {
  "project:read": "Read projects",
  "project:write": "Write projects",
  "device:read": "Read devices",
  "device:write": "Manage devices",
  "credential:read": "Read credentials",
  "credential:rotate": "Rotate credentials",
  "security:read": "Read security",
};

export default function DeveloperApi() {
  const [creds, setCreds] = useState<ApiCredential[]>([]);
  const [pairing, setPairing] = useState<Array<{ nonce: string; status: string; expires_at: string }>>([]);

  async function load() {
    const [c, p] = await Promise.all([
      request("GET", "/api/v1/devapi/credentials"),
      request("GET", "/api/v1/devapi/pairing"),
    ]);
    if (c.status === 200) setCreds(c.body.credentials);
    if (p.status === 200) setPairing(p.body.pairing);
  }
  useEffect(() => {
    load();
  }, []);

  async function revoke(credentialId: string) {
    await request("POST", `/api/v1/devapi/credentials/${credentialId}/revoke`);
    load();
  }

  return (
    <main className="container" style={{ paddingTop: 56, paddingBottom: 80 }}>
      <div className="row between">
        <div>
          <h1 className="section-title" style={{ fontSize: 26, margin: 0 }}>Developer API</h1>
          <p className="muted" style={{ marginTop: 4, fontSize: 14 }}>
            Scoped API credentials for authenticated Termux installations. Credentials are
            least-privilege; no Owner-level scope exists.
          </p>
        </div>
        <Link to="/devices" className="btn">Manage devices</Link>
      </div>

      {pairing.length > 0 && (
        <GlassCard strong style={{ padding: 24, marginTop: 24 }}>
          <Badge tone="warn">Pending device pairings</Badge>
          <p className="muted mt-8" style={{ fontSize: 14 }}>
            {pairing.length} device(s) waiting for approval. Approve from a Termux
            device via <span className="mono">ghostlink developer login</span>.
          </p>
        </GlassCard>
      )}

      <div className="stack mt-24">
        {creds.length === 0 && (
          <Card><EmptyState title="No developer API credentials yet." /></Card>
        )}
        {creds.map((c) => (
          <Card key={c.credential_id}>
            <div className="row between">
              <div>
                <div className="row gap-8">
                  <span style={{ fontWeight: 600 }}>{c.name}</span>
                  <span className="mono muted">{c.credential_id}</span>
                  <StatusBadge status={c.status} />
                </div>
                <div className="row gap-8 mt-8" style={{ flexWrap: "wrap" }}>
                  {c.scopes.map((s) => (
                    <Badge key={s} tone="accent">{SCOPE_LABELS[s] ?? s}</Badge>
                  ))}
                </div>
                <p className="muted mono" style={{ marginTop: 8, fontSize: 12 }}>
                  Created {c.created_at ? new Date(c.created_at).toLocaleDateString() : "—"} · Last
                  used {c.last_used_at ? new Date(c.last_used_at).toLocaleString() : "never"}
                </p>
              </div>
              {c.status === "active" && (
                <Button variant="danger" onClick={() => revoke(c.credential_id)}>Revoke</Button>
              )}
            </div>
          </Card>
        ))}
      </div>
    </main>
  );
}
