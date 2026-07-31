import { FormEvent, useState } from "react";
import { auth, ApiError } from "../lib/api";
import { useAuth } from "../hooks/useAuth";

export function PasswordChangeForm({ onDone }: { onDone?: () => void }) {
  const { refresh } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSuccess(null);
    if (next !== confirm) {
      setError("New password and confirmation don't match");
      return;
    }
    setSubmitting(true);
    try {
      const result = await auth.changePassword(current, next);
      await refresh();
      setSuccess(
        result.revoked_sessions > 0
          ? `Password changed. ${result.revoked_sessions} other session(s) were signed out.`
          : "Password changed."
      );
      setCurrent("");
      setNext("");
      setConfirm("");
      onDone?.();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to change password");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={onSubmit}>
      <div className="field">
        <label htmlFor="current-password">Current password</label>
        <input
          id="current-password"
          type="password"
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
          required
          style={{ width: "100%" }}
        />
      </div>
      <div className="field">
        <label htmlFor="new-password">New password</label>
        <input
          id="new-password"
          type="password"
          value={next}
          onChange={(e) => setNext(e.target.value)}
          required
          minLength={10}
          style={{ width: "100%" }}
        />
      </div>
      <div className="field">
        <label htmlFor="confirm-password">Confirm new password</label>
        <input
          id="confirm-password"
          type="password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          required
          minLength={10}
          style={{ width: "100%" }}
        />
      </div>
      {error && <p className="error-text">{error}</p>}
      {success && <p className="success-text">{success}</p>}
      <button type="submit" className="primary" disabled={submitting}>
        {submitting ? "Changing…" : "Change password"}
      </button>
    </form>
  );
}
