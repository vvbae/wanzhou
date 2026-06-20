# 中文开放书目 — 设计文档 v0.2

> 状态：草案，待 review。本版相对 v0.1 是一次**方向性重写**：从"扁平的 ISBN 查询 API"
> 升级为一个**社区共建、带审核的开放中文书目数据库**——作者 / 作品 / 版本 三层结构，
> 数据 CC0，代码开源。

---

## 1. 这是什么（重新定位）

一个**开放的中文书目数据库**。不是只给开发者调的 ISBN 接口，而是一个**人能逛、能查、能贡献**的中文书目站点：

- 按书名 / 作者 **搜索和浏览**已收录的书；
- 看一个**作者**的简介和他的全部作品；
- 看一本**作品**的不同**版本**（精装/平装/再版/不同出版社）；
- 我们没有的书，用户能**添加**；信息不全或有错，用户能**补全/纠错**；
- 所有用户提交**先经人工审核**才入库；
- 数据 CC0、代码开源。

数据底子来自 OpenLibrary 公开 dump + Google Books，**中文部分由社区持续补全**。

---

## 2. 三个核心动作（先把概念分清，这是 v0.1 最乱的地方）

| 动作 | 含义 | 数据来源 | 要审核吗 |
|---|---|---|---|
| **找** 🔍 | 在**我们已收录**的范围内搜书 / 按作者浏览 | 只查本地库，**绝不**实时打外部 | — |
| **加** ➕ | "我有本书你们没有" → 收进目录 | 先自动拉外部源；源也没有 → 用户手填 | 用户手填的要审 |
| **改** ✏️ | "这本/这个作者的信息不对或缺" | 用户填 | 要审 |

> **关键修正**：搜索**不再**偷偷实时聚合外部。搜不到就提示"没有？去【加书】"。
> "加书"才是触发外部查询 / 手动录入的明确入口。一个动作一个意思。

---

## 3. 用户旅程

### 3.1 找（搜索 / 浏览）
- 搜索框输书名/作者/出版社 → FTS 搜**本地目录** → 结果列表 → 点进**作品**或**版本**详情。
- 作者名可点 → **作者页**：简介 + 他的全部作品。
- 作品页：书名/作者/简介/原作名 + **该作品的所有版本列表**。
- 版本页（按 ISBN）：这一版的出版社/译者/封面/页数 + 链回它所属的作品。

### 3.2 加书（我们没有这本）
1. 用户在"加书"输入 / 扫 ISBN。
2. 系统先查**本地**：已有 → 直接跳过去。
3. 本地没有 → **实时查 Google + OpenLibrary**：
   - **源有** → 自动拉成一条版本记录（含所属作品）→ 入库（来源=外部源，可信，**不需审核**，等同我们平时的聚合）。
   - **源也没有** → 给用户一张**空白表单手动录入**整本（书名/作者/出版社/年份…）→ 进**待审队列** → 管理员审核通过 → 入库。
4. 录入时若该书属于已有作者/作品，尽量关联；拿不准就新建，归并交给后续。

### 3.3 改（补全 / 纠错）
- 在作品页 / 版本页 / 作者页点"补全·纠错" → 改字段 → 进**待审队列** → 审核通过 → 生效。
- 生效后该字段来源标 `crowdsource`（最高优先级，覆盖外部源）。

---

## 4. 数据模型（三层 + 来源 + 贡献）

核心从"一个 ISBN 一条扁平记录"改为 **作者 → 作品 → 版本** 三层：

```
author（作者） ──<  work_authors  >── work（作品） ──< edition（版本，主键 ISBN）
                                          │
   作者层：简介、原名                       作品层：书名、原作名、简介、主题
                                          版本层：出版社、译者、封面、页数、语言、丛书
```

为什么这样分：
- **译者是版本级的**（不同版本译者不同）→ 放 edition。
- **作者、原作名、简介是作品级的**（所有版本共享）→ 放 work。
- **作者简介、原名是作者级的** → 放 author。
- 一个作品多个版本 = 天然解决"一本书多版本"。

### 4.1 `authors`
| 字段 | 说明 |
|---|---|
| `id` PK | 内部 id；OL 导入的用 OL key（`OL…A`），用户新建的生成 `a_<uuid>` |
| `name` | 中文/通用显示名 |
| `name_original` | 原文名（外国作者） |
| `aliases` JSON | 其它译名/别名 |
| `bio` | 简介 |
| `ol_key` | 来源链接（可空） |
| `created_at` / `updated_at` | |

### 4.2 `works`（作品）
| 字段 | 说明 |
|---|---|
| `id` PK | OL work key（`OL…W`）或 `w_<uuid>` |
| `title` | 书名（中文） |
| `title_original` | 原作名（译作） |
| `description` | 简介 |
| `subjects` JSON | 主题/标签 |
| `first_publish_year` | |
| `ol_key` / `created_at` / `updated_at` | |

### 4.3 `work_authors`（作品↔作者，多对多）
`work_id`, `author_id`, `role`（author / editor …）

### 4.4 `editions`（版本，主键 ISBN）
| 字段 | 说明 |
|---|---|
| `isbn_13` PK | |
| `work_id` FK | 所属作品 |
| `isbn_10` | |
| `subtitle` | 副书名 |
| `translators` JSON | **译者（版本级）** |
| `publisher` | |
| `publish_date` / `publish_year` | |
| `cover_url` | |
| `page_count` | |
| `language` | zh-Hans / zh-Hant … |
| `series` | 丛书 |
| `format` | 精装/平装（可空） |
| `ol_key` / `created_at` / `updated_at` | |

