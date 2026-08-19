import { ReactNode, createContext, useContext } from "react";

import { AuthUser } from "../api/authClient";

export type AuthSession = {
  user: AuthUser;
  accountSettingsOpen: boolean;
  openAccountSettings: () => void;
  closeAccountSettings: () => void;
  openAdminManagement: () => void;
  signOut: () => void;
};

const AuthSessionContext = createContext<AuthSession | null>(null);

export function AuthSessionProvider({ value, children }: { value: AuthSession; children: ReactNode }) {
  return <AuthSessionContext.Provider value={value}>{children}</AuthSessionContext.Provider>;
}

export function useOptionalAuthSession(): AuthSession | null {
  return useContext(AuthSessionContext);
}
