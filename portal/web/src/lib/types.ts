// Shared types mirroring the backend API responses.

export interface User {
  id: number;
  email: string;
  status: string;
  emailVerified: boolean;
  developerId: string;
  createdAt: string;
}

export interface CredentialMeta {
  id: number;
  name: string;
  keyId: string;
  status: string;
  createdAt: string;
  lastUsedAt: string | null;
  rotatedAt: string | null;
  revokedAt: string | null;
}

export interface NewCredential {
  keyId: string;
  name: string;
  id: number | null;
  secret: string;
}

export interface Project {
  id: number;
  name: string;
  slug: string;
  status: string;
  createdAt: string;
}

export interface Session {
  id: number;
  createdAt: string;
  lastActiveAt: string;
  expiresAt: string;
  revokedAt: string | null;
  device: string;
  browser: string;
  os: string;
  current?: boolean;
}

export interface ActivityEvent {
  action: string;
  actor: string;
  metadata: string;
  created_at: string;
}

export interface DashboardData {
  user: User;
  counts: {
    credentials: number;
    projects: number;
    sessions: number;
    mfaEnabled: boolean;
  };
}
