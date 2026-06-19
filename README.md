# 中文开放书目 API

免费、开放的中文书目查询 API。给 ISBN，返回干净的中文书元数据（书名、作者、译者、
出版社、封面、简介）。数据 CC0，代码开源。

> 当前进度：**Phase 3 —— 极简 UI**。只读 API + 众包 `/contribute` + 网页（搜索页 / 详情页 / 贡献页）都已可用。

## 技术栈
Python 3.12+ · FastAPI · SQLite + FTS5 · httpx · Pydantic · uv

## 本地起服务

```bash
uv sync                                  # 装依赖（含开发依赖）

# 可选：配 Google Books API key（不配会被 Google 限流 429，OpenLibrary 仍可用）
export GOOGLE_BOOKS_API_KEY=你的key

uv run uvicorn cnbib.api:app --reload    # 起服务，默认 http://127.0.0.1:8000
```

起来后：

- **网页（手机可用）**：
  - `GET /` 搜索/首页 —— 关键词或 ISBN 搜索、随机一本
  - `GET /book?isbn=…` 详情页 —— 所有字段 + **每字段来源徽章**（人工/OpenLibrary/Google）+ ⚑ 待补中文
  - `GET /edit?isbn=…` 贡献页 —— 输入或扫 ISBN → 预填 → 改 → 提交
- 文档（Swagger UI）：http://127.0.0.1:8000/docs
- 试一下（首次会实时聚合外部源并写回库，第二次走缓存）：
  ```bash
  curl http://127.0.0.1:8000/books/9787208061644   # 追风筝的人
  curl --get http://127.0.0.1:8000/search --data-urlencode "q=三体"   # 中文需 URL 编码
  curl http://127.0.0.1:8000/books/random
  curl http://127.0.0.1:8000/stats
  # 众包补全/纠错（众包=最高优先级，覆盖外部源并记 edits 日志）
  curl -X POST http://127.0.0.1:8000/contribute -H 'Content-Type: application/json' \
    -d '{"isbn":"9787544253994","fields":{"title":"百年孤独","translators":"范晔"}}'
  ```

数据库文件默认是 `cnbib.db`（随查询自然增长）。换路径：`export CNBIB_DB=/path/to.db`。

## 跑测试

```bash
uv run pytest            # 全部
uv run pytest -q tests/test_aggregator.py tests/test_cleaning.py   # 核心逻辑
```

测试不联网（aggregator 用假源，清洗是纯函数）。

## 端点

| 端点 | 说明 |
|------|------|
| `GET /books/{isbn}` | 单本完整元数据 + 每字段来源；库里没有则实时聚合外部源后写回 |
| `GET /search?q=&page=&page_size=` | FTS5 全文搜索（书名/作者/出版社） |
| `GET /books/random` | 随机一本 |
| `GET /stats` | 总记录数、各来源字段数、最近新增 |
| `POST /contribute` | 众包补全/纠错：写 edits 日志、更新 books、改动字段来源标 `crowdsource`（最高优先级）。无需登录，contributor 用 IP/匿名标识 |
| `GET /` | 极简贡献网页（手机可用，输入或扫 ISBN → 预填 → 改 → 提交） |

## 架构

```
cnbib/
  isbn.py        ISBN 规范化（10↔13、校验位）
  cleaning.py    中文清洗：译者拆分 / 原作名 / 年份解析 / 语言代码规范化（纯函数，重点单测）
  sources/
    base.py      统一源接口 + 内部记录结构
    google_books.py
    openlibrary.py
  aggregator.py  并发查源 + 字段级合并（按优先级，核心逻辑，单测）
  db.py          SQLite + FTS5 读写
  schema.py      Pydantic 模型
  api.py         FastAPI 路由（薄层）+ POST /contribute + 贡献页
  static/
    search.html  首页/搜索页
    book.html    详情页（每字段来源徽章 + 待补中文标记）
    index.html   贡献页（手机优先，原生 BarcodeDetector 扫码，无外部依赖）
tests/           cleaning / aggregator / db / isbn / contribute 单测
probe.py         Phase 0 探针（字段覆盖率，历史产物）
```

字段合并优先级：`crowdsource`（人工，Phase 2）> `openlibrary` > `google_books`。
每个采用值记一条 `field_sources`，对外每字段附 `_source`。

## 部署

见 [DEPLOY.md](DEPLOY.md)。要点：容器化（`Dockerfile`），数据库 `cnbib.db` 放**持久卷**（`CNBIB_DB=/data/cnbib.db`），否则每次发版数据被清空。本地 `docker` 已验证镜像可跑、卷可持久。Fly.io 配置见 `fly.toml`。

## 开源协议

- **代码**：MIT（见 [LICENSE](LICENSE)）。
- **数据**：CC0（公共领域，随便用）。

## 已知问题 / 待确认

- **OpenLibrary 端点迁移**：设计文档写的 `/api/books?jscmd=data` 已被 OL 弃用（恒 500），
  adapter 已改用现行 `/isbn/{isbn}.json` + `/works` + `/authors`。
- **OL 数据质量**：标题/人名常是罗马拼音（`Bai nian gu du`、`Fan Ye`），不是汉字；
  work 级作者偶尔混入其它版本的译者（如《三体》混入法语版译者）。这正是众包要补的。
- **合并优先级 vs 标题质量**：按文档 OL 优先于 Google，但 OL 标题常是拼音、Google 是规范中文。
  标题/简介是否应改为 Google 优先，需产品确认（见 CLAUDE.md "改动前先确认"）。
