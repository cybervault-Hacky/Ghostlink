import { useEffect, useState } from "react";
import { request } from "../lib/api";
import { Card } from "../components/ui";
import type { ActivityEvent } from "../lib/types";

export default function Activity() {
  const [events, setEvents] = useState<ActivityEvent[]>([]);

  useEffect(() => {
    request("GET", "/api/v1/activity").then((res) => {
      if (res.status === 200) setEvents(res.body.events);
    });
  }, []);

  return (
    <main className="container" style={{ paddingTop: 56, paddingBottom: 80 }}>
      <h1 className="section-title" style={{ fontSize: 26 }}>Security activity</h1>
      <p className="muted" style={{ marginTop: 4, fontSize: 14 }}>A metadata-only audit trail of your account.</p>
      <div className="stack mt-24">
        {events.length === 0 && <Card className="empty">No activity yet.</Card>}
        {events.map((e, i) => (
          <Card key={i}>
            <div className="row between">
              <span className="mono" style={{ fontSize: 13 }}>{e.action}</span>
              <span className="muted" style={{ fontSize: 12 }}>{new Date(e.created_at).toLocaleString()}</span>
            </div>
          </Card>
        ))}
      </div>
    </main>
  );
}
