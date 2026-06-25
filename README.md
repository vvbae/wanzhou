# 万轴 · 中文开放图书馆

万轴是一个社区共建的开放中文书目数据库。网站：[wanzhoubooks.org](https://wanzhoubooks.org)。

按书名或作者检索图书，查看同一部作品的不同版本，浏览一位作者及其全部作品；添加缺失的书，补全、纠错书或者作者信息，所有修改经人工审核后入库。数据以 CC0 协议进入公共领域，代码以 MIT 协议开源。

## 是什么

万轴目前支持的功能包括：

- **找书** — 在已收录范围内搜索、浏览。只查本地库。
- **加书** — 输入 ISBN 添加图书，自动从 Google Books、OpenLibrary 获取已有信息；外部源也没有的，可手动填写。
- **改书** — 补全或纠正已有图书、作者的字段。


## 数据来源

- **OpenLibrary dump**：本地离线解析（零 API 调用），范围为大陆 9787 开头的 ISBN，作为基础数据。
- **Google Books**：按配额补充中文标题、简介、封面。
- **Wikidata**：补充作者的一句话身份与生卒年（CC0）。
- **众包**：志愿者在站点上补全、纠错，审核通过后采纳。

## 本地运行

```bash
uv sync
CNBIB_DB=data/library.db uv run uvicorn cnbib.api:app --reload   # http://127.0.0.1:8000
```

数据库由 `parse_dumps.py` 从 OpenLibrary dump 离线构建；本地开发把 `CNBIB_DB` 指向已有的库文件即可。加书时实时查外部源需要 Google Books API key（可选，不配时仅 OpenLibrary 可用）：

```bash
export GOOGLE_BOOKS_API_KEY=...
```

## 功能

- 按书名、作者、ISBN 搜索；结果与主题浏览页支持翻页
- 作品页（含各版本）、版本页、作者页（含全部作品）
- 按主题浏览
- 补全 / 纠错图书与作者字段，提交进待审
- 审核台：登录后审核待审条目，通过才入库；众包冲突可择一采纳
- 账号与角色：匿名可贡献；审核员为邀请制；审核员不能审核自己的提交
- 作者合并：将重复的作者条目并为一个
- 批量提交：按 CSV 模板整理后邮件提交，管理员用 `import_csv.py` 导入为待审条目

## 协议

- 代码：MIT（见 [LICENSE](LICENSE)）。
- 数据：CC0（公共领域）。

## 贡献

欢迎参与，方式见 [CONTRIBUTING.md](CONTRIBUTING.md)。补书、纠错等数据贡献直接在站点上进行；代码改进走 GitHub。
