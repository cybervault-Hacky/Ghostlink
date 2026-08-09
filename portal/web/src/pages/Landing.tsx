import { Link } from "react-router-dom";
import { GlassCard } from "../components/ui";

export default function Landing() {
  return (
    <main>
      <section className="container" style={{ paddingTop: 140, paddingBottom: 120, textAlign: "center" }}>
        <h1 className="hero-title">
          GhostLink
          <br />
          Developer Portal
        </h1>
        <p className="muted" style={{ fontSize: 18, maxWidth: 560, margin: "0 auto 32px" }}>
          Build securely. Ship with confidence.
        </p>
        <div className="row" style={{ justifyContent: "center" }}>
          <Link to="/sign-up" className="btn btn-primary">Create developer account</Link>
          <Link to="/sign-in" className="btn">Sign in</Link>
        </div>
        <div style={{ maxWidth: 720, margin: "64px auto 0" }}>
          <GlassCard strong>
            <h3 className="section-title">End-to-end trust, by design</h3>
            <p className="muted" style={{ marginTop: 8, fontSize: 15, lineHeight: 1.6 }}>
              Credentials are generated locally, shown exactly once, and stored
              only as salted verification material — never in plaintext. Rotate,
              revoke, and audit everything from one clean console.
            </p>
          </GlassCard>
        </div>
      </section>

      <section className="container" style={{ paddingBottom: 120 }}>
        <div className="grid-3">
          <div className="card">
            <h3 className="section-title">Security architecture</h3>
            <p className="muted mt-8" style={{ lineHeight: 1.6 }}>
              Passwords hashed with PBKDF2, sessions revocable, CSRF-protected,
              rate-limited, and audited. Secrets never leave your control.
            </p>
          </div>
          <div className="card">
            <h3 className="section-title">Developer workflow</h3>
            <p className="muted mt-8" style={{ lineHeight: 1.6 }}>
              Create projects and developer keys, then manage them from the CLI
              or this portal — consistently, with full metadata visibility.
            </p>
          </div>
          <div className="card">
            <h3 className="section-title">Credential management</h3>
            <p className="muted mt-8" style={{ lineHeight: 1.6 }}>
              Rotate on demand, revoke instantly, and review every security
              event. Old credentials stop working the moment you revoke.
            </p>
          </div>
        </div>
      </section>

      <footer style={{ borderTop: "1px solid var(--border)", padding: "32px 0" }}>
        <div className="container muted" style={{ fontSize: 13 }}>
          GhostLink Developer Portal · terminal-first, privacy-first. No telemetry.
        </div>
      </footer>
    </main>
  );
}
