import { createContext, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { request, setCsrf, clearCsrf } from "./api";
import type { User } from "./types";

interface AuthState {
  user: User | null;
  loading: boolean;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

export const AuthContext = createContext<AuthState>({
  user: null,
  loading: true,
  refresh: async () => {},
  logout: async () => {},
});

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  async function refresh() {
    const res = await request("GET", "/api/v1/dashboard");
    if (res.status === 200) {
      setUser(res.body.user);
      setCsrf(null); // dashboard returns no csrf; kept in session for mutations
    } else {
      setUser(null);
      clearCsrf();
    }
    setLoading(false);
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function logout() {
    await request("POST", "/api/v1/auth/signout");
    setUser(null);
    clearCsrf();
  }

  return (
    <AuthContext.Provider value={{ user, loading, refresh, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
