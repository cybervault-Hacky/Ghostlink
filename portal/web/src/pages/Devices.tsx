import { useCallback, useEffect, useState } from "react";
import { request } from "../lib/api";
import { Badge, Button, Card, EmptyState, ErrorState, Loading, StatusBadge } from "../components/ui";
import { ConnectDeviceModal } from "../components/ConnectDeviceModal";

interface Device {
  device_id: string;
  name: string;
  platform: string;
  client_version: string;
  status: string;
  created_at: string;
  last_seen_at: string | null;
}

export default function Devices() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showPairModal, setShowPairModal] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await request("GET", "/api/v1/devapi/devices");
      if (res.status === 200) {
        setDevices(res.body.devices);
      } else {
        setError(res.body?.error?.message ?? "Unable to load devices.");
      }
    } catch {
      setError("Unable to reach the portal.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function revoke(deviceId: string) {
    await request("POST", `/api/v1/devapi/devices/${deviceId}/revoke`);
    load();
  }

  return (
    <main className="container" style={{ paddingTop: 56, paddingBottom: 80 }}>
      <div className="row between">
        <div>
          <h1 className="section-title" style={{ fontSize: 26 }}>Devices</h1>
          <p className="muted" style={{ marginTop: 4, fontSize: 14 }}>
            Termux installations registered to this developer account. Revoking a
            device instantly invalidates its tokens.
          </p>
        </div>
        <Button variant="primary" onClick={() => setShowPairModal(true)}>
          Connect device
        </Button>
      </div>
      <div className="stack mt-24">
        {loading && <Loading label="Loading devices…" />}
        {!loading && error && <ErrorState title="Could not load devices" message={error} onRetry={load} />}
        {!loading && !error && devices.length === 0 && (
          <Card><EmptyState title="No devices registered yet." /></Card>
        )}
        {!loading && !error && devices.map((d) => (
          <Card key={d.device_id}>
            <div className="row between">
              <div>
                <div className="row gap-8">
                  <span style={{ fontWeight: 600 }}>{d.name}</span>
                  <span className="mono muted">{d.device_id}</span>
                  <StatusBadge status={d.status} />
                  {d.platform && <Badge tone="accent">{d.platform}</Badge>}
                </div>
                <p className="muted mono" style={{ marginTop: 8, fontSize: 12 }}>
                  Created {d.created_at ? new Date(d.created_at).toLocaleString() : "—"} · Last seen{" "}
                  {d.last_seen_at ? new Date(d.last_seen_at).toLocaleString() : "never"}
                </p>
              </div>
              {d.status === "active" && (
                <Button variant="danger" onClick={() => revoke(d.device_id)}>Revoke</Button>
              )}
            </div>
          </Card>
        ))}
      </div>

      <ConnectDeviceModal
        isOpen={showPairModal}
        onClose={() => setShowPairModal(false)}
        onConnected={load}
      />
    </main>
  );
}
