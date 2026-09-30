# 展馆多语展签审校 Web 应用

面向展馆的多语展签翻译—校对—发布流程系统。馆员维护中文原文与目标语言；译者逐条提交译文；另一名校对者确认；**同一账号不得确认自己的译文**。全部译文对应当前原文、全部确认后，馆员才能发布**不可变语言快照**；游客页只展示每种语言的最新已发布快照。

技术栈：**Python 3.12 / FastAPI + SQLite（WAL）+ 原生 HTML/CSS/JavaScript**，无前端构建步骤。

## 一键启动

```bash
docker compose up -d --build
```

首次启动自动：安装依赖、创建数据库表、初始化 3 个本地账号、2 种目标语言和 3 条示例中文展签。数据持久化在 Docker 命名卷 `exhibit-data` 中。

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时删除数据库卷（慎用）
```

## 访问地址与端口

| 入口 | 地址 | 说明 |
|---|---|---|
| 登录 / 工作台 | http://localhost:8000/ | 已登录进入工作台，未登录跳转登录页 |
| 游客页（无需登录） | http://localhost:8000/visitor | 展示各语言最新已发布快照，未发布语言显示“暂无内容” |
| OpenAPI 文档 | http://localhost:8000/docs | 接口调试文档 |

端口映射在 `docker-compose.yml` 中为 `8000:8000`，如端口被占用可改成 `"8080:8000"`。

## 默认账号（本地账号）

| 角色 | 账号 | 密码 | 权限 |
|---|---|---|---|
| 馆员 librarian | `librarian` | `lib123` | 新增/修改中文原文、新增语言、发布语言快照 |
| 译者 translator | `translator` | `tra123` | 提交/修订译文、可确认**他人**译文 |
| 校对 reviewer | `reviewer` | `rev123` | 确认译文、也可提交/修订译文 |

> 译者与校对者都能提交译文和确认译文，二者形成交叉审校：**确认人 ≠ 提交人** 是硬性服务端校验，自己提交的译文必须换账号确认。演示“禁止自审”可用 `translator` 提交后，再换 `reviewer` 登录确认。

## 配置（环境变量）

在 `docker-compose.yml` 或启动前的环境 / `.env` 中配置，**仅在数据库首次初始化时生效**：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DB_PATH` | `/data/exhibit.db` | SQLite 文件路径 |
| `SEED_LIBRARIAN_PW` | `lib123` | 馆员初始密码 |
| `SEED_TRANSLATOR_PW` | `tra123` | 译者初始密码 |
| `SEED_REVIEWER_PW` | `rev123` | 校对初始密码 |

## 工作流程与修订号规则

1. **原文修订号** `source_rev` 从 1 起递增；**译文修订号** `trans_rev` 同样从 1 起递增。
2. **修改原文**（即使又改回原来的字句）：`source_rev +1`，该展签所有语言译文的确认立即失效，看板状态变为「过期」，须译者据新原文重新提交、再由他人确认。
3. **提交 / 修订译文**：`trans_rev +1`，确认清除（回到「待审」），并记录所对应的原文修订号。
4. **乐观并发控制**：提交译文须携带读取时的 `source_rev`（修订时还须携带 `trans_rev`）；确认须携带双修订号。服务端比对不一致即返回 `409` **拒绝并保留现状**，前端提示刷新重试。
5. 看板按语言分四类统计：**待译 / 待审 / 过期 / 已确认**，并实时给出不可发布原因清单（缺译文、译文过期、待确认等）。
6. **发布**：仅当该语言全部**在展**展签均有译文、译文对应当前原文修订号且全部已确认，馆员才能发布。快照带递增版本号，以 JSON 整体落库，系统不提供修改/删除接口——**一经发布即不可变**。之后原文/译文再变动，只影响下一版，游客页继续显示旧快照直到发布新版。
7. **撤展 / 复展**（详见下节）：馆员可按展签编号、读取时的**状态版本** `status_rev` 及原因撤展，或按编号 + 状态版本复展。撤展立即从**所有语言**的游客最新发布内容中隐藏该展签；历史快照与已登录用户查询快照的内容**保持不变**。撤展展签不计入语言看板的待办与发布条件，也不写入后续快照；**无在展展签时拒绝发布**。复展保留译文但清除该展签各语言确认，游客须等该语言译文按当前原文经他人重新确认并发布新快照后才能再次看到。
8. **待审译文认领**（本轮新增，详见“待审译文认领”节）：待审译文可被译者/校对凭展签、语言及读取时的双修订号认领 **30 分钟**；提交者本人不得认领。认领期间其他账号的确认一律拒绝，认领人可提前释放，到期后他人可接手。原文/译文修订、撤展、复展使认领失效，成功确认结束认领；无人认领时沿用原确认流程（请求仍只需双修订号）。

