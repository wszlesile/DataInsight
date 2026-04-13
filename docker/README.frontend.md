# 前端 Docker 镜像说明

## 构建镜像

在项目根目录执行：

```powershell
docker build -f docker/Dockerfile.frontend -t data-insight-frontend:latest .
```

## 运行容器

前端镜像本身只托管静态资源。
`/api` 和 `/files` 由部署平台的统一入口网关转发到后端服务。

```powershell
docker run -d ^
  --name data-insight-frontend ^
  -p 3000:80 ^
  --env-file docker/frontend.env.example ^
  data-insight-frontend:latest
```

## 说明

- 前端镜像采用两阶段构建：
  - `node:20-alpine` 负责执行 `vite build`
  - `nginx:alpine` 负责托管构建后的静态资源
- 前端源码中的本地 `.env` 文件不会被打进镜像。
- 前端仍然使用相对路径 `/api`、`/files` 发请求，正式环境由平台网关负责同域转发。
- 前端请求头里的认证信息改为从浏览器 `localStorage.ticket` 读取，并自动补齐 `Bearer ` 前缀。
