import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 生产构建由后端挂载在 /app/ 下，资源路径必须带前缀；
// 开发服务器仍用根路径，保持 http://127.0.0.1:5175/ 的体验。
export default defineConfig(({ command }) => ({
  base: command === "build" ? "/app/" : "/",
  plugins: [vue()],
  server: {
    host: "127.0.0.1",
    port: 5175,
    strictPort: true,
    // 同源代理：浏览器只访问 5175，避免 CORS，也让前端不需要知道后端端口
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8010",
        changeOrigin: false
      }
    }
  }
}));
