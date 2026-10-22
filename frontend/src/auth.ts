/**
 * Operator Authentication Store & Utilities (ADR-0004).
 */

const TOKEN_KEY = "bayaan_operator_token";
const USER_KEY = "bayaan_operator_user";

export interface LoginResult {
  access_token: string;
  token_type: string;
  expires_in_hours: number;
  username: string;
}

export function getStoredToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function getStoredUsername(): string | null {
  return localStorage.getItem(USER_KEY);
}

export function saveAuth(token: string, username: string): void {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(USER_KEY, username);
}

export function clearAuth(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

export function isAuthenticated(): boolean {
  return Boolean(getStoredToken());
}

export async function loginOperator(username: string, password: string): Promise<LoginResult> {
  const resp = await fetch("/api/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });

  if (!resp.ok) {
    const errorBody = await resp.json().catch(() => ({}));
    throw new Error(errorBody.detail || "Authentication failed. Invalid username or password.");
  }

  const data: LoginResult = await resp.json();
  saveAuth(data.access_token, data.username);
  return data;
}

export function logoutOperator(): void {
  clearAuth();
  window.location.href = "/login";
}
