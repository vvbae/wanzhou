# CLAUDE.md

本文件是给 Claude Code 的项目上下文。每次开始工作前先读这里 + `docs/design.md`（v0.2，权威设计），严格遵守边界。

## 项目是什么
一个**社区共建、带审核的开放中文书目数据库**。人能逛/查/贡献：按书名作者搜索浏览、看作者简介和作品、看一本作品的不同版本；我们没有的书用户能加，信息不全能补，所有用户提交先人工审核才入库。数据 CC0，代码开源。

> 注意：这**不再**是 v0.1 那个"扁平 ISBN API"。定位见 `docs/design.md` 第 1 节。别再用"ISBN API / 不是 OL 克隆"那套老话给项目定型。

## 技术栈（已定，不要换）
- Python 3.12+，FastAPI
- SQLite + FTS5（不要上 Postgres / Elasticsearch）
- httpx 做异步 HTTP
- Pydantic 做数据模型
- 包管理用 uv

## 三个核心动作（概念必须分清）
- **找** 🔍：在已收录范围内搜索/浏览。**只查本地库，绝不实时打外部。**
- **加** ➕：用户输 ISBN 加书。先自动拉 Google/OL；源也没有 → 用户手填。手填的进待审。
- **改** ✏️：补全/纠错已有书或作者字段。进待审。

## 数据模型：三层（核心，改前先确认）
`作者 author ──< work_authors >── 作品 work ──< 版本 edition（主键 isbn_13）`
- **author**：name, name_original, aliases(JSON), bio, ol_key …
- **work**：title, title_original, description, subjects(JSON), first_publish_year, ol_key …
- **edition**（主键 isbn_13）：work_id, isbn_10, subtitle, **translators(JSON，版本级)**, publisher, publish_date, publish_year, cover_url, page_count, language, series, format, ol_key …
- **work_authors**：work_id, author_id, role
- **field_sources**（多态）：entity_type(author/work/edition), entity_id, field_name, source, confidence
- **contributions**（贡献+审核）：status(pending/approved/rejected), target_type, target_id, kind(add/edit), payload(JSON), contributor_hint, reviewed_by …

分层原则：译者=版本级；作者/原作名/简介=作品级；作者简介/原名=作者级。

## 中文特殊处理（默认逻辑会错，已实现，复用）
- 译者：从 Google authors / OL by_statement（`X zhu ; Y yi`）拆出来。
- 原作名：OL `translation_of` 或 notes `Translation of: X`。
- **外国作者两个都存**：`authors`=中文译名，`name_original`=原文名。
- 语言代码：规范化 zh/zh-Hant/zh-CN，繁简可分。
- publish_year：从日期串解析。
- **全力避免拼音**：合并时有汉字的值优先（与源优先级无关）；剩余拼音标 needs_chinese 交众包。

## 来源与可信度
- 底子：OpenLibrary dump（editions+works+authors，本地解析，零 API，范围 ISBN 9787 大陆）；Google Books 按配额补中文标题/简介/封面。
- 合并优先级：`crowdsource`（审核通过）> `openlibrary` > `google_books`。每个采用值写一条 field_sources。
- **谁要审**：volunteer 手动新增/改字段。**谁不审**：dump 导入、Google 聚合、加书时从源自动拉到的（可信）。

## 审核 / 批量
- 审核：contributions 走 pending → admin 审核台（**口令保护，不是用户系统，不破"不做登录"**）→ 通过才落库 + 写 field_sources(crowdsource)。
- 批量：**不做自助上传**。页面公布 admin 邮箱 + CSV 格式，volunteer 邮件发来，管理员脚本入库。

## 仍然不做
- ❌ 用户登录/账号体系（admin 用口令；contributor 用 IP/匿名）
- ❌ 自建搜索引擎（用 FTS5）
- ❌ 封面自托管（只存 URL）
- ❌ 全文阅读/电子书
- ❌ 书评/书单/社交

## 工作方式
- 一次推进一个阶段，核心逻辑配单测。
- 数据模型、合并优先级、中文清洗、审核规则，改动前先确认，不要想当然。
- 实现正在从 v0.1 扁平 books 迁移到三层：新数据层在 `cnbib/store.py`，旧 `cnbib/db.py` 待读侧切换后退役。
