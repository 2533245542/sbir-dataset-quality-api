# Dataset Quality API: GitHub + Render 部署手册

本文档说明如何把当前 FastAPI + SQLite 项目部署为公开 API。推荐架构是：

```text
本地生成 SQLite 数据库
          ↓
GitHub: <your-account>/sbir-dataset-quality-api
branch: main
          ↓ Render 自动构建 Docker image
Render Web Service
          ↓
https://<service-name>.onrender.com
```

Render 运行的是整个 Docker 镜像。SQLite 数据库是镜像中的只读文件，所以 Render 的临时文件系统不会造成数据丢失。

## 最简单的做法：生成独立的个人部署仓库

为了不影响 `BIDS-Xu-Lab/sbir-project-api`，推荐用脚本生成一个完全独立的 Render 部署目录：

```bash
cd /Users/wz426/Desktop/sbir/imm_dataset_quality/absa_preprocess/api/sbir-project-api-wz
chmod +x prepare_render_repo.sh
./prepare_render_repo.sh
```

默认输出目录是：

```text
/Users/wz426/Desktop/sbir/imm_dataset_quality/absa_preprocess/api/sbir-dataset-quality-api
```

脚本会自动：

- 对 SQLite 数据库执行 integrity check。
- 压缩 `api.db`，并将压缩文件拆成小于 GitHub 100 MiB 限制的 `api.db.gz.part-*` 分片。
- 验证全部分片能够重新组合，并逐字节还原原始数据库。
- 复制 `quality_api.py` 和 `requirements.txt`。
- 生成 Render 专用 Dockerfile、`.dockerignore` 和 `.gitignore`。
- 生成 `render.yaml`，让 Render Blueprint 自动配置 Free Web Service。
- 不会自动 commit、push 或访问 Render。

如果你已经 clone 了个人 GitHub repository，可以直接将其路径传给脚本：

```bash
./prepare_render_repo.sh /path/to/my-personal-repo
```

重新生成时，如果目标仓库的受管文件已有人工修改，脚本会停止而不是直接覆盖。检查变更后可以显式更新：

```bash
./prepare_render_repo.sh --force /path/to/my-personal-repo
```

## 0. 部署前要知道的事

- 当前开发仓库仍然保持不变；测试部署使用脚本生成的独立个人仓库。
- 原始数据库在 `../api_preprocess/output/api.db`；具体大小会随 pipeline 更新。
- 压缩数据库会被拆成若干个不超过 90 MiB 的分片，可以通过普通 Git 提交，不需要 Git LFS。
- API 会公开数据、Swagger docs 和搜索接口。上线前应确认数据中没有不能公开的内容。
- 当前 API 没有 API key、用户登录或应用内限流。对于 demo 没问题，但不应直接当作有 SLA 的生产服务。

## 1. 手动准备流程（使用脚本时可跳过）

以下命令都从项目根目录运行：

```bash
cd /Users/wz426/Desktop/sbir/imm_dataset_quality/absa_preprocess/api/sbir-project-api-wz
```

### 1.1 检查数据库

```bash
sqlite3 ../api_preprocess/output/api.db "PRAGMA integrity_check;"
```

预期输出：

```text
ok
```

### 1.2 生成并拆分压缩数据库

```bash
gzip -9 -c ../api_preprocess/output/api.db > /tmp/api.db.gz
split -b 90m /tmp/api.db.gz api.db.gz.part-
ls -lh api.db.gz.part-*
```

不要把原始的 `api.db` 或完整的 `api.db.gz` 复制进 GitHub repository；单个文件可能超过 GitHub 普通 Git object 的 100 MiB 上限。通常应直接运行准备脚本，由脚本完成拆分及还原验证。

### 1.3 修改 Dockerfile

将 `Dockerfile` 替换为：

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY quality_api.py .
COPY api.db.gz.part-* /tmp/db-parts/
RUN cat /tmp/db-parts/api.db.gz.part-* | gzip -dc > /app/api.db \
    && rm -f /tmp/db-parts/api.db.gz.part-* \
    && rmdir /tmp/db-parts

ENV DB_PATH=/app/api.db
ENV PYTHONUNBUFFERED=1

EXPOSE 10000

