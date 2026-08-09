import { useEffect, useState } from "react";
import { request } from "../lib/api";
import { Card } from "../components/ui";

interface ApiEvent {
  endpoint: string;
  category: string;
  result: string;
  created_at: string;
}

export default function ApiActivity() {
  const [events, setEvents] = useState<ApiEvent[]>([]);

  useEffect(() => {
    request("GET", "/api/v1/devapi/activity").then((res) => {
      if (res.status === 200) setEvents(res.body.events);
    });
  }, []);

  return (
    <main className="container" style={{ paddingTop: 56, paddingBottom: 80 }}>
      <h1 className="section-title" style={{ fontSize: 26 }}>API activity</h1>
      <p className="muted" style={{ marginTop: 4, fontSize: 14 }}>
        Metadata-only API usage history. No tokens, secrets, or request bodies are recorded.
      </p>
      <div className="stack mt-24">
        {events.length === 0 && <Card className="empty">No API activity yet.</Card>}
        {events.map((e, i) => (
          <Card key={i}>
            <div className="row between">
              <span className="mono" style={{ fontSize: 13 }}>
                {e.category} · {e.endpoint}
              </span>
              <span className="muted" style={{ fontSize: 12 }}>
                {e.result} · {new Date(e.created_at).toLocaleString()}
              </span>
            </div>
          </Card>
        ))}
      </div>
    </main>
  );
}
