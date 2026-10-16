import { useEffect, useState, type FormEvent } from "react";
import { api, login, type Session } from "../api/client";
import { ErrorNote } from "../components/ui";

// Administrator sign-in. The password is checked by the API against the hash held in Key Vault.
export function LoginPage({ onSignedIn }: { onSignedIn: (s: Session) => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.health().catch(() => setError("The Workbench API is not reachable."));
  }, []);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await login(username.trim(), password));
    } catch (err) {
      setError((err as Error).message);
      setPassword("");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="login">
      <form className="modal" onSubmit={submit}>
        <div className="eyebrow">ADMINISTRATOR SIGN-IN</div>
        <h2>Migration Workbench</h2>
        <label className="field">Username
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required />
        </label>
        <label className="field">Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required autoFocus />
        </label>
        <ErrorNote error={error} />
        <div className="actions">
          <button type="submit" disabled={busy || !password}>{busy ? "Signing in..." : "Sign in"}</button>
        </div>
      </form>
    </div>
  );
}
