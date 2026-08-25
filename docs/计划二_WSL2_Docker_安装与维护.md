# 计划二｜WSL2 Docker 安装与维护

> 本说明只针对本机开发环境，不改变项目业务架构。环境完成日期：2026-08-25。

> **Docker 操作入口：** 先执行第 3 节预检并复用 Compose 资源。项目 PostgreSQL
> 已有永久容器和 named volume，日常启动不新建独立 PostgreSQL 容器。

## 1. 已完成的环境

- Windows 10 + WSL 2.6.3，发行版为 `Ubuntu-22.04`，WSL 版本为 2。
- Ubuntu 已启用 systemd：`/etc/wsl.conf` 中有 `[boot] systemd=true`。
- 已安装 Docker 官方 Engine/Compose Plugin：
  - Docker Engine `28.4.0`
  - Docker Compose Plugin `v2.39.4`
  - containerd `1.7.28`
- `docker.service` 和 `containerd.service` 已启用，当前 Docker 通过 Unix socket
  `/var/run/docker.sock` 提供服务。
- 当前用户 `sehor` 已加入 `docker` 组。该组具有近似 root 的权限，只应加入可信用户。
- `/etc/docker/daemon.json` 已移除原来的 `tcp://0.0.0.0:2375`，当前只配置本地日志驱动：

  ```json
  {"log-driver": "local"}
  ```

  原配置备份在 WSL 内：`/etc/docker/daemon.json.bak-20260825`。
- Windows 用户配置 `C:\Users\pzr\.wslconfig` 使用 `dnsProxy=false`，让 WSL NAT 使用 Windows 当前 DNS；这是修复本机 `172.24.96.1:53` DNS 超时所需的设置。

## 2. 关系和调用方式

```text
Windows
  └─ WSL2 Ubuntu-22.04
       ├─ systemd
       │    ├─ docker.service
       │    └─ containerd.service
       └─ Docker Engine
            ├─ images / containers / networks / volumes
            └─ project Compose services
```

Windows 当前没有 Docker CLI，也没有安装或依赖 Docker Desktop。统一从 WSL 调用 Docker：

```powershell
wsl.exe -d Ubuntu-22.04 -- docker compose `
  --project-directory /mnt/e/projects/video-maker `
  -f /mnt/e/projects/video-maker/compose.yaml ps -a
```

也可以从 Windows PowerShell 直接执行：

```powershell
wsl.exe -d Ubuntu-22.04 -- bash -lc "cd /mnt/e/projects/video-maker && docker compose ps"
```

不要设置 `DOCKER_HOST=tcp://...:2375`，不要运行 `dockerd -H tcp://0.0.0.0:2375`。

项目 Compose 的端口边界如下：

| 服务 | 容器端口 | Windows 访问 | 说明 |
|---|---:|---:|---|
| `postgres` | 5432 | 不映射 | 只在 Compose 内网供 API/Web 使用 |
| `api` | 8000 | `http://localhost:8000` | Compose 启动完整栈时映射 |
| `web` | 3000 | `http://localhost:3000` | Compose 启动完整栈时映射 |

## 3. 项目永久资源与启动预检

截至 2026-08-25，项目 PostgreSQL 基线如下：

| 项目 | 当前值 |
|---|---|
| Compose project | `video-maker` |
| Compose service | `postgres` |
| 容器名 | `video-maker-postgres-1` |
| 镜像 | `postgres:17.6-alpine` |
| restart policy | `unless-stopped` |
| named volume | `video-maker_postgres-data` |
| 数据挂载 | `/var/lib/postgresql/data` |
| 数据库／用户 | `video_factory`／`video_factory` |
| Alembic 版本 | `0002_core_domain_contract` |

每次 Docker 或数据库任务按以下顺序执行：

```powershell
# 1. 确认发行版；Docker 位于 WSL2，不在 Windows PATH
wsl.exe --list --verbose

# 2. 先看现有容器，包括 stopped 状态
wsl.exe -d Ubuntu-22.04 -- docker compose `
  --project-directory /mnt/e/projects/video-maker `
  -f /mnt/e/projects/video-maker/compose.yaml ps -a

# 3. 现有 postgres 若为 stopped，只启动原容器
wsl.exe -d Ubuntu-22.04 -- docker compose `
  --project-directory /mnt/e/projects/video-maker `
  -f /mnt/e/projects/video-maker/compose.yaml start postgres

# 4. 验证健康和迁移版本
wsl.exe -d Ubuntu-22.04 -- docker exec video-maker-postgres-1 `
  pg_isready -U video_factory -d video_factory
wsl.exe -d Ubuntu-22.04 -- docker exec video-maker-postgres-1 `
  psql -U video_factory -d video_factory -Atc "SELECT version_num FROM alembic_version"
```

`restart: unless-stopped` 会在 WSL2/Docker daemon 再次启动时恢复 PostgreSQL。容器确实不存在或
`compose.yaml` 已变更时，才执行：

```powershell
wsl.exe -d Ubuntu-22.04 -- docker compose `
  --project-directory /mnt/e/projects/video-maker `
  -f /mnt/e/projects/video-maker/compose.yaml up -d postgres
```

项目 PostgreSQL 始终由 Compose service `postgres` 管理。不要用 `docker run postgres...`
另建项目数据库容器；测试迁移优先在现有 PostgreSQL 内创建隔离测试数据库。

## 4. 数据在哪里