## 撤展 / 复展

### 规则要点

- 展签有独立于原文修订号的**状态版本** `status_rev`（从 1 起，撤展 / 复展各 +1），作为撤展、复展的乐观锁；修改原文只升 `source_rev`，不影响状态版本。
- **撤展立即对所有语言生效**：通过每语言的隐藏标记 `translations.public_hidden` 在游客接口中过滤实现，**不修改任何已发布快照 JSON**，因此：
  - 历史快照原样保留；已登录用户 `GET /api/snapshots/{id}` 的字段与内容完全不变；
  - 游客接口字段约定不变（仍是 `language / published / snapshot_id / version / published_at / entries`），只是 `entries` 中不再出现被撤展的展签；全部被隐藏时游客页显示“当前暂无在展展签”。
- 撤展展签从看板四类待办统计与发布条件中排除（单独出现在“已撤展展签”区，显示状态、原因、操作者、时间），也不会写入之后发布的新快照。
- 某语言没有任何在展展签时，发布返回 `409`，`detail.reasons` 中说明“当前没有在展展签（全部展签均已撤展），不能发布”。
- **复展**：保留各语言译文文本，清除该展签各语言的确认（`confirmed=0`）；隐藏标记在该语言**发布含该展签的新快照时才清除**，所以复展后、重新确认并发布前，游客仍看不到它（各语言相互独立）。
- 撤展期间禁止提交 / 修订 / 确认该展签译文（`409`）。
- 状态版本过期、重复变更（对已撤展再撤展 / 对在展再复展）、非馆员操作（`403`）、编号无效（`404`）、撤展原因为空（`422`）一律明确拒绝且不改变数据。
- 撤展 / 复展与原文修改、译文提交、确认、发布共用同一把写锁并在库内按当前状态校验，按一致状态串行处理。

### HTTP API

| 方法 | 路径 | 角色 | 请求体 | 说明 |
|---|---|---|---|---|
| POST | `/api/labels/{id}/withdraw` | librarian | `{"status_rev": 1, "reason": "文物外借展出"}` | 撤展；返回展签完整状态（含 `status_rev`、`withdrawn_by_name`、`withdrawn_at` 等） |
| POST | `/api/labels/{id}/restore` | librarian | `{"status_rev": 2}` | 复展；保留译文、清除各语言确认 |

错误码：`404` 编号无效；`403` 非馆员；`409` 状态版本过期或重复变更（响应体 `detail` 含服务器当前状态版本）；`422` 原因为空。看板 `GET /api/board/{lang_id}` 新增 `withdrawn_total` 与 `withdrawn_items[]`（其 `reason` 即撤展原因，`label` 内含 `withdrawn_reason / withdrawn_by_name / withdrawn_at / restored_by_name / restored_at`）；`GET /api/labels` 的每条展签也带这些字段。

### curl 调用示例

```bash
# 1) 馆员登录（保存 cookie）
curl -c lib.cookie -X POST http://localhost:8000/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"librarian","password":"lib123"}'

# 2) 查展签，取编号与当前状态版本
curl -b lib.cookie http://localhost:8000/api/labels

# 3) 撤展 #1（携带读取时的 status_rev 与原因）
curl -b lib.cookie -X POST http://localhost:8000/api/labels/1/withdraw \
  -H 'Content-Type: application/json' \
  -d '{"status_rev":1,"reason":"文物外借展出至年底"}'
# 游客两语言最新内容立即不再含 #1；历史快照不变
curl http://localhost:8000/api/public/languages

# 4) 复展 #1（携带撤展后的 status_rev=2）
curl -b lib.cookie -X POST http://localhost:8000/api/labels/1/restore \
  -H 'Content-Type: application/json' -d '{"status_rev":2}'

# 5) 译者/校对重新确认后，馆员发布新快照，游客才再次看到 #1
```

