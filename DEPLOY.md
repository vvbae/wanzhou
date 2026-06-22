# 部署（Fly.io 香港）

万轴 = 容器(Dockerfile) + SQLite 库放**持久卷**。`fly.toml` 已配好：香港区 `hkg`、卷挂 `/data`、
`CNBIB_DB=/data/cnbib.db`、端口 8080。本地 `docker` 已验证可跑。

## 为什么必须挂卷
平台每次部署都用 Git 里的代码现搭干净容器，容器内文件系统是临时的。代码会回来，但运行时的
`cnbib.db` 会被清空。所以库放独立持久卷。

## 首次部署（香港）

```bash
# 1. CLI + 登录（交互，用你的账号；需绑卡，免费额度够）
brew install flyctl
fly auth login

# 2. 如果之前在美区(sjc)建过，先清掉旧机器和旧卷（区域绑死，不能迁）
fly machine list            # 记下旧机器 id → 删
fly machine destroy <id> --force
fly volumes list            # 记下旧(sjc)卷 id → 删
fly volumes destroy <id> --yes

# 3. 香港建卷（3GB：库 1.2G + WAL + 余量）
fly volumes create cnbib_data --region hkg --size 3 --yes

# 4. 密钥（Google 富化 + 审核口令；不写进代码）
fly secrets set GOOGLE_BOOKS_API_KEY=你的key CNBIB_ADMIN_TOKEN=你设的审核口令

# 5. 部署（单机，跟卷同在 hkg）
fly deploy --ha=false
fly open                    # 打开公共 URL（先是空库）
```

## 上传 1.2G 的库到卷

库太大、压缩后再传（SQLite 压得动）：
```bash
gzip -kf data/library.db                        # → data/library.db.gz
fly ssh sftp shell
> put data/library.db.gz /data/library.db.gz
> exit
fly ssh console -C "sh -c 'gunzip -f /data/library.db.gz && mv -f /data/library.db /data/cnbib.db'"
fly apps restart wanzhou                          # 重启加载新库
```
（也可先 `fly deploy` 空库验证站点在 hkg 跑通，再做这步灌库。）

## 之后
- 上线后建管理员：`fly ssh console -C "sh -c 'cd /app && uv run python make_admin.py 用户名 密码'"`
  （或本地对着卷里的库建好再传）。
- 备份：定期 `fly ssh console -C "cat /data/cnbib.db" > backup-$(date +%F).db`。
- 域名：`fly certs add yourdomain.org` + 域名商加记录。

## 大陆访问
香港区比美区延迟低、相对稳，但防火墙照样过滤。MVP 接受。
