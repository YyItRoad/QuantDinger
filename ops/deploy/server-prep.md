# 服务器一次性准备 runbook(SSH 部署目标)

这份文档只描述 **第一次** 把你的 Linux 服务器准备好,好让
`.github/workflows/deploy.yml` 能 SSH 上去、`docker compose pull && up` 把
backend 跑起来。

跑过一次之后,日常部署就只是到 fork 仓库的 **Actions 页面点一下
"Run workflow"**。

> 整份文档对应的是 `lscpu` 显示 **amd64 / GenuineIntel / Skylake IBRS**、
> `dpkg --print-architecture = amd64`(Debian/Ubuntu 系)的机器。其他发行版
> (CentOS/RHEL/Alma 等)把第 1 步换成对应的 `dnf` / `yum` 命令即可。

---

## 0. 你要做几次:一次

整份 runbook 是一次性开销。之后所有部署都从 GitHub Actions 触发。

---

## 1. 装 Docker + Compose v2

服务器上需要 Docker Engine 23+ 和 `docker compose`(v2 plugin)。

Debian/Ubuntu 默认源(最简单):

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# 注意:Ubuntu 24.04 / Debian 12 用 $VERSION_CODENAME,旧版发行为 bookworm/jammy 等
. /etc/os-release && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
```

> 你也可以用国内镜像(`download.docker.com` 国内某些云会慢),这里不展开。

装完验证:

```bash
docker --version
docker compose version
docker info | head -20
```

看到 `Server Version: 24.x.x` / `docker compose version 2.x` 就 OK。

---

## 2. 准备一个非 root 的 `deploy` 用户

GHCR pull 不需要 sudo,所以直接给用户 `docker` 组权限就够了:

```bash
sudo useradd -m -s /bin/bash -G docker deploy
sudo passwd -l deploy    # 关掉密码登录(SSH 走 key 认证)

sudo mkdir -p /opt/quantdinger
sudo chown deploy:deploy /opt/quantdinger
sudo chmod 755 /opt/quantdinger
```

切到 deploy 用户去配 SSH:

```bash
sudo -u deploy -i
```

---

## 3. 在 deploy 用户上生成 SSH key

```bash
ssh-keygen -t ed25519 -C "github-deploy" -N "" -f ~/.ssh/github_deploy
cat ~/.ssh/github_deploy.pub >> ~/.ssh/authorized_keys
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys ~/.ssh/github_deploy
chmod 644 ~/.ssh/github_deploy.pub
```

- `-N ""` 是空 passphrase,这样 GitHub Actions 才能用,这是必须的。
- **`.pub` 留在服务器**,**无 `.pub` 的私钥全文** 粘到 GitHub Secrets(下一步)。

退出 deploy 用户:

```bash
exit
```

---

## 4. 在 fork 仓库 Settings 配 Secrets

打开: `https://github.com/<你的 fork owner>/QuantDinger/settings/secrets/actions`

点 **New repository secret**,逐个添加:

| Secret name | 内容 |
|---|---|
| `DEPLOY_SSH_KEY` | `~/.ssh/github_deploy` **私钥**全文,包含 `-----BEGIN OPENSSH PRIVATE KEY-----` 和尾部换行 |
| `SERVER_HOST` | 服务器的 IP 或域名,例如 `1.2.3.4` 或 `quantdinger.example.com` |
| `SERVER_USER` | `deploy` |
| `SERVER_PORT` | `22`(默认;改 SSH 端口才需要) |

不需要 `GHCR_PAT` / `DOCKERHUB_TOKEN` 之类的——`GITHUB_TOKEN` 对你自己 fork owner
名下的 GHCR 默认就有 read+write,workflow 已经声明了 `packages: write`。

---

## 5. 首次 `backend.env`

`backend.env` 装着 backend 的运行时 secret。**workflow 永远不会覆盖它**,
它属于你:

```bash
sudo -u deploy bash
cd /opt/quantdinger
curl -fsSL "https://raw.githubusercontent.com/<你的 fork owner>/QuantDinger/main/backend_api_python/env.example" \
  -o backend.env

# 至少改这几个:
$EDITOR backend.env
#   ADMIN_USER=
#   ADMIN_PASSWORD=
#   SECRET_KEY=        # 跑一次会自己生成一个,但首次最好自己写一个强随机
#   DATABASE_URL=      # 如果你的 postgres 端口不默认,改这里
#   加上你用得到的 exchange / LLM API key

chmod 600 backend.env
```

> 漏掉 `SECRET_KEY` 也没事——backend 容器启动时会自动生成并写回 host 的
> `backend.env`(`docker-compose.ghcr.yml:175-178` 注释里有说明)。但手动写一个
> 更稳。

**重要**:如果 `backend.env` 是 **目录**,Docker bind-mount 会把它当目录挂到
`/app/.env` 上,导致容器读不到环境变量。`deploy-remote.sh` 里有防这种踩坑的
检查,但你自己拉下来**之后**不要 `mkdir backend.env`。

---

## 6. 拉 `docker-compose.ghcr.yml`(可选,workflow 也会拉)

```bash
cd /opt/quantdinger
curl -fsSL "https://raw.githubusercontent.com/<你的 fork owner>/QuantDinger/main/docker-compose.ghcr.yml" \
  -o docker-compose.ghcr.yml
```

或者你什么都不干,workflow 第一次跑时会自动拉。

