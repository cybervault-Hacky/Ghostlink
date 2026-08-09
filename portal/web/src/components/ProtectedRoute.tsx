import { Navigate } from "react-router-dom";
import type { ReactNode } from "react";
import { useAuth } from "../lib/auth";

export default function ProtectedRoute({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) {
    return <div className="center muted">Loading…</div>;
  }
  if (!user) {
    return <Navigate to="/sign-in" replace />;
  }
  return <>{children}</>;
}
