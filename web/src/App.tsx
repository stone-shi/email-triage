import { Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider } from "./hooks/useAuth";
import { RequireAdmin, RequireAuth, RequirePasswordChanged } from "./routes/guards";
import { AppShell } from "./components/AppShell";
import { LoginPage } from "./pages/LoginPage";
import { ChangePasswordPage } from "./pages/ChangePasswordPage";
import { DashboardPage } from "./pages/DashboardPage";
import { LogsPage } from "./pages/LogsPage";
import { SettingsLayout } from "./pages/SettingsLayout";
import { IntegrationsSettingsPage } from "./pages/IntegrationsSettingsPage";
import { McpTokenSettingsPage } from "./pages/McpTokenSettingsPage";
import { AccountSettingsPage } from "./pages/AccountSettingsPage";
import { AdminLayout } from "./pages/AdminLayout";
import { AdminUsersPage } from "./pages/AdminUsersPage";
import { AdminSystemSettingsPage } from "./pages/AdminSystemSettingsPage";
import { AdminPromptsPage } from "./pages/AdminPromptsPage";
import { NotFoundPage } from "./pages/NotFoundPage";

export function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />

        <Route element={<RequireAuth />}>
          <Route path="/change-password" element={<ChangePasswordPage />} />

          <Route element={<RequirePasswordChanged />}>
            <Route element={<AppShell />}>
              <Route path="/" element={<DashboardPage />} />

              <Route path="/settings" element={<SettingsLayout />}>
                <Route index element={<Navigate to="/settings/integrations" replace />} />
                <Route path="integrations" element={<IntegrationsSettingsPage />} />
                <Route path="mcp" element={<McpTokenSettingsPage />} />
                <Route path="password" element={<AccountSettingsPage />} />
              </Route>

              <Route element={<RequireAdmin />}>
                <Route path="/logs" element={<LogsPage />} />

                <Route path="/admin" element={<AdminLayout />}>
                  <Route index element={<Navigate to="/admin/users" replace />} />
                  <Route path="users" element={<AdminUsersPage />} />
                  <Route path="settings" element={<AdminSystemSettingsPage />} />
                  <Route path="prompts" element={<AdminPromptsPage />} />
                </Route>
              </Route>

              <Route path="*" element={<NotFoundPage />} />
            </Route>
          </Route>
        </Route>
      </Routes>
    </AuthProvider>
  );
}