CMD ["sh", "-c", "exec uvicorn quality_api:app --host 0.0.0.0 --port ${PORT:-10000}"]
```

这里不能继续固定使用 Hugging Face Space 的 `7860` 端口。Render 会通过 `PORT` 环境变量告诉应用应该监听哪个端口。

### 1.4 添加 .dockerignore

创建 `.dockerignore`：

```dockerignore
.git
.gitignore
.venv
__pycache__
*.py[cod]
.env
.DS_Store
data/
```

不要在 `.dockerignore` 中忽略 `api.db.gz.part-*`。

### 1.5 确认 Python dependencies

`requirements.txt` 至少需要：

```text
fastapi>=0.115
uvicorn[standard]>=0.32
rapidfuzz>=3.9
```

`quality_api.py` 是当前 Render 部署的入口，不是 `main.py`。

## 2. 本地构建和验证（可选）

需要先安装并启动 Docker Desktop。

```bash
docker build -t sbir-dataset-quality-api .
docker run --rm --name sbir-dataset-quality-api \
  -e PORT=10000 \
  -p 10000:10000 \
  sbir-dataset-quality-api
```

保持容器运行，在另一个 terminal 中测试：

```bash
curl -i http://127.0.0.1:10000/
curl -i "http://127.0.0.1:10000/datasets/search?q=ImmPort&top_n=1"
open http://127.0.0.1:10000/docs
```

至少确认：

- `/` 返回 HTTP 200，并显示 dataset 数量。
- `/docs` 能显示 Swagger UI。
- `ImmPort` 搜索返回正常 JSON。
- Docker log 中没有 `Database not found` 或 out-of-memory 错误。

测试完成后，在运行容器的 terminal 中按 `Ctrl-C`。

## 3. 提交并 push 到个人 GitHub repository

进入脚本生成的独立目录。如果使用默认目录：

```bash
cd /Users/wz426/Desktop/sbir/imm_dataset_quality/absa_preprocess/api/sbir-dataset-quality-api
```

先在 GitHub 个人账号中创建一个空 repository，例如 `sbir-dataset-quality-api`。不要预先添加 README、`.gitignore` 或 license，然后使用 GitHub 页面显示的 repository URL：

```bash
git init -b main
git add Dockerfile .dockerignore .gitignore render.yaml \
  requirements.txt quality_api.py api.db.gz.part-* DEPLOY.md
git status --short
git commit -m "Deploy dataset quality API on Render"
git remote add origin https://github.com/<your-account>/sbir-dataset-quality-api.git
git push -u origin main
```

如果是先 clone 个人 repository、再把路径传给脚本，则不需要 `git init` 和 `git remote add`，只需要：

```bash
git add Dockerfile .dockerignore .gitignore render.yaml \
  requirements.txt quality_api.py api.db.gz.part-* DEPLOY.md
