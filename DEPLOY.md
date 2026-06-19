# 部署

公共实例 = 一台一直在线的电脑（server）跑容器，数据库 `cnbib.db` 放**持久卷**上（跨发版存活）。本仓库已含 `Dockerfile` + `fly.toml`，本地 `docker` 已验证可跑、卷可持久。

## 为什么必须挂卷

平台（Fly.io / Railway）每次部署都用 Git 里的代码**现搭一个干净容器**——容器内文件系统是临时的。代码会从 Git 回来，但运行时长出来的 `cnbib.db`（众包/聚合攒的数据）会被清空。所以数据库必须放在**独立的持久卷**上，`CNBIB_DB` 指过去。

## Fly.io（推荐，配置已就绪）

```bash
# 1. 装 CLI 并登录（交互式，用你自己的账号；需要绑卡，免费额度够 MVP）
brew install flyctl
fly auth login

# 2. 创建 app（会读 fly.toml；把 fly.toml 里的 app 名改成你要的、全局唯一）
fly apps create wanzhou        # 或 fly launch --no-deploy 让它引导

# 3. 创建持久卷（和 fly.toml 里 source=cnbib_data、region 对应）
fly volumes create cnbib_data --region sjc --size 1   # 1GB，够几十万条

# 4. 配 Google Books API key（作为密钥，不写进代码/配置）
fly secrets set GOOGLE_BOOKS_API_KEY=你的key

# 5. 部署
fly deploy

# 6. 打开
fly open            # 浏览器打开公共 URL
fly logs           # 看日志
```

部署后访问 `https://<你的app>.fly.dev/`（搜索页）、`/docs`（API 文档）。

### 带种子数据上线
公共实例别空着上。**先在本地把 `cnbib.db` 用批量导入喂饱**，再把它放上卷：
```bash
# 本地挖好 cnbib.db 后，传到卷上（需要一台带卷的机器在跑）
fly ssh sftp shell
> put cnbib.db /data/cnbib.db
```
（或首版先空跑，之后用导入脚本远程灌。挖书脚本是下一步要写的。）

## Railway（备选，同一个 Dockerfile）

1. Railway 控制台 → New Project → Deploy from GitHub repo，选本仓库。它会用 `Dockerfile` 构建。
2. 项目里加一个 **Volume**，挂载到 `/data`。
3. Variables 里设 `CNBIB_DB=/data/cnbib.db` 和 `GOOGLE_BOOKS_API_KEY=...`。
4. 部署。Railway 现在基本要 Hobby 档（~$5/月）。

## 备份（持久卷也会坏，别只靠它）

定期把 `cnbib.db` 备份到对象存储（Cloudflare R2 / S3，免费额度够），留带时间戳的快照。最简单：一个每日 cron。

```bash
# 例：从 Fly 机器导出当前库（也可在容器里跑 sqlite3 .backup）
fly ssh console -C "cat /data/cnbib.db" > backup-$(date +%F).db
```

## 域名（可选）

一个 `.org` 约 $10-15/年。Fly：`fly certs add yourdomain.org`，再去域名商加 CNAME/A 记录。

## 大陆访问

美区实例大陆能连，但延迟高、偶有不稳。MVP 接受；等真有人抱怨慢，再考虑香港节点或 CDN。
