import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  // 使用相对静态资源路径，避免部署到任意 /os/{group}/{name}/ 子路径时丢失前缀。
  // 这样 index.html、JS、CSS 都会相对于当前页面地址加载。
  base: './',
  plugins: [vue()],
  server: {
    host: '0.0.0.0',
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:5000',
        changeOrigin: true
      },
      '/files': {
        target: 'http://localhost:5000',
        changeOrigin: true
      }
    }
  }
})