- 项目代码仍在 Windows `E:\projects\video-maker`，WSL 路径为 `/mnt/e/projects/video-maker`。
- Docker Engine 的镜像、容器层、网络和 named volume 保存在 WSL 的 Docker root：`/var/lib/docker`，实际位于 Ubuntu 的 WSL 虚拟磁盘中。
- 项目 PostgreSQL volume 为 `video-maker_postgres-data`，挂载点为 `/var/lib/docker/volumes/video-maker_postgres-data/_data`。
- `local-storage` 和 `web-node-modules` 也是 Compose named volume，不在 E: 代码目录中。
- 将数据库和上传/结果数据放在 WSL named volume；不要把 PostgreSQL 数据目录 bind mount 到 `/mnt/e`。

代码放在 E: 便于 Windows 工具访问，当前项目可正常运行。若后续大量 Linux 文件扫描、依赖安装或热重载出现性能问题，再考虑把完整工作树迁移到 `~/src/video-maker`；这不是本次环境配置的必要条件。

## 5. 常用操作

```bash
# 进入发行版
wsl -d Ubuntu-22.04

# 服务状态
systemctl status docker --no-pager
systemctl is-active docker
systemctl is-enabled docker

# 基础验证
docker info
docker compose version
docker run --rm hello-world

# 项目开发
cd /mnt/e/projects/video-maker
docker compose ps -a
docker compose start postgres       # 日常恢复现有 PostgreSQL
docker compose up -d                # 需要完整栈时创建/启动缺失服务
docker compose up -d --build api web # 仅 Dockerfile/依赖变化时重建
docker compose logs -f postgres
docker compose stop                 # 停止并保留现有容器
docker compose down                 # 删除容器，保留 named volumes
docker compose down -v              # 删除容器和项目数据，禁止用于日常操作
```

日常暂停使用 `docker compose stop`，日常恢复使用 `docker compose start`。`down`、`rm`
和独立 `docker run` 都不是日常启动流程。

## 6. 重启和故障排查

### WSL 重启后 Docker 没有起来

```powershell
wsl --shutdown
wsl -d Ubuntu-22.04 -- systemctl is-system-running
wsl -d Ubuntu-22.04 -- systemctl is-active docker
wsl -d Ubuntu-22.04 -- docker info
```

若状态仍为 `activating`，等待几秒后再检查；若为 `failed`：

```bash
systemctl status docker --no-pager -l
journalctl -u docker.service -b --no-pager -n 100
cat /etc/docker/daemon.json
```

`daemon.json` 不得配置 `hosts`；systemd unit 已通过 `-H fd://` 管理本地 socket。

### Docker Hub 拉取失败或 DNS 超时

```bash
cat /etc/resolv.conf
getent ahostsv4 registry-1.docker.io
docker pull hello-world
```

Windows 10 上本机使用 `C:\Users\pzr\.wslconfig` 的 `dnsProxy=false`。如果 `/etc/resolv.conf` 又只出现失效的 `172.24.96.1`，执行 `wsl --shutdown` 后重试，并检查 `.wslconfig` 是否仍存在。不要把失效的代理地址写进 Docker daemon 配置。

### 检查是否错误开放 Docker TCP

```bash
ss -lntp
```

正常情况下只应看到业务显式映射的端口；Docker API 应通过 Unix socket 工作。Windows 侧也可检查：

```powershell
netstat -ano | Select-String ':2375|:2376'
```

没有输出才符合本机安全要求。业务端口应只按 Compose 显式配置映射，并优先绑定到 localhost。

### PostgreSQL 数据检查

```bash
cd /mnt/e/projects/video-maker
docker compose ps postgres
docker compose exec -T postgres pg_isready -U video_factory -d video_factory
docker volume inspect video-maker_postgres-data
docker compose exec -T postgres psql -U video_factory -d video_factory \
  -Atc "SELECT version_num FROM alembic_version"
```

不要使用 `docker compose down -v`，除非明确要删除本地数据库和其他项目 volume。备份或迁移前先停止写入并导出数据库。

### API 镜像构建遇到 `.pytest_cache` 权限错误

`apps/api/.dockerignore` 已排除 `.venv`、`.pytest_cache`、测试数据库和本地数据目录。
若旧缓存仍导致 `failed to xattr ... .pytest_cache: permission denied`，这是 Windows
构建上下文 ACL 问题，不是 PostgreSQL 故障。保留现有 `postgres` 容器和 volume，修复或清理
该缓存后再构建 API；不要通过另建 PostgreSQL 容器绕过问题。

## 7. 已验证结果

- `docker run --rm hello-world` 成功。
- `docker compose version` 返回 `v2.39.4`。
- PostgreSQL `17.6-alpine` 通过项目 Compose 启动并变为 healthy。
- 永久容器为 `video-maker-postgres-1`，使用 `video-maker_postgres-data`，重启策略为
  `unless-stopped`；WSL2 重新唤醒后可自动恢复。
- 项目数据库已迁移到 `0002_core_domain_contract`，核心表和关键约束已在 PostgreSQL 17.6
  中核对。
- 数据写入 PostgreSQL 后重启容器仍可读出，证明 named volume 持久化有效。
- 临时 Nginx 绑定 `127.0.0.1:18080` 后，Windows `http://localhost:18080` 返回 HTTP 200。
- WSL 内没有 `2375` 监听，Docker context 为 `default`，endpoint 为 `unix:///var/run/docker.sock`。
- `wsl --shutdown` 后 systemd、Docker 和 PostgreSQL 可恢复；完整项目栈按需执行
  `docker compose up -d`，仅依赖或 Dockerfile 变化时使用 `--build`。
