import { create } from "zustand";
import { persist } from "zustand/middleware";

interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  userId: string | null;
  setTokens: (a: string, r: string, uid: string) => void;
  logout: () => void;
}

export const useAuth = create<AuthState>()(
  persist(
    (set) => ({
      accessToken: null,
      refreshToken: null,
      userId: null,
      setTokens: (a, r, uid) =>
        set({ accessToken: a, refreshToken: r, userId: uid }),
      logout: () => set({ accessToken: null, refreshToken: null, userId: null }),
    }),
    { name: "tmaster-auth" },
  ),
);
