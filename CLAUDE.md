# CLAUDE.md

本文件是给 Claude Code 的项目上下文。每次开始工作前先读这里，严格遵守边界。

## 项目是什么
一个免费、开放的**中文书目查询 API**。给 ISBN，返回干净的中文书元数据（书名、作者、译者、出版社、封面、简介）。数据 CC0，代码开源。

**不是** OpenLibrary 克隆。目标是一个人能维护的、开发者真的会用的小而干净的 API。

## 技术栈（已定，不要换）
- Python 3.12+，FastAPI
- SQLite + FTS5（不要上 Postgres / Elasticsearch）
- httpx 做异步 HTTP
- Pydantic 做数据模型
- 包管理用 uv

## 硬边界：v1 明确不做（不要主动实现这些）
- ❌ Work/Edition 归并抽象 —— v1 一个 ISBN 一条记录
- ❌ 用户系统 / 登录 / 权限
- ❌ 自建搜索引擎 —— 用 SQLite FTS5
- ❌ 封面图片自托管 —— 只存 URL
- ❌ 全文阅读 / 电子书
- ❌ 书评 / 书单 / 社交功能

如果某个需求会让"一个人周末做不完"，停下来问，不要擅自扩展。

## 数据模型（核心，不要随意改）

### books 表（主键 isbn_13）
isbn_13(PK), isbn_10, title, subtitle, authors(JSON), translators(JSON),
original_title, original_authors(JSON), publisher, publish_date, publish_year(INT),
description, cover_url, page_count, language, series, subjects(JSON), clc,
created_at, updated_at

### field_sources 表
记录每个字段的值来自哪个源：isbn_13, field_name, source, confidence, updated_at

### edits 表
众包修改日志：id(PK), isbn_13, field_name, old_value, new_value, contributor_hint, created_at

### FTS5
在 books 上建虚拟表，索引 title / authors / publisher。

## 中文书的特殊处理（重要，默认逻辑会错）
- **译者**：Google Books 经常把译者混进 authors。必须把译者拆出来放 `translators`，不要堆在 authors 里。
- **原作名**：译作要尽量提取 `original_title`。
- **外国作者名（两个都存）**：`authors` 存**中文译名**（加西亚·马尔克斯），`original_authors` 存**原文名**（Gabriel García Márquez）。外部 API 只给得出原文名，聚合时自动把译作的原文作者名填进 `original_authors`；`authors` 里若仍是拉丁字母，标记为待补中文，靠众包补译名。
- **语言代码**：统一规范化 zh / zh-Hant / zh-CN 的混乱，繁简要能区分。
- **publish_year**：从各种格式的日期字符串里解析出纯年份。

## 架构分层
- `sources/` —— 每个数据源一个 adapter，统一输出内部 dict。base.py 定义统一结构。新增源只加文件，不动主流程。
- `aggregator.py` —— 并发查所有源，按字段优先级合并。核心逻辑，要可单测。
- `db.py` —— SQLite 读写 + FTS5。
- `api.py` —— FastAPI 路由，薄层。
- `schema.py` —— Pydantic 模型。

## 字段合并优先级（aggregator 规则）
缺失互补，冲突按优先级覆盖，每次采用写一条 field_sources：
1. crowdsource（人工确认）—— 最高
2. openlibrary（作者、ISBN 较规整）
3. google_books（简介、封面通常最全）

## 缓存策略
查询时：先查本地 SQLite → 没有再打外部 API → 结果写回库。库随使用自然增长。

## API 端点
- GET /books/{isbn}
- GET /search?q=&page=
- GET /books/random
- POST /contribute  （Phase 2 才做）
- GET /stats

## 工作方式
- 一次只做一个 phase，不要一口气把整个项目生成完。
- 写完核心逻辑（尤其 aggregator）要配单元测试。
- 数据合并优先级和中文清洗逻辑改动前先确认，不要想当然。
