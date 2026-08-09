import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { signIn } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Button, Field, GlassCard } from "../components/ui";

export default function SignIn() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [mfa, setMfa] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [preauth, setPreauth] = useState("");
  const [error, setError] = useState("");
  const navigate = useNavigate();
  const { refresh } = useAuth();

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const res = await signIn(email, password);
    if (res.status === 200 && res.body.mfaRequired) {
      setPreauth(res.body.preauth);
      setMfa("required");
      return;
    }
    if (res.status !== 200) {
      setError(res.body?.error?.message || "Sign in failed.");
      return;
    }
    await refresh();
    navigate("/dashboard");
  }

  async function onMfa(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const res = await fetch("/api/v1/auth/mfa", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ preauth, code }),
    });
    const body = await res.json();
    if (res.status !== 200) {
      setError(body?.error?.message || "MFA failed.");
      return;
    }
    await refresh();
    navigate("/dashboard");
  }

  return (
    <div className="center">
      <div className="container" style={{ maxWidth: 420 }}>
        <GlassCard strong style={{ padding: 28 }}>
          <h2 className="section-title" style={{ marginTop: 0 }}>Sign in</h2>
          {mfa === "required" ? (
            <form onSubmit={onMfa}>
              <Field label="Authentication code">
                <input className="input" value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" autoFocus />
              </Field>
              {error && <p className="error-text" role="alert">{error}</p>}
              <Button variant="primary" style={{ width: "100%" }}>Verify code</Button>
            </form>
          ) : (
            <form onSubmit={onSubmit}>
              <Field label="Email">
                <input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
              </Field>
              <Field label="Password">
                <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
              </Field>
              {error && <p className="error-text" role="alert">{error}</p>}
              <Button variant="primary" style={{ width: "100%" }}>Sign in</Button>
            </form>
          )}
          <p className="muted mt-16" style={{ fontSize: 13 }}>
            New here? <Link to="/sign-up">Create an account</Link>
          </p>
        </GlassCard>
      </div>
    </div>
  );
}