工作台操作：馆员在“中文原文展签”每条展签下填写撤展原因后点【撤展】，或对已撤展点【复展】；看板底部“已撤展展签”区展示原因、操作者与时间；译者 / 校对对撤展展签只读、无编辑操作。

## 待审译文认领

待审（在展、译文对应当前原文修订、尚未确认）译文可由译者或校对**认领 30 分钟**，表示“这条我来校对”，避免多人同时审校同一条。

### 规则要点

- **认领凭证**：`展签 + 语言 + 读取时的原文修订号 source_rev + 译文修订号 trans_rev`，四者共同确定认领对象，缺一不可（与确认接口的乐观锁一致）。
- **谁能认领**：译者、校对均可；**提交该译文的账号不得认领自己的译文**（`403`，与禁止自审一致）。馆员无认领入口（`403`）。
- **互斥与看板**：同一译文同一时刻仅一条有效认领。看板（`GET /api/board/{lang_id}`）每个在展条目的新增字段 `claim` 展示认领人（`claimer_id / claimer_name / claimer_username`）、认领与到期时间（`claimed_at / expires_at`，UTC）及认领所基于的双修订号；无有效认领时为 `null`。前端在条目上显示认领条与**剩余时间倒计时**。
- **认领期间**：其他账号确认该译文一律 `409` 拒绝；认领人可正常确认（确认成功即结束认领）。**无人认领时沿用原有确认流程**——确认请求体不变，仍只需 `{"source_rev", "trans_rev"}`，自审拒绝（`403`）与修订冲突拒绝（`409`）约定全部保留。
- **提前释放**：认领人可随时释放，释放后他人可立即接手；他人不能代为释放（`403`）。
- **到期**：到期后认领不再展示、不再阻拦，他人认领即自动接手（旧的到期记录被覆盖）。到期采用服务端 UTC 时间惰性判定，无后台任务。
- **自动失效**：以下事件删除相关认领（无论是否到期）：
  - 馆员**修订原文**（`source_rev` 变化）→ 该展签**所有语言**的认领失效；
  - 任何人**提交/修订译文**（`trans_rev` 变化）→ 该（语言, 展签）的认领失效；
  - **撤展 / 复展** → 该展签所有语言的认领失效；
  - **确认成功** → 该条认领结束。
- **拒绝且不改变状态**：认领**过期版本**（原文或译文修订号与当前不符、译文基于旧版原文）、**已确认**、**已撤展**的译文，均返回 `409`，且不写入/改动任何认领；展签或语言不存在返回 `404`，尚无译文返回 `404`。
- **并发一致**：认领、释放、确认、译文提交、撤复展、原文修订共用同一把写锁串行执行，并在库内按当前修订号与展签状态即时校验，因此认领与确认并发时，最终结果必与当前修订、展签状态一致（恰一个操作生效，另一个被拒）。
- **重复认领**：对自己仍有效持有的认领再次认领是幂等的，返回原认领（不续期、不新建）；对他人有效认领的重复认领返回 `409`。
- 认领**不阻止**译文修订本身（修订遵循既有乐观锁规则）：修订一旦成功，原认领立即失效，这是预期行为。

### HTTP API

| 方法 | 路径 | 角色 | 请求体 | 说明 |
|---|---|---|---|---|
| POST | `/api/board/{lang_id}/labels/{label_id}/claim` | translator / reviewer | `{"source_rev": 1, "trans_rev": 1}` | 认领 30 分钟；返回认领记录（含认领人、`claimed_at`、`expires_at`） |
| POST | `/api/board/{lang_id}/labels/{label_id}/claim/release` | translator / reviewer | 无 | 认领人提前释放；无有效认领时幂等成功 |

错误码：`401` 未登录；`403` 非译者/校对角色、提交者认领自己的译文、或代他人释放；`404` 语言/展签/译文不存在；`409` 已撤展、已确认、版本过期（原文/译文修订号不匹配、译文基于旧版原文）、他人有效认领中。看板响应的每个在展条目新增 `claim` 字段（有效认领对象或 `null`）；已撤展区（`withdrawn_items`）不带认领。认领信息为只读快照字段，不改变任何既有接口的请求格式。

