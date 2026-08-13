import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    env: {
      VITE_PDF2MD_SESSION_TOKEN: "test-session-token-0123456789abcdef0123456789abcdef",
    },
  },
});
