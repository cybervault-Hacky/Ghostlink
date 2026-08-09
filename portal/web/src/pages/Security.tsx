import { useState } from "react";
import { Link } from "react-router-dom";
import { request } from "../lib/api";
import { Badge, Button, Card, Field, GlassCard } from "../components/ui";

export default function Security() {
  const [mfaSecret, setMfaSecret] = useState("");
  const [otpauth, setOtpauth] = useState("");
  const [code, setCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");

  async function setupMfa() {
    setError("");
    const res = await request("POST", "/api/v1/security/mfa/setup");
    if (res.status !== 200) {
      setError(res.body?.error?.message || "Could not start MFA setup.");
      return;
    }
    setMfaSecret(res.body.secret);
    setOtpauth(res.body.otpauth);
  }

  async function confirmMfa(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const res = await request("POST", "/api/v1/security/mfa/setup", { confirm: true, code });
    if (res.status !== 201) {
      setError(res.body?.error?.message || "Invalid code.");
      return;
    }
    setRecoveryCodes(res.body.recoveryCodes);
    setMfaSecret("");
    setCode("");
  }

  async function disableMfa() {
    await request("POST", "/api/v1/security/mfa/disable");
    setRecoveryCodes([]);
  }

  async function changePassword(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const res = await request("POST", "/api/v1/security/password", {
      currentPassword,
      newPassword,
    });
    if (res.status !== 200) {
      setError(res.body?.error?.message || "Could not change password.");
      return;
    }
    setCurrentPassword("");
    setNewPassword("");
  }

  return (
    <main className="container" style={{ paddingTop: 56, paddingBottom: 80 }}>
      <div className="row between">
        <div>
          <h1 className="section-title" style={{ fontSize: 26, margin: 0 }}>Security center</h1>
          <p className="muted" style={{ marginTop: 4, fontSize: 14 }}>Protect your account and review access.</p>
        </div>
      </div>

      <div className="grid-3 mt-24">
        <Card>
          <Badge tone="accent">Account security</Badge>
          <div className="stack mt-16">
            <Link to="/sessions" className="muted">Sessions & devices →</Link>
            <Link to="/activity" className="muted">Security activity →</Link>
            <Link to="/settings" className="muted">Settings →</Link>
          </div>
        </Card>
        <Card>
          <Badge tone="accent">Two-factor (TOTP)</Badge>
          {mfaSecret ? (
            <div className="mt-16">
              <p className="mono muted" style={{ fontSize: 12 }}>Scan with your authenticator app, then enter a code.</p>
              <p className="mono" style={{ wordBreak: "break-all", fontSize: 13 }}>{otpauth}</p>
              <form onSubmit={confirmMfa}>
                <Field label="Authentication code">
                  <input className="input" value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" />
                </Field>
                <Button variant="primary">Enable MFA</Button>
              </form>
            </div>
          ) : (
            <>
              <p className="muted mt-8" style={{ fontSize: 14 }}>Add an authenticator app for an extra layer of security.</p>
              <Button className="mt-8" onClick={setupMfa}>Set up MFA</Button>
              <Button variant="danger" className="mt-8" onClick={disableMfa}>Disable MFA</Button>
            </>
          )}
        </Card>
        <Card>
          <Badge tone="accent">Passkey (WebAuthn)</Badge>
          <p className="muted mt-8" style={{ fontSize: 14 }}>
            Register your device passkey for passwordless sign-in. The browser
            handles biometrics; we only receive cryptographic assertions.
          </p>
        </Card>
      </div>

      {recoveryCodes.length > 0 && (
        <GlassCard strong style={{ padding: 28, marginTop: 24 }}>
          <Badge tone="warn">Save these recovery codes now. They are shown once.</Badge>
          <div className="mono mt-16" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
            {recoveryCodes.map((c) => <span key={c}>{c}</span>)}
          </div>
        </GlassCard>
      )}

      <Card style={{ marginTop: 24 }}>
        <h3 className="section-title">Change password</h3>
        <form onSubmit={changePassword} style={{ maxWidth: 420 }}>
          <Field label="Current password">
            <input className="input" type="password" value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} required />
          </Field>
          <Field label="New password">
            <input className="input" type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} required />
          </Field>
          {error && <p className="error-text">{error}</p>}
          <Button variant="primary">Update password</Button>
        </form>
      </Card>
    </main>
  );
}