### curl 调用示例

```bash
# 0) 三个账号分别登录（保存 cookie）
curl -c lib.cookie -X POST http://localhost:8000/api/login \
  -H 'Content-Type: application/json' -d '{"username":"librarian","password":"lib123"}'
curl -c tra.cookie -X POST http://localhost:8000/api/login \
  -H 'Content-Type: application/json' -d '{"username":"translator","password":"tra123"}'
curl -c rev.cookie -X POST http://localhost:8000/api/login \
  -H 'Content-Type: application/json' -d '{"username":"reviewer","password":"rev123"}'

# 1) 译者提交展签 #1 的英文译文（语言 id=1，读取时原文修订号 1）
curl -b tra.cookie -X POST http://localhost:8000/api/board/1/labels/1/translation \
  -H 'Content-Type: application/json' \
  -d '{"text":"Blue and white vase with interlocking lotus scrolls","source_rev":1}'

# 2) 查看板，取双修订号；待审条目 claim 为 null
curl -b rev.cookie http://localhost:8000/api/board/1

# 3) 校对凭双修订号认领 30 分钟（提交者本人调此接口会 403）
curl -b rev.cookie -X POST http://localhost:8000/api/board/1/labels/1/claim \
  -H 'Content-Type: application/json' -d '{"source_rev":1,"trans_rev":1}'
# => {"id":1,"language_id":1,"label_id":1,"claimer_id":3,
#     "claimed_at":"2026-09-30 14:12:09","expires_at":"2026-09-30 14:42:09", ...}

# 4) 认领期间其他账号确认被拒（409，detail 说明认领人与到期时间）
curl -b tra.cookie -X POST http://localhost:8000/api/board/1/labels/1/confirm \
  -H 'Content-Type: application/json' -d '{"source_rev":1,"trans_rev":1}'

# 5a) 认领人提前释放（之后他人可接手；无有效认领时重复调用也返回 200）
curl -b rev.cookie -X POST http://localhost:8000/api/board/1/labels/1/claim/release

# 5b) 或认领人直接确认——成功后认领自动结束（确认请求仍是原有双修订号）
curl -b rev.cookie -X POST http://localhost:8000/api/board/1/labels/1/confirm \
  -H 'Content-Type: application/json' -d '{"source_rev":1,"trans_rev":1}'

# 6) 失效示例：认领期间馆员修订原文（source_rev→2），认领立即失效；
#    再用旧修订号认领会 409（过期版本），译者据新原文修订后凭 (2, 2) 重新认领
curl -b lib.cookie -X PUT http://localhost:8000/api/labels/1 \
  -H 'Content-Type: application/json' \
  -d '{"source_text":"青花缠枝莲纹瓶（修订说明：永乐官窑）","source_rev":1}'
```

工作台操作：译者/校对在「待审」条目上可点【认领校对 30 分钟】；认领后条目显示认领人与到期倒计时，本人持有时显示【提前释放认领】与【确认译文】按钮，他人持有时确认按钮隐藏并提示等待到期/释放；馆员与所有角色都能在看板上只读看到当前认领人。到期后刷新看板（或重新进入该语言页签）即可接手；倒计时归零会提示“已到期”。

## 本地无 Docker 运行（开发）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DB_PATH=./exhibit.db uvicorn app.main:app --reload
```

## 目录结构

```
app/
├── db.py                 # 连接/Schema/默认数据初始化（账号、语言、示例展签；含 claims 认领表）
├── main.py               # FastAPI 全部接口、鉴权、看板聚合、认领/释放与发布逻辑
└── static/
    ├── login.html        # 登录页
    ├── app.html/app.js   # 审校工作台（按角色显示操作、认领展示/认领/释放/倒计时）
    ├── visitor.html      # 游客公开页
    └── styles.css
test_claims.py            # 认领功能端到端测试（stdlib + FastAPI TestClient，56 项断言）
Dockerfile
docker-compose.yml
requirements.txt
```

跑测试（需安装 `requirements.txt` 与 `httpx`；使用独立临时数据库，不影响卷内数据）：

```bash
python3 test_claims.py
```