git status --short
git commit -m "Deploy dataset quality API on Render"
git push -u origin main
```

如果 GitHub 拒绝 push，检查：

- 当前 GitHub 用户是否登录，并且对这个个人 repository 有 write permission。
- Git credential 或 SSH key 是否有效。
- 每个 `api.db.gz.part-*` 是否确实小于 100 MiB。
- 是否误加了未压缩的 `api.db`。

GitHub 上最终应能在 `main` 分支看到 Dockerfile、`render.yaml`、`quality_api.py` 和全部 `api.db.gz.part-*` 分片。

## 4. 在 Render 使用 Blueprint 创建 Web Service

1. 打开 [Render Dashboard](https://dashboard.render.com/)，使用 GitHub 登录。
2. 选择 **New > Blueprint**。
3. 授权 Render 访问你的个人 `sbir-dataset-quality-api` repository。
4. 选中该 repository，Render 会自动读取根目录的 `render.yaml`。
5. 检查 Blueprint 提出的配置。

| Render 字段 | 建议值 |
|---|---|
| Name | `sbir-dataset-quality-api` |
| Region | 选择离主要用户最近的区域 |
| Branch | `main` |
| Root Directory | 留空 |
| Language / Runtime | `Docker` |
| Dockerfile Path | `./Dockerfile` |
| Instance Type | 先选 `Free` |
| Health Check Path | `/` |
| Auto-Deploy | `Yes` |

6. 不需要创建 Render Postgres，也不需要挂载 persistent disk。
7. 不需要手动设置 `DB_PATH`；Dockerfile 已经把它设置为 `/app/api.db`。
8. 确认 plan 为 `Free`，然后创建 Blueprint，等待 build 和 deploy 完成。

Render 的构建日志应当依次显示：

```text
pip install requirements
copy, combine, and decompress api.db.gz.part-*
start uvicorn
Loaded 87612 dataset names from /app/api.db
```

dataset 数量会随新数据库变化，不一定永远是 `87612`。

## 5. 上线后验证

假设 Render 生成的 URL 是：

```text
https://sbir-dataset-quality-api.onrender.com
```

执行：

```bash
curl -i https://sbir-dataset-quality-api.onrender.com/
curl -i "https://sbir-dataset-quality-api.onrender.com/datasets/search?q=ImmPort&top_n=1"
curl -i "https://sbir-dataset-quality-api.onrender.com/datasets/search?q=GSE131907&top_n=1"
```

在浏览器打开：

```text
https://sbir-dataset-quality-api.onrender.com/docs
```

验证清单：

- 根路由返回 HTTP 200。
- Swagger UI 正常显示所有 endpoint。
- dataset 精确搜索和 fuzzy search 都有结果。
- dataset profile、papers 和 mentions 接口都能打开。
- Render Logs 中没有 500、database locked 或 memory error。

## 6. Free 和付费版如何选

Render Free 适合先上线验证：

- 512 MB RAM，0.1 CPU。
- 15 分钟没有 inbound traffic 后休眠。
- 休眠后第一个请求可能需要等待约一分钟。
- 每个 workspace 每月有 750 Free instance hours。

如果遇到以下情况，再在 Render Settings 中将 Instance Type 升级为付费规格：

- 不能接受冷启动。
- 进程因为超过 512 MB 而被终止。
- fuzzy search 延迟过高。
- API 已经被稳定的前端或外部用户使用。

付费升级不需要改代码、URL 或数据库；Render 会重新部署同一个 Docker image。开通前以 Render Dashboard 显示的当前价格为准。

## 7. 后续更新代码或数据

更新代码后：

```bash
git add quality_api.py requirements.txt Dockerfile
git commit -m "Update dataset quality API"
git push origin main
```

Render 会自动重新构建和部署。

数据库重新生成后：

```bash
cd /Users/wz426/Desktop/sbir/imm_dataset_quality/absa_preprocess/api/sbir-project-api-wz
./prepare_render_repo.sh --force /path/to/my-personal-repo
cd /path/to/my-personal-repo
git add api.db.gz.part-*
git commit -m "Update dataset quality database"
git push origin main
```

每次更新 gzip 文件都会增加 Git repository history 大小。如果以后频繁更新数据库，应将数据库改为从 Hugging Face Dataset repository、GitHub Release 或 object storage 下载，而不是每个版本都存进 Git history。

## 8. 常见错误

### `Database not found: /app/api.db`

检查：

- GitHub 分支中是否有完整的一组 `api.db.gz.part-*` 分片。
- Dockerfile 是否按文件名顺序拼接并解压全部分片。
- `DB_PATH` 是否是 `/app/api.db`。

### Render 报 `No open ports detected`

检查 Uvicorn 启动命令是否同时满足：

```text
--host 0.0.0.0
--port ${PORT:-10000}
```

### GitHub 拒绝大文件

不要提交 `api.db` 或完整的 `api.db.gz`。只提交每个均小于 100 MiB 的 `api.db.gz.part-*` 分片。

### Render 启动后马上终止

先在 Logs 中查看是否是 dependency error、database error 或 out of memory。如果是 OOM，收集峰值内存证据后升级实例，不要为了规避限制删掉数据库完整性检查。

### Render 中找不到 GitHub repository

在 GitHub 的 **Settings > Applications > Installed GitHub Apps** 中检查 Render 的 repository access。对 organization repository，可能需要 organization owner 批准。

## 9. 官方参考

- [Render: Deploy a FastAPI App](https://render.com/docs/deploy-fastapi)
- [Render: Docker deployment](https://render.com/docs/docker)
- [Render: Web Services and port binding](https://render.com/docs/web-services)
- [Render: Free service limitations](https://render.com/docs/free)
- [GitHub: Repository file limits](https://docs.github.com/en/repositories/creating-and-managing-repositories/repository-limits)
