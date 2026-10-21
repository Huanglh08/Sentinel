import { create } from "zustand";

import { storage } from "@/utils/storage";

export type ThemeMode = "light" | "dark";

interface ThemeState {
  theme: ThemeMode;
  setTheme: (theme: ThemeMode) => void;
  toggleTheme: () => void;
  initialize: () => void;
}

function applyTheme(theme: ThemeMode) {
  document.documentElement.classList.toggle("dark", theme === "dark");
}

export const useThemeStore = create<ThemeState>((set, get) => ({
  // 界面为常驻深空底设计（.sentinel-app #04070F），默认必须 dark：
  // 正文/角标/悬浮卡（含 portal 到 body 的 Tooltip）都依赖 dark: 变体，light 口径的 #333 文字在深底上不可读
  theme: "dark",
  setTheme: (theme) => {
    storage.setTheme(theme);
    applyTheme(theme);
    set({ theme });
  },
  toggleTheme: () => {
    const next = get().theme === "light" ? "dark" : "light";
    get().setTheme(next);
  },
  initialize: () => {
    const stored = storage.getTheme();
    const theme = stored === "light" ? "light" : "dark";
    applyTheme(theme);
    set({ theme });
  }
}));
