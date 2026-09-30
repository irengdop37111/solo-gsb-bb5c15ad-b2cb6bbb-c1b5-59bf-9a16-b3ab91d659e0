# 展馆多语展签审校 Web 应用

面向展馆的多语展签翻译—校对—发布流程系统。馆员维护中文原文与目标语言；译者逐条提交译文；另一名校对者确认；**同一账号不得确认自己的译文**。全部译文对应当前原文、全部确认后，馆员才能发布**不可变语言快照**；游客页只展示每种语言的最新已发布快照。译文待审时，译者/校对者还可**认领 30 分钟**，看板公示认领人与到期时间，认领期间他人不能确认（详见下文「译文认领」）。

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
8. **译文认领**（本轮新增，详见「译文认领」节）：待审译文可由译者/校对凭展签、语言及读取时的双修订号认领 30 分钟，**提交该译文的账号不得认领**；认领人可提前释放，到期后他人可接手，认领期间其他账号的确认一律拒绝。无人认领时确认流程与请求体不变。

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

## 译文认领（本轮新增）

待审译文支持 **30 分钟限时认领**，用于多人交叉审校时避免“两个人同时校对同一条”。认领不改变译文内容与确认状态，只影响“这段时间内谁能确认”。

### 规则要点

- **谁能认领**：`translator` / `reviewer` 账号；馆员不能认领。**提交该译文的账号不得认领自己的译文**（`403`）。
- **认领凭据**：展签编号、语言 ID、读取时的**原文修订号与译文修订号**（双修订号乐观锁），与确认请求所用一致。
- **有效期 30 分钟**：成功即得 `claimed_at` / `expires_at`（UTC，与库内其他时间同格式），看板与接口同时返回剩余秒数 `remaining_seconds`。
- **独占**：同一译文（展签 + 语言）同一时刻仅一个有效认领。他人持有中再认领 → `409`，响应体 `detail` 内含当前认领（`claim`）与 `held_by`；**本人重复认领幂等**，直接返回原认领，不延长、不重置到期时间。
- **看板公示**：`GET /api/board/{lang_id}` 的每条在展展签条目新增 `claim` 字段（无有效认领为 `null`），内含 `claimant_id / claimant_name / claimant_username / claimed_at / expires_at / remaining_seconds` 及认领时双修订号。前端工作台对所有人显示认领人与到期倒计时。
- **提前释放**：仅认领人本人可调释放接口；他人释放 → `403`，无有效认领先释放 → `404`，均不改变状态。
- **到期接手**：到期是按当前时间即时判定（不做后台清理）；到期后原认领视为无效，任何符合条件的账号可重新认领（数据库中仍只保留一行，新认领覆盖旧行）。
- **与确认的关系**：
  - 有效认领期间，**其他账号**调用确认接口一律 `409` 拒绝（响应体同样带当前认领信息），认领状态不变；
  - **认领人本人**可在认领有效期内直接确认（仍须满足双修订号、禁止自审等既有校验），**确认成功后认领即结束**；
  - **无人认领时完全沿用现有确认流程**：确认请求仍只需双修订号，不需要先认领；自审拒绝（`403`）与修订冲突拒绝（`409`）约定全部保留。
- **认领失效（自动删除）**：原文修订（该展签各语言）、译文提交 / 修订（该译文）、撤展、复展都会使相关认领立即失效；成功确认也结束认领。
- **禁止认领且不改变状态**的场景：展签/语言/译文不存在（`404`）、展签已撤展（`409`）、译文已确认（`409`）、译文基于旧版原文即过期（`409`）、双修订号与当前不一致（`409`）、提交人自领（`403`）、馆员等无角色账号（`403`，由角色依赖拦截）。
- **并发一致**：认领、释放、确认、译文提交、原文修改、撤展 / 复展共用同一把写锁并在库内按当前修订号与展签状态校验，按一致状态串行处理；认领与确认并发时，结果始终与当前修订、展签状态一致。
- 认领不影响发布条件、快照内容与游客页；快照/游客接口字段无任何变化。

### HTTP API

| 方法 | 路径 | 角色 | 请求体 | 说明 |
|---|---|---|---|---|
| POST | `/api/board/{lang_id}/labels/{label_id}/claim` | translator / reviewer | `{"source_rev": 1, "trans_rev": 1}` | 认领 30 分钟；返回认领对象（含到期时间与剩余秒数）。他人持有中 `409`（`detail.claim` 为当前认领）；提交人自领 `403`；已撤展 / 已确认 / 过期 / 修订号不符 `409`；不存在 `404` |
| POST | `/api/board/{lang_id}/labels/{label_id}/claim/release` | 任意登录用户（须为认领人） | 无 | 提前释放；非认领人 `403`，无有效认领 `404` |
| GET | `/api/board/{lang_id}/labels/{label_id}/claim` | 任意登录用户 | — | 查询当前有效认领；无则返回 `{"claim": null}` |

