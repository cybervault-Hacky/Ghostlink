import { BrowserRouter, Routes, Route } from "react-router-dom";
import { AuthProvider } from "./lib/auth";
import ParticleField from "./components/ParticleField";
import Nav from "./components/Nav";
import ProtectedRoute from "./components/ProtectedRoute";
import Landing from "./pages/Landing";
import SignIn from "./pages/SignIn";
import SignUp from "./pages/SignUp";
import Verify from "./pages/Verify";
import Dashboard from "./pages/Dashboard";
import Credentials from "./pages/Credentials";
import Projects from "./pages/Projects";
import Security from "./pages/Security";
import Sessions from "./pages/Sessions";
import Activity from "./pages/Activity";
import Settings from "./pages/Settings";
import Devices from "./pages/Devices";
import DeveloperApi from "./pages/DeveloperApi";
import ApiActivity from "./pages/ApiActivity";

export default function App() {
  return (
    <AuthProvider>
      <ParticleField />
      <div className="page">
        <BrowserRouter>
          <Nav />
          <Routes>
            <Route path="/" element={<Landing />} />
            <Route path="/sign-in" element={<SignIn />} />
            <Route path="/sign-up" element={<SignUp />} />
            <Route path="/verify" element={<Verify />} />
            <Route
              path="/dashboard"
              element={
                <ProtectedRoute>
                  <Dashboard />
                </ProtectedRoute>
              }
            />
            <Route
              path="/credentials"
              element={
                <ProtectedRoute>
                  <Credentials />
                </ProtectedRoute>
              }
            />
            <Route
              path="/projects"
              element={
                <ProtectedRoute>
                  <Projects />
                </ProtectedRoute>
              }
            />
            <Route
              path="/security"
              element={
                <ProtectedRoute>
                  <Security />
                </ProtectedRoute>
              }
            />
            <Route
              path="/sessions"
              element={
                <ProtectedRoute>
                  <Sessions />
                </ProtectedRoute>
              }
            />
            <Route
              path="/activity"
              element={
                <ProtectedRoute>
                  <Activity />
                </ProtectedRoute>
              }
            />
            <Route
              path="/settings"
              element={
                <ProtectedRoute>
                  <Settings />
                </ProtectedRoute>
              }
            />
            <Route
              path="/developer-api"
              element={
                <ProtectedRoute>
                  <DeveloperApi />
                </ProtectedRoute>
              }
            />
            <Route
              path="/devices"
              element={
                <ProtectedRoute>
                  <Devices />
                </ProtectedRoute>
              }
            />
            <Route
              path="/api-activity"
              element={
                <ProtectedRoute>
                  <ApiActivity />
                </ProtectedRoute>
              }
            />
          </Routes>
        </BrowserRouter>
      </div>
    </AuthProvider>
  );
}
