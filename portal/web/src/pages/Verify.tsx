import { useState } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import { request } from "../lib/api";
import { Button, Field, GlassCard } from "../components/ui";

export default function Verify() {
  const [params] = useSearchParams();
  const email = params.get("email") || "";
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const navigate = useNavigate();

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const res = await request("POST", "/api/v1/auth/verify", { token });
    if (res.status !== 200) {
      setError(res.body?.error?.message || "Verification failed.");
      return;
    }
    navigate("/sign-in");
  }

  return (
    <div className="center">
      <div className="container" style={{ maxWidth: 440 }}>
        <GlassCard strong style={{ padding: 28 }}>
          <h2 className="section-title" style={{ marginTop: 0 }}>Verify your email</h2>
          <p className="muted" style={{ fontSize: 14, lineHeight: 1.6 }}>
            We sent a verification link to <strong>{email || "your email"}</strong>.
            Paste the code from the email below (a development email adapter records
            it in local logs for local testing).
          </p>
          <form onSubmit={onSubmit}>
            <Field label="Verification token">
              <input className="input mono" value={token} onChange={(e) => setToken(e.target.value)} required />
            </Field>
            {error && <p className="error-text" role="alert">{error}</p>}
            <Button variant="primary" style={{ width: "100%" }}>Verify email</Button>
          </form>
        </GlassCard>
      </div>
    </div>
  );
}
