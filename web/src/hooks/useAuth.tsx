import { createContext, useCallback, useContext, useEffect, useRef, useState, ReactNode } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { auth, User, onPasswordChangeRequired, onUnauthorized } from "../lib/api";

type AuthStatus = "loading" | "anonymous" | "authenticated";

interface AuthContextValue {
  status: AuthStatus;
  user: User | null;
  mustChangePassword: boolean;
  login: (username: string, password: string) => Promise<{ mustChangePassword: boolean }>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [user, setUser] = useState<User | null>(null);
  const navigate = useNavigate();
  const location = useLocation();
  const locationRef = useRef(location);

  useEffect(() => {
    locationRef.current = location;
  }, [location]);

  const refresh = useCallback(async () => {
    try {
      const me = await auth.me();
      setUser(me);
      setStatus("authenticated");
    } catch {
      setUser(null);
      setStatus("anonymous");
    }
  }, []);

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const offUnauthorized = onUnauthorized(() => {
      setUser(null);
      setStatus("anonymous");
      const { pathname, search } = locationRef.current;
      if (pathname === "/login") return;
      const next = encodeURIComponent(pathname + search);
      navigate(`/login?next=${next}`, { replace: true });
    });
    const offPasswordChange = onPasswordChangeRequired(() => {
      navigate("/change-password", { replace: true });
    });
    return () => {
      offUnauthorized();
      offPasswordChange();
    };
  }, [navigate]);

  const login = useCallback(async (username: string, password: string) => {
    const result = await auth.login(username, password);
    setUser(result.user);
    setStatus("authenticated");
    return { mustChangePassword: result.must_change_password };
  }, []);

  const logout = useCallback(async () => {
    await auth.logout();
    setUser(null);
    setStatus("anonymous");
  }, []);

  const value: AuthContextValue = {
    status,
    user,
    mustChangePassword: Boolean(user?.must_change_password),
    login,
    logout,
    refresh,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
