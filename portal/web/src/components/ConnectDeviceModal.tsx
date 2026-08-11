import { useEffect, useState } from "react";
import { request } from "../lib/api";
import { Button, GlassCard, Loading } from "./ui";
import { QrCodeView } from "./QrCodeView";

interface PairingSession {
  pairing_code: string;
  nonce: string;
  device_id: string;
  qr_payload: string;
  expires_in: number;
}

export function ConnectDeviceModal({
  isOpen,
  onClose,
  onConnected,
}: {
  isOpen: boolean;
  onClose: () => void;
  onConnected?: () => void;
}) {
  const [session, setSession] = useState<PairingSession | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [paired, setPaired] = useState(false);
  const [tab, setTab] = useState<"qr" | "code">("code");
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!isOpen) {
      setSession(null);
      setPaired(false);
      setError("");
      setCopied(false);
      return;
    }

    let active = true;
    let pollInterval: any = null;

    async function initSession() {
      setLoading(true);
      setError("");
      try {
        const res = await request("POST", "/api/v1/devapi/pairing/create", {
          device_name: "Termux Android",
          scopes: "project:read device:read credential:read",
        });
        if (!active) return;
        if (res.status === 201) {
          setSession(res.body);
          // Start polling status
          pollInterval = setInterval(async () => {
            if (!active) return;
            const sRes = await request(
              "GET",
              `/api/v1/devapi/pairing/status?code=${encodeURIComponent(res.body.pairing_code)}`
            );
            if (sRes.status === 200 && sRes.body.status === "used") {
              setPaired(true);
              clearInterval(pollInterval);
              if (onConnected) onConnected();
            }
          }, 1500);
        } else {
          setError(res.body?.error?.message || "Could not generate pairing session.");
        }
      } catch {
        if (active) setError("Could not reach portal backend.");
      } finally {
        if (active) setLoading(false);
      }
    }

    initSession();

    return () => {
      active = false;
      if (pollInterval) clearInterval(pollInterval);
    };
  }, [isOpen, onConnected]);

  if (!isOpen) return null;

  function copyCode() {
    if (session?.pairing_code) {
      navigator.clipboard?.writeText(session.pairing_code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    }
  }

  return (
    <div
      style={{
        position: "fixed",
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        background: "rgba(15, 23, 42, 0.65)",
        backdropFilter: "blur(6px)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1000,
        padding: 16,
      }}
      onClick={onClose}
    >
      <div onClick={(e) => e.stopPropagation()} style={{ maxWidth: 480, width: "100%" }}>
        <GlassCard strong style={{ padding: 32, position: "relative" }}>
          <button
            onClick={onClose}
            style={{
              position: "absolute",
              top: 16,
              right: 16,
              background: "transparent",
              border: "none",
              fontSize: 20,
              cursor: "pointer",
              color: "var(--muted)",
            }}
            aria-label="Close"
          >
            ×
          </button>

          {paired ? (
            <div style={{ textAlign: "center", padding: "16px 0" }}>
              <div
                style={{
                  width: 56,
                  height: 56,
                  borderRadius: "50%",
                  background: "rgba(52, 211, 153, 0.15)",
                  color: "var(--success, #34D399)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  fontSize: 28,
                  margin: "0 auto 16px",
                  fontWeight: 700,
                }}
              >
                ✓
              </div>
              <h2 className="section-title" style={{ fontSize: 22 }}>
                Device Connected
              </h2>
              <p className="muted" style={{ marginTop: 8, fontSize: 14 }}>
                Termux Android is now connected to your developer account with scoped permissions.
              </p>
              <div style={{ marginTop: 24 }}>
                <Button variant="primary" onClick={onClose} style={{ width: "100%" }}>
                  Done
                </Button>
              </div>
            </div>
          ) : (
            <div>
              <h2 className="section-title" style={{ fontSize: 22 }}>
                Connect this Termux Device
              </h2>
              <p className="muted" style={{ marginTop: 6, fontSize: 14, lineHeight: 1.5 }}>
                Pair your Termux terminal without exposing permanent secrets.
              </p>

              {loading && (
                <div style={{ padding: "40px 0", textAlign: "center" }}>
                  <Loading label="Generating secure pairing code…" />
                </div>
              )}

              {error && (
                <div style={{ padding: "20px 0" }}>
                  <p className="error-text">{error}</p>
                  <Button variant="ghost" onClick={onClose} style={{ marginTop: 12 }}>
                    Close
                  </Button>
                </div>
              )}

              {!loading && session && (
                <div>
                  <div
                    style={{
                      display: "flex",
                      gap: 8,
                      margin: "20px 0 16px",
                      borderBottom: "1px solid var(--border)",
                      paddingBottom: 8,
                    }}
                  >
                    <button
                      type="button"
                      className={`btn ${tab === "code" ? "btn-primary" : "btn-ghost"}`}
                      style={{ flex: 1, padding: "6px 12px", fontSize: 13 }}
                      onClick={() => setTab("code")}
                    >
                      Pairing Code
                    </button>
                    <button
                      type="button"
                      className={`btn ${tab === "qr" ? "btn-primary" : "btn-ghost"}`}
                      style={{ flex: 1, padding: "6px 12px", fontSize: 13 }}
                      onClick={() => setTab("qr")}
                    >
                      Scan QR Code
                    </button>
                  </div>

                  {tab === "code" ? (
                    <div style={{ textAlign: "center", padding: "8px 0" }}>
                      <p className="muted" style={{ fontSize: 13, marginBottom: 12 }}>
                        In Termux, run:
                      </p>
                      <pre
                        className="mono"
                        style={{
                          background: "rgba(0,0,0,0.06)",
                          padding: "10px 14px",
                          borderRadius: 6,
                          fontSize: 14,
                          fontWeight: 600,
                          margin: "0 0 16px",
                          userSelect: "all",
                        }}
                      >
                        ghostlink developer pair {session.pairing_code}
                      </pre>

                      <div
                        style={{
                          background: "var(--card-bg, #fff)",
                          border: "1px solid var(--border)",
                          borderRadius: 8,
                          padding: 16,
                          marginBottom: 16,
                        }}
                      >
                        <span className="muted" style={{ fontSize: 12, display: "block" }}>
                          Single-use pairing code:
                        </span>
                        <div
                          className="mono"
                          style={{
                            fontSize: 22,
                            fontWeight: 700,
                            letterSpacing: "0.08em",
                            color: "var(--accent, #6366f1)",
                            margin: "6px 0",
                          }}
                        >
                          {session.pairing_code}
                        </div>
                        <Button
                          variant="ghost"
                          onClick={copyCode}
                          style={{ fontSize: 12, padding: "4px 10px", marginTop: 4 }}
                        >
                          {copied ? "Copied to clipboard" : "Copy pairing code"}
                        </Button>
                      </div>

                      <p className="muted" style={{ fontSize: 12 }}>
                        Waiting for Termux connection… (expires in 10 minutes)
                      </p>
                    </div>
                  ) : (
                    <div style={{ textAlign: "center", padding: "8px 0" }}>
                      <QrCodeView value={session.qr_payload} size={180} />
                      <p className="muted" style={{ fontSize: 13, marginTop: 12 }}>
                        Scan or paste QR payload into Termux
                      </p>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </GlassCard>
      </div>
    </div>
  );
}
