import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { request } from "../lib/api";
import { Button, Field, GlassCard } from "../components/ui";

export default function SignUp() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const navigate = useNavigate();

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (password !== confirm) {
      setError("Passwords do not match.");
      return;
    }
    const res = await request("POST", "/api/v1/auth/signup", { email, password });
    if (res.status !== 201) {
      setError(res.body?.error?.message || "Could not create account.");
      return;
    }
    navigate(`/verify?email=${encodeURIComponent(email)}`);
  }

  return (
    <div className="center">
      <div className="container" style={{ maxWidth: 420 }}>
        <GlassCard strong style={{ padding: 28 }}>
          <h2 className="section-title" style={{ marginTop: 0 }}>Create developer account</h2>
          <form onSubmit={onSubmit}>
            <Field label="Email">
              <input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
            </Field>
            <Field label="Password">
              <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
              <span className="muted" style={{ fontSize: 12 }}>12–128 characters</span>
            </Field>
            <Field label="Confirm password">
              <input className="input" type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required />
            </Field>
            {error && <p className="error-text" role="alert">{error}</p>}
            <Button variant="primary" style={{ width: "100%" }}>Create account</Button>
          </form>
          <p className="muted mt-16" style={{ fontSize: 13 }}>
            Already have one? <Link to="/sign-in">Sign in</Link>
          </p>
        </GlassCard>
      </div>
    </div>
  );
}
