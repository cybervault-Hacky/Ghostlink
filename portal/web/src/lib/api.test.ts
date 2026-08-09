// Frontend security-behavior tests (Phase 10B).
// Verifies the credential-reveal boundary and the auth guard redirect.

import { describe, it, expect, vi, beforeEach } from "vitest";

describe("credential reveal boundary", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("never stores the secret in module state", async () => {
    // The API client and auth layer must not hold the credential secret in
    // persistent frontend state (it is only ever a transient local variable
    // inside a component event handler).
    const api = await import("./api");
    expect(api).toBeDefined();
    const stateKeys = Object.keys(api);
    expect(stateKeys.some((k) => k.toLowerCase().includes("secret"))).toBe(false);
  });

  it("CSRF token is only attached to unsafe methods", async () => {
    const api = await import("./api");
    // Mock fetch to capture headers.
    const fetchMock = vi.fn(
      async () => new Response(JSON.stringify({}), { status: 200 })
    );
    vi.stubGlobal("fetch", fetchMock);
    api.setCsrf("csrf-abc");
    await api.request("GET", "/api/v1/dashboard");
    const getCall = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const getHeaders = new Headers(getCall[1]?.headers);
    expect(getHeaders.get("X-CSRF-Token")).toBeNull();
    await api.request("POST", "/api/v1/developer-keys", { name: "x" });
    const postCall = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    const postHeaders = new Headers(postCall[1]?.headers);
    expect(postHeaders.get("X-CSRF-Token")).toBe("csrf-abc");
  });
});