---

## 7. 告诉服务器拉你的 fork 镜像

服务器上的 compose 默认认为 backend 镜像在 `ghcr.io/openbyteinc/quantdinger-backend`。
**因为你改了 backend 代码,要让服务器拉你 fork 出来的镜像**,写一个 `.env`:

```bash
cd /opt/quantdinger
touch .env

# 把这两行写进去(把 owner 换成你 fork 的 GitHub 用户名):
cat >> .env <<'EOF'
BACKEND_IMAGE=ghcr.io/<你的 fork owner>/quantdinger-backend
# IMAGE_TAG 和 BACKEND_TAG 由 deploy.yml 自动写,不需要手填
EOF

chmod 600 .env
```

> `BACKEND_IMAGE` 不带 tag,tag 由 `IMAGE_TAG` / `BACKEND_TAG` 决定,workflow 会写。

---

## 8. (可选,改了前端才需要)前端镜像地址

你说你 fork 了 `QuantDinger-Vue` 并改了前端。本 workflow 不管 frontend,但
服务器侧 compose 默认拉 `ghcr.io/openbyteinc/quantdinger-frontend`。

在你 fork 的 `QuantDinger-Vue` repo 里跑它自己的 `release-frontend.yml`,推到
你自己的 GHCR,然后在 `/opt/quantdinger/.env` 追加:

```bash
echo "FRONTEND_IMAGE=ghcr.io/<你的 vue fork owner>/quantdinger-frontend" \
  >> /opt/quantdinger/.env
```

下次 `docker compose up -d frontend` 时就会拉你的前端。

---

## 9. 首次启动整个 stack(只启 postgres / redis / migration 这些基础服务)

backend / worker 留到 GitHub Actions 来起。先把基础服务拉起来:

```bash
cd /opt/quantdinger
sudo -u deploy docker compose -f docker-compose.ghcr.yml up -d \
  postgres redis redis-jobs migration

# 看 health
sudo -u deploy docker compose -f docker-compose.ghcr.yml ps
```

等 `migration` 状态 `Exited (0)`、postgres / redis / redis-jobs 显示 `healthy`,
就能让 deploy workflow 接手了。

---

## 10. 第一次 GitHub Actions Run workflow 演练

打开 fork repo → **Actions** → 选 **Build & deploy backend to personal server**
→ **Run workflow** → 选 `main` branch:

- `image_tag`: **留空**(自动 `manual-<short-sha>`)
- `host`: **留空**
- `deploy_dir`: **留空**(默认 `/opt/quantdinger`)
- `prune`: 不勾
- `dry_run`: ✅ **勾上**

点绿色按钮。

期望:
- **Build & push backend image** 步骤绿(7~15 分钟,视 GHA runner 网络)。
- **Confirm image is reachable** 步骤绿(说明 GHCR 上有 `manual-xxxxxxx` 这个 tag)。
- workflow 提早结束,**SSH 没有跑**。

成功后,在 https://github.com/<你的 fork owner>?tab=packages 能看到
`quantdinger-backend / manual-xxxxxxx` 这个包。

---

## 11. 真实部署

第二次 Run workflow,这次 `dry_run` **不勾**。SSH 现在上场。

跑完后:

```bash
sudo -u deploy docker compose -f /opt/quantdinger/docker-compose.ghcr.yml ps
```

应看到 `quantdinger-backend` / `quantdinger-trading-worker` /
`quantdinger-scheduler-worker` / `quantdinger-celery-worker` /
`quantdinger-celery-beat` 都 `Up`,时间是你刚才点 Run 之后没几分钟内。

浏览器访问 `http://<SERVER_IP>:8888` 应该看到前端(假设你已经发了 Vue 镜像)。

---

## 故障兜底

- **SSH connect timeout**:多半是 `SERVER_HOST` 写错、服务器防火墙挡了 `22`、
  或者 `SERVER_PORT` 跟 sshd 配置对不上。先 `ssh deploy@<host>`(你的开发机)
  通一下。
- **`Permission denied (publickey)`**:`DEPLOY_SSH_KEY` 粘错(漏换行)、或
  `~/.ssh/authorized_keys` 没那把 pubkey。
- **`backend.env is a directory`**:`rmdir /opt/quantdinger/backend.env` 后
  workflow 会自动 `touch` 一个。
- **`manifest unknown`**:你 `image_tag` 输入了一个 GHCR 上没推过的 tag。
  留空让 workflow 自动算。
- **`/api/health 200` 但前端空白**:多半是 `BACKEND_IMAGE` 没改成你 fork owner,
  或者 frontend 镜像还没推到你的 GHCR。

---

## 关键文件 map(给以后自己看的备忘)

| 文件 | 谁管 |
|---|---|
| `/opt/quantdinger/backend.env` | **你手动维护**,workflow 永不写 |
| `/opt/quantdinger/.env` | workflow 写 `IMAGE_TAG` / `BACKEND_TAG`,你写 `BACKEND_IMAGE` / `FRONTEND_IMAGE` 等 |
| `/opt/quantdinger/docker-compose.ghcr.yml` | workflow 自动下载;你修改可永久保留 |
| `~/.ssh/github_deploy` | 私钥,**永远不要上传到任何仓库** |
| `~/.ssh/github_deploy.pub` | 公钥,留在服务器 `~/.ssh/authorized_keys` |
