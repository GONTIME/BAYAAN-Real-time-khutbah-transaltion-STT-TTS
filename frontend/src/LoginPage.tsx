import React, { useState } from "react";
import { loginOperator } from "./auth";
import { Icon } from "./components/Icon";

export default function LoginPage() {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!username.trim() || !password.trim()) {
      setError("Please enter both username and password.");
      return;
    }
    setError(null);
    setLoading(true);
    try {
      await loginOperator(username.trim(), password.trim());
      // Redirect to dashboard
      window.location.href = "/";
    } catch (err: any) {
      setError(err?.message || "Login failed.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-shell">
      <div className="login-card">
        <div className="login-head">
          <div className="login-mark">
            <Icon name="mosque" size={28} />
          </div>
          <h1 className="login-title">BAYAAN Operations</h1>
          <p className="login-sub">
            Mosque Audio, Subtitle &amp; Translation Cockpit
          </p>
        </div>

        {error && <div className="login-alert">{error}</div>}

        <form onSubmit={handleSubmit}>
          <div className="login-field">
            <label className="login-label">Operator Username</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="e.g. admin"
              required
              className="login-input"
            />
          </div>

          <div className="login-field">
            <label className="login-label">Password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              required
              className="login-input"
            />
          </div>

          <button
            type="submit"
            disabled={loading}
            className="login-submit"
          >
            {loading ? "Authenticating..." : "Sign In to Cockpit"}
          </button>
        </form>

        <div className="login-footer">
          Protected Mosque Operations Console • BAYAAN Platform
        </div>
      </div>
    </div>
  );
}