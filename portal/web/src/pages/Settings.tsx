import { useAuth } from "../lib/auth";
import { Card } from "../components/ui";

export default function Settings() {
  const { user } = useAuth();
  return (
    <main className="container" style={{ paddingTop: 56, paddingBottom: 80 }}>
      <h1 className="section-title" style={{ fontSize: 26 }}>Account settings</h1>
      <div className="stack mt-24" style={{ maxWidth: 560 }}>
        <Card>
          <h3 className="section-title">Profile</h3>
          <p className="muted mt-8" style={{ fontSize: 14 }}>Email</p>
          <p className="mono">{user?.email}</p>
          <p className="muted mt-8" style={{ fontSize: 14 }}>Developer ID</p>
          <p className="mono">{user?.developerId}</p>
          <p className="muted mt-8" style={{ fontSize: 14 }}>Account status</p>
          <p>{user?.status}</p>
        </Card>
        <Card>
          <h3 className="section-title">Security</h3>
          <p className="muted mt-8" style={{ fontSize: 14, lineHeight: 1.6 }}>
            Manage your password, two-factor authentication, and passkeys from
            the <a href="/security">Security center</a>. Review sessions in{" "}
            <a href="/sessions">Sessions</a>.
          </p>
        </Card>
      </div>
    </main>
  );
}
