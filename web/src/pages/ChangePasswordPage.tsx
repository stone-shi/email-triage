import { useNavigate } from "react-router-dom";
import { PasswordChangeForm } from "../components/PasswordChangeForm";

export function ChangePasswordPage() {
  const navigate = useNavigate();

  return (
    <div className="centered-page">
      <div className="card auth-card">
        <h2>Change your password</h2>
        <p className="muted">You must set a new password before continuing.</p>
        <PasswordChangeForm onDone={() => navigate("/", { replace: true })} />
      </div>
    </div>
  );
}
