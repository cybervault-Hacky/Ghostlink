// Typed API client for the GhostLink Developer Portal backend.

export interface ApiError {
  code: string;
  message: string;
}

interface ApiResponse {
  status: number;
  body: any;
}

let csrfToken: string | null = null;

export function setCsrf(token: string | null) {
  csrfToken = token;
}

export async function request(
  method: string,
  path: string,
  data?: Record<string, unknown>
): Promise<ApiResponse> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (csrfToken && method !== "GET" && method !== "HEAD") {
    headers["X-CSRF-Token"] = csrfToken;
  }
  const res = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: data ? JSON.stringify(data) : undefined,
  });
  let body: any = {};
  try {
    body = await res.json();
  } catch {
    body = {};
  }
  return { status: res.status, body };
}

export async function signIn(
  email: string,
  password: string
): Promise<ApiResponse> {
  const res = await request("POST", "/api/v1/auth/signin", { email, password });
  if (res.status === 200 && res.body.csrfToken) {
    setCsrf(res.body.csrfToken);
  }
  return res;
}

export async function signOut(): Promise<void> {
  await request("POST", "/api/v1/auth/signout");
  setCsrf(null);
}

export function clearCsrf() {
  setCsrf(null);
}
