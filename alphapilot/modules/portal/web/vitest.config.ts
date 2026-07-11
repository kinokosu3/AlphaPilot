import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
    coverage: {
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/**/*.test.{ts,tsx}", "src/**/types.ts", "src/**/*.d.ts", "src/main.tsx"],
      reportsDirectory: "../../../../git_ignore_folder/qa/portal_interaction/coverage",
      thresholds: { lines: 70 },
    },
  },
});