看板变化：`GET /api/board/{lang_id}` 与 `GET /api/overview` 的认证、字段保持兼容；看板每个在展条目（`items[]`）新增 `claim` 字段（结构同上），已撤展区 `withdrawn_items[]` 不含认领（撤展即失效）。确认接口 `POST .../confirm` 在“他人有效认领”时新增一种 `409`（`detail.claim` / `detail.held_by`），其余拒绝约定（自审 `403`、修订冲突 `409`、撤展 `409`）不变，请求体仍为 `{"source_rev", "trans_rev"}`。

### curl 调用示例

```bash
# 0) 三个账号分别登录（演示用，正式环境密码见 README 账号表）
curl -c tra.cookie -X POST http://localhost:8000/api/login -H 'Content-Type: application/json' \
  -d '{"username":"translator","password":"tra123"}'
curl -c rev.cookie -X POST http://localhost:8000/api/login -H 'Content-Type: application/json' \
  -d '{"username":"reviewer","password":"rev123"}'
curl -c lib.cookie -X POST http://localhost:8000/api/login -H 'Content-Type: application/json' \
  -d '{"username":"librarian","password":"lib123"}'

# 1) 译者对展签 #1 / 语言 1（English）提交译文（无译文则无从认领）
curl -b tra.cookie -X POST http://localhost:8000/api/board/1/labels/1/translation \
  -H 'Content-Type: application/json' \
  -d '{"text":"Blue-and-white vase with interlocking lotus scrolls","source_rev":1}'

# 2) 校对（非提交人）凭双修订号认领 30 分钟
curl -b rev.cookie -X POST http://localhost:8000/api/board/1/labels/1/claim \
  -H 'Content-Type: application/json' -d '{"source_rev":1,"trans_rev":1}'
# -> {"claimant_id":3,"claimant_name":"校对老王","claimed_at":"...","expires_at":"...","remaining_seconds":1800,...}

# 3) 看板中该条带 claim（认领人/到期时间/剩余秒数）
curl -b lib.cookie http://localhost:8000/api/board/1

# 4) 提交人自己认领 → 403；其他账号在认领期间确认 → 409（detail.claim 为当前认领）
curl -b tra.cookie -X POST http://localhost:8000/api/board/1/labels/1/claim \
  -H 'Content-Type: application/json' -d '{"source_rev":1,"trans_rev":1}'   # 403 提交人不得自领
curl -b tra.cookie -X POST http://localhost:8000/api/board/1/labels/1/confirm \
  -H 'Content-Type: application/json' -d '{"source_rev":1,"trans_rev":1}'   # 409 正被他人认领

# 5) 认领人可提前释放（释放后任何符合条件的账号可认领/确认；确认请求体不变）
curl -b rev.cookie -X POST http://localhost:8000/api/board/1/labels/1/claim/release
curl -b rev.cookie -X POST http://localhost:8000/api/board/1/labels/1/confirm \
  -H 'Content-Type: application/json' -d '{"source_rev":1,"trans_rev":1}'   # 成功确认，认领随之结束

# 6) 认领 30 分钟到期后：GET 视为无认领，他人可凭当前双修订号接手
curl -b tra.cookie http://localhost:8000/api/board/1/labels/1/claim          # {"claim": null}
```

> 说明：认领在以下事件发生时自动失效，无需额外调用——馆员修改原文、任何人提交 / 修订该译文、撤展、复展、成功确认。对已确认、已撤展、基于旧版原文（过期）的译文发起认领，服务端一律拒绝且不改变任何状态。

工作台操作：在语言看板每条「待审」译文下，译者 / 校对可点【认领 30 分钟】；被认有条目显示紫色（本人）或黄色（他人）认领条，含认领人、到期时间与秒级倒计时，本人可点【提前释放】。本人持有认领时可直接【确认译文】；他人持有中不显示确认按钮并提示等待；无人认领时同时显示【认领】与【直接确认（未认领时按原流程）】。到期后页面倒计时归零会自动刷新看板，他人即可接手。馆员在看板上只读查看认领人与到期时间。

## 本地无 Docker 运行（开发）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DB_PATH=./exhibit.db uvicorn app.main:app --reload
```

## 目录结构

```
app/
├── db.py                 # 连接/Schema/默认数据初始化（账号、语言、示例展签）
├── main.py               # FastAPI 全部接口、鉴权、看板聚合与发布逻辑
└── static/
    ├── login.html        # 登录页
    ├── app.html/app.js   # 审校工作台（按角色显示操作）
    ├── visitor.html      # 游客公开页
    └── styles.css
Dockerfile
docker-compose.yml
requirements.txt
```
