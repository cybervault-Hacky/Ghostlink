import { useEffect, useState } from "react";
import { request } from "../lib/api";
import { Badge, Button, Card, Field, EmptyState, GlassCard, StatusBadge } from "../components/ui";
import type { CredentialMeta, NewCredential } from "../lib/types";

export default function Credentials() {
  const [creds, setCreds] = useState<CredentialMeta[]>([]);
  const [showNew, setShowNew] = useState(false);
  const [name, setName] = useState("");
  const [newCred, setNewCred] = useState<NewCredential | null>(null);
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState<string | null>(null);

  async function load() {
    const res = await request("GET", "/api/v1/developer-keys");
    if (res.status === 200) setCreds(res.body.credentials);
  }
  useEffect(() => {
    load();
  }, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const res = await request("POST", "/api/v1/developer-keys", { name });
    if (res.status !== 201) {
      setError(res.body?.error?.message || "Could not create key.");
      return;
    }
    setNewCred(res.body.credential);
    setShowNew(false);
    setName("");
    load();
  }

  async function revoke(keyId: string) {
    await request("POST", `/api/v1/developer-keys/${keyId}/revoke`);
    setConfirming(null);
    load();
  }

  async function rotate(keyId: string) {
    const res = await request("POST", `/api/v1/developer-keys/${keyId}/rotate`);
    if (res.status === 200) {
      setNewCred(res.body.credential);
      load();
    }
  }

  return (
    <main className="container" style={{ paddingTop: 56, paddingBottom: 80 }}>
      <div className="row between">
        <div>
          <h1 className="section-title" style={{ fontSize: 26, margin: 0 }}>Developer credentials</h1>
          <p className="muted" style={{ marginTop: 4, fontSize: 14 }}>Manage API keys for your developer account.</p>
        </div>
        <Button variant="primary" onClick={() => setShowNew(true)}>Create developer key</Button>
      </div>

      {newCred && (
        <GlassCard strong style={{ padding: 28, marginTop: 24 }}>
          <Badge tone="warn">Copy this secret now. You will not be able to view it again.</Badge>
          <p className="mono" style={{ margin: "16px 0", fontSize: 14, wordBreak: "break-all" }}>{newCred.secret}</p>
          <Button onClick={() => navigator.clipboard?.writeText(newCred.secret)}>Copy secret</Button>
          <Button variant="ghost" onClick={() => setNewCred(null)}>Done</Button>
        </GlassCard>
      )}

      {showNew && (
        <GlassCard strong style={{ padding: 28, marginTop: 24 }}>
          <form onSubmit={create}>
            <Field label="Key name">
              <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="production-key" required />
            </Field>
            {error && <p className="error-text" role="alert">{error}</p>}
            <Button variant="primary">Generate key</Button>
          </form>
        </GlassCard>
      )}

      <div className="stack mt-24">
        {creds.length === 0 && !showNew && (
          <Card><EmptyState title="No developer keys yet." cta={<Button onClick={() => setShowNew(true)}>Create one</Button>} /></Card>
        )}
        {creds.map((c) => (
          <Card key={c.keyId}>
            <div className="row between">
              <div>
                <div className="row gap-8">
                  <span style={{ fontWeight: 600 }}>{c.name}</span>
                  <span className="mono muted">{c.keyId}</span>
                  <StatusBadge status={c.status} />
                </div>
                <p className="muted mono" style={{ marginTop: 8, fontSize: 12 }}>
                  Created {new Date(c.createdAt).toLocaleDateString()} · Last used {c.lastUsedAt ? new Date(c.lastUsedAt).toLocaleString() : "never"}
                </p>
              </div>
              <div className="row">
                {c.status === "active" && (
                  <>
                    <Button variant="ghost" onClick={() => setConfirming(c.keyId)}>Rotate</Button>
                    <Button variant="danger" onClick={() => setConfirming(`revoke:${c.keyId}`)}>Revoke</Button>
                  </>
                )}
              </div>
            </div>
            {confirming === c.keyId && (
              <div className="card mt-16">
                <p className="muted">Rotate this key? The current credential will stop working immediately, and a new one will be shown once.</p>
                <div className="row">
                  <Button variant="danger" onClick={() => rotate(c.keyId)}>Rotate key</Button>
                  <Button variant="ghost" onClick={() => setConfirming(null)}>Cancel</Button>
                </div>
              </div>
            )}
            {confirming === `revoke:${c.keyId}` && (
              <div className="card mt-16">
                <p className="muted">Revoke {c.keyId}? This is irreversible. Create a new key to re-access.</p>
                <div className="row">
                  <Button variant="danger" onClick={() => revoke(c.keyId)}>Revoke key</Button>
                  <Button variant="ghost" onClick={() => setConfirming(null)}>Cancel</Button>
                </div>
              </div>
            )}
          </Card>
        ))}
      </div>
    </main>
  );
}