### 4.5 `field_sources`（字段来源，多态）
记录**每个字段的值来自哪个源**，是开放数据可信度的基础。
`entity_type`（author/work/edition）, `entity_id`, `field_name`, `source`, `confidence`, `updated_at`

### 4.6 `contributions`（贡献 + 审核，取代 v0.1 的 edits）
| 字段 | 说明 |
|---|---|
| `id` PK | |
| `status` | `pending` / `approved` / `rejected` |
| `target_type` | author / work / edition（新建时为目标类型） |
| `target_id` | 改已有：目标 id；新建：空 |
| `kind` | `add`（新建整条） / `edit`（改字段） |
| `payload` JSON | 新建时的整条提议数据 / 改字段时 `{field: new_value, ...}` |
| `contributor_hint` | IP 或匿名标识（**不做登录**） |
| `reviewed_by` / `review_note` / `created_at` / `reviewed_at` | |

### 4.7 FTS
对 works（title）、authors（name）、editions（publisher）建 FTS5（trigram，支持中文子串）。

---

## 5. 来源与可信度

### 数据底子（管理员侧，可信，直接入库）
- **OpenLibrary dump**（每月全量，本地解析，零 API）：
  - `editions` dump → 版本 + `works:[{key}]`（**版本属于哪个作品，OL 已分好**，归并直接继承）
  - `works` dump → 作品标题/简介/主题/作者链接
  - `authors` dump → 作者名/简介
  - 范围：版本 ISBN 以 **9787** 开头（大陆）。
- **Google Books**：按配额选择性补**中文标题 / 简介 / 封面**（OL 中文常是拼音）。

### 社区侧（volunteer，须审核）
- 手动新增的书、手动改的字段。审核通过后来源标 `crowdsource`。

### 合并优先级（字段级）
`crowdsource`（审核通过的人工）> `openlibrary` > `google_books`；
**有汉字的值优先于拼音/拉丁**（避免拼音规则保留）；剩余拼音标 `needs_chinese`。

---

## 6. 审核流（moderation）

- **谁要审**：volunteer 的手动新增 / 改字段（`contributions.status=pending`）。
- **谁不用审**：管理员侧的 dump 导入、Google 聚合、"加书"时从外部源自动拉到的数据（来源可信）。
- **管理员怎么审**：一个**极简审核页** + **admin 口令**（环境变量里一个密钥，**不是用户系统**，不破"不做登录"）。
  - 待审列表 → 每条显示 目标/字段/旧值→新值/贡献者 → 通过 / 驳回（可写理由）。
  - 通过 → 应用到 author/work/edition + 写 field_sources（crowdsource）+ 记账。
- 所有贡献永久留痕，可审计、可回滚。

---

## 7. 批量提交（不做自助上传）

- 页面公布一个 **admin 邮箱** + 一份**规定格式**（CSV 模板：ISBN + 各字段列）。
- volunteer 整理好邮件发来 → 管理员用 import 脚本入库（同样过审/抽检）。
- 好处：零滥用面，零额外 UI。

---

## 8. API / 页面

### 页面
- `/` 首页 = 搜索 + 浏览
- `/work/{id}` 作品页（含版本列表）
- `/book/{isbn}` 版本页（含所属作品）
- `/author/{id}` 作者页（简介 + 作品列表）
- `/add` 加书（ISBN → 源查 → 命中入库 / 未命中手填送审）
- `/contribute`、`/edit` 改字段（送审）
- `/admin` 审核台（口令保护）

### JSON 端点（对外，CC0）
- `GET /works/{id}`、`GET /books/{isbn}`（版本，附作品 + 字段来源）、`GET /authors/{id}`
- `GET /search?q=&page=`、`GET /stats`
- `POST /contribute`（进待审）
- `GET /dump`（全量 CC0 下载，后续）

---

## 9. 部署与持久化（已定，简述）

- 容器化（Dockerfile）部署 Fly.io 美区；SQLite 单文件放**持久卷** `/data`，发版不丢；定期备份到对象存储。
- 大陆访问偏慢，MVP 接受，后续再谈香港节点。
- 详见 `DEPLOY.md`。

---

## 10. 从当前实现迁移（要改什么）

现状（v0.1 实现）是**扁平 books 表**。本设计落地需要：
- **schema 重做**：books（扁平）→ authors / works / editions / work_authors；edits → contributions（带 status）；field_sources 改多态。
- **dump 解析器重写**：从"只解析 editions、扁平进库"→ 解析 editions + works + authors 三个 dump，建三层关系。（已下载的 editions dump 仍用得上，再下 works/authors 两个。）
- **聚合/缓存**改成按 work/edition 落库。
- **API/页面**按三层 + 三动作重做。
- **审核**：新增 contributions 流 + admin 审核台。

> 已写好的：避拼音合并规则、中文清洗（译者拆分/年份/语言/原作名）、ISBN 工具、单测——**大部分可复用**，挪到新结构上即可。

---

## 11. 分阶段计划（重排）

- **A. schema + 三 dump 解析**：建 authors/works/editions/contributions 表；解析三个 OL dump 把大陆中文书的三层关系灌进本地库。
- **B. 读侧**：作品/版本/作者页 + 搜索 + 按作者浏览 + JSON 端点。
- **C. 写侧 + 审核**：加书（源查/手填）、改字段 → contributions 待审；admin 审核台。
- **D. 富化**：Google 按配额补中文标题/简介/封面。
- **E. 上线**：种子库传 Fly 持久卷 + 备份；公开、发布。
- **batch 提交**：邮箱 + CSV 格式说明，随时可加。

---

*v0.2 草案。最该先 review：第 2 节（三动作）、第 4 节（数据模型三层）、第 6 节（审核流）。定了我再更新 CLAUDE.md 并开始 A 阶段。*
