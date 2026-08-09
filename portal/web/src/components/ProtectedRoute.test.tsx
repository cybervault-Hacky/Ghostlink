import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import ProtectedRoute from "./ProtectedRoute";
import { AuthContext } from "../lib/auth";

// A minimal AuthContext provider that lets us control the auth state.
function WithAuth({ children, authed }: { children: React.ReactNode; authed: boolean }) {
  const value = {
    user: authed ? { id: 1, email: "a@b.com", status: "active", emailVerified: true, developerId: "dev_x", createdAt: "" } : null,
    loading: false,
    refresh: async () => {},
    logout: async () => {},
  };
  return (
    <AuthContext.Provider value={value as React.ContextType<typeof AuthContext>}>
      {children}
    </AuthContext.Provider>
  );
}

describe("ProtectedRoute", () => {
  it("redirects unauthenticated users to /sign-in", () => {
    render(
      <WithAuth authed={false}>
        <MemoryRouter initialEntries={["/dashboard"]}>
          <Routes>
            <Route path="/dashboard" element={<ProtectedRoute><div>Secret dashboard</div></ProtectedRoute>} />
            <Route path="/sign-in" element={<div>Sign in page</div>} />
          </Routes>
        </MemoryRouter>
      </WithAuth>
    );
    expect(screen.getByText("Sign in page")).toBeTruthy();
    expect(screen.queryByText("Secret dashboard")).toBeNull();
  });

  it("shows content for authenticated users", () => {
    render(
      <WithAuth authed={true}>
        <MemoryRouter initialEntries={["/dashboard"]}>
          <Routes>
            <Route path="/dashboard" element={<ProtectedRoute><div>Secret dashboard</div></ProtectedRoute>} />
          </Routes>
        </MemoryRouter>
      </WithAuth>
    );
    expect(screen.getByText("Secret dashboard")).toBeTruthy();
  });
});
