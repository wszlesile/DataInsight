# 后端 Docker 镜像说明

## 构建镜像

在项目根目录执行：

```powershell
docker build -f docker/Dockerfile.backend -t data-insight-backend:latest .
```

## 运行容器

建议通过运行时环境变量注入配置，而不是依赖镜像内的 `src/.env`。

### 方案一：外置数据库版

适用于测试环境、生产环境，或者本地联调但数据库不使用 SQLite 的场景。

```powershell
docker run -d ^
  --name data-insight-backend ^
  -p 5000:5000 ^
  --env-file docker/backend.env.example ^
  -v data-insight-temp:/app/temp ^
  -v data-insight-uploads:/app/uploads ^
  -v data-insight-logs:/app/logs ^
  data-insight-backend:latest
```

如果数据库在宿主机本机，记得把 `backend.env.example` 里的连接地址改成宿主机可访问地址。

### 方案二：本地 Docker + SQLite 版

适用于本地调试镜像，并复用宿主机已有的 SQLite 数据库文件。

```powershell
docker run -d ^
  --name data-insight-backend-local ^
  -p 5000:5000 ^
  --env-file docker/backend.env.sqlite.example ^
  -v D:\PycharmProjects\DataInsight\data_insight.db:/app/data/data_insight.db ^
  -v D:\PycharmProjects\DataInsight\temp:/app/temp ^
  -v D:\PycharmProjects\DataInsight\uploads:/app/uploads ^
  -v D:\PycharmProjects\DataInsight\logs:/app/logs ^
  data-insight-backend:latest
```

## 说明

- 容器内部使用 `gunicorn + gthread` 启动后端服务。
- `TEMP_DIR` 默认目录是 `/app/temp`。
- `UPLOAD_DIR` 默认目录是 `/app/uploads`。
- `LOG_DIR` 默认目录是 `/app/logs`。
- [src/agent/sys_prompt.md](D:/PycharmProjects/DataInsight/src/agent/sys_prompt.md) 属于 Agent 源码的一部分，会随着 `src` 目录一起复制进镜像。
- `docker/backend.env.example` 用于外置数据库场景。
- `docker/backend.env.sqlite.example` 用于本地 Docker 调试 SQLite 场景。
- 如果容器内使用 SQLite，`SQLITE_PATH` 必须写成容器内路径，宿主机真实数据库文件通过 `-v` 挂载进去。
- 如果 SQLite 里保存了 Windows 本地文件路径类数据源，容器内未必能直接访问这些路径；这类数据源是否可用，需要看你是否也把对应目录挂载进容器，并且路径解释逻辑是否一致。
