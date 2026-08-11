import { NavLink, Link, useNavigate } from "react-router-dom";
import { useAuth } from "../lib/auth";

const authed = [
  { to: "/quickstart", label: "Quick start" },
  { to: "/dashboard", label: "Dashboard" },
  { to: "/developer-api", label: "Developer API" },
  { to: "/devices", label: "Devices" },
  { to: "/projects", label: "Projects" },
  { to: "/api-activity", label: "API activity" },
  { to: "/security", label: "Security" },
];

export default function Nav() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  async function onLogout() {
    await logout();
    navigate("/");
  }

  return (
    <header className="nav">
      <div className="container nav-inner">
        <Link to="/" className="brand">
          <span className="brand-mark">G</span>
          <span>GhostLink</span>
        </Link>
        <nav className="nav-links" aria-label="Primary">
          {user ? (
            <>
              {authed.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) => (isActive ? "active" : "")}
                >
                  {item.label}
                </NavLink>
              ))}
              <button onClick={onLogout}>Sign out</button>
            </>
          ) : (
            <>
              <Link to="/sign-in">Sign in</Link>
              <Link to="/sign-up" className="btn btn-primary" style={{ marginLeft: 4 }}>
                Get started
              </Link>
            </>
          )}
        </nav>
      </div>
    </header>
  );
}
