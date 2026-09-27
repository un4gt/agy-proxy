import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  build: {
    outDir: "app/static",
    emptyOutDir: false,
    rollupOptions: {
      output: {
        entryFileNames: "assets/ui.js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: (asset) =>
          asset.names.some((n) => n.endsWith(".css"))
            ? "app.css"
            : "assets/[name][extname]",
      },
    },
  },
  server: {
    proxy: {
      "/signin": {
        target: "http://127.0.0.1:8317",
        bypass(req) {
          if (req.method === "GET") return "/index.html";
        },
      },
      ...Object.fromEntries(
        [
          "/dashboard",
          "/api",
          "/health",
          "/signout",
          "/login/",
          "/update/",
        ].map((p) => [p, "http://127.0.0.1:8317"]),
      ),
    },
  },
});
