import { PasswordChangeForm } from "../components/PasswordChangeForm";

export function AccountSettingsPage() {
  return (
    <div className="card" style={{ maxWidth: 420 }}>
      <h2>Change password</h2>
      <PasswordChangeForm />
    </div>
  );
}
