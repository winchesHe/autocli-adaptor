# autocli-adaptor

本仓库是 autocli 的本地 adapter 集合（YAML 描述 + 少量 Python 辅助脚本）。autocli 通过浏览器扩展接管用户日常 Chrome，在 evaluate 阶段把 JS 注入目标站点页面 context 执行——既可以采 DOM，也可以走站点自身的 GraphQL/REST 接口。

---

## 目录约定

```
~/.autocli/
├── adapters/
│   └── <site>/
│       ├── <command>.yaml       # 主适配器
│       ├── <command>.yaml.bak.* # 改动前的本地备份
│       └── *.py                 # 可选的 stdin/stdout 辅助脚本
├── config.json                  # autocli auth token + LLM 配置
└── README.md
```

---

## 抓取链路：DOM vs GraphQL

X、知乎这类 SPA 站点都有两种抓取路径，**优先选 GQL/API，DOM 仅作兜底**。

### DOM 路径的典型坑

- `article` 类节点用虚拟列表渲染，**滚出视口的节点会被卸载**，`document.querySelectorAll` 只能拿到当前视口附近的几个。
- `window.scrollTo(0, scrollHeight)` 跳跃式滚动容易让中间区域跳过 IntersectionObserver，懒加载不触发。
- 如果 autocli 操作的 tab 处于**后台**，Chrome 节流后台 tab 的网络/IO，X 的 timeline 可能只下发首屏几条，再怎么滚都加载不出来。
- 置顶 post 出现位置不固定，可能与正常时间线交错，让"按 created_at 排序"的判断失效。

诊断命令（在 evaluate 末尾注入页面状态到 `warnings`）：

```js
const diag = {
  url: location.href,
  title: document.title,
  scroll_height_final: document.body.scrollHeight,
  articles_in_dom: document.querySelectorAll('article[data-testid="tweet"]').length,
  has_ct0_cookie: /(?:^|;\s*)ct0=/.test(document.cookie || ''),
  login_link: !!document.querySelector('a[href="/login"], a[data-testid="loginButton"]'),
  rate_limit_text: (document.body.innerText.match(/(rate.?limit|temporarily.?limited|too many requests|something went wrong)/i) || [])[0] || '',
  empty_state: !!document.querySelector('div[data-testid="emptyState"]'),
};
warnings.push(`diag=${JSON.stringify(diag)}`);
```

`articles_in_dom` 远小于预期、`title` 为空、`has_ct0_cookie` 为 `false`，分别对应「虚拟列表/后台 tab 节流」「SPA 路由没 hydration 完成」「未登录」三类问题。

### GraphQL 路径（推荐）

适用于已登录 SPA。利用浏览器自带的 cookies + 站点公开的 web bearer，直接调站内 GQL，不依赖 DOM 渲染。

**Cookies 来源**：evaluate 在站点页面 context 执行，直接 `document.cookie` 读 `ct0`（CSRF），其他 HttpOnly cookie（`auth_token`、`twid` 等）由 `credentials: 'include'` 自动带上。**前提是用户日常 Chrome 已登录该域名**。

**通用 GQL 调用骨架**（以 X 为例，参考 [adapters/twitter/user-posts.yaml](adapters/twitter/user-posts.yaml)）：

```js
const bearer = 'AAAAAAAAAAAAAAAAAAAAANRILgAAAAAA...'; // X web 公开 bearer
const ct0 = document.cookie.split(';').map((c) => c.trim())
  .find((c) => c.startsWith('ct0='))?.split('=')[1];

const resolveQueryId = async (operationName, fallbackId) => {
  // 1) 先查 fa0311/twitter-openapi 的 placeholder.json
  try {
    const r = await fetch('https://raw.githubusercontent.com/fa0311/twitter-openapi/refs/heads/main/src/config/placeholder.json');
    if (r.ok) {
      const m = await r.json();
      if (m[operationName]?.queryId) return m[operationName].queryId;
    }
  } catch {}
  // 2) 再扫 client-web JS bundle 里的 queryId 字面量
  for (const url of performance.getEntriesByType('resource')
      .filter((r) => r.name.includes('client-web') && r.name.endsWith('.js'))
      .map((r) => r.name).slice(0, 15)) {
    try {
      const text = await (await fetch(url)).text();
      const re = new RegExp(`queryId:"([A-Za-z0-9_-]+)"[^}]{0,200}operationName:"${operationName}"`);
      const m = text.match(re);
      if (m) return m[1];
    } catch {}
  }
  // 3) fallback 到硬编码 ID（会被 X 周期性轮换）
  return fallbackId;
};

const gqlGet = async (operationName, queryId, variables, features, fieldToggles) => {
  if (!ct0) throw new Error('No ct0 cookie - not logged into x.com');
  const url = `/i/api/graphql/${queryId}/${operationName}`
    + `?variables=${encodeURIComponent(JSON.stringify(variables))}`
    + `&features=${encodeURIComponent(JSON.stringify(features))}`
    + (fieldToggles ? `&fieldToggles=${encodeURIComponent(JSON.stringify(fieldToggles))}` : '');
  const resp = await fetch(url, {
    credentials: 'include',
    headers: {
      Authorization: `Bearer ${decodeURIComponent(bearer)}`,
      'X-Csrf-Token': ct0,
      'X-Twitter-Active-User': 'yes',
      'X-Twitter-Auth-Type': 'OAuth2Session',
    },
  });
  if (!resp.ok) throw new Error(`${operationName} HTTP ${resp.status}`);
  return resp.json();
};
```

**典型 timeline 抓取流程**：`UserByScreenName` 拿 `rest_id` → `UserTweets` 用 cursor 分页（每页 ≤40 条）→ 解析 `data.user.result.timeline_v2.timeline.instructions[].entries`：
- `tweet-` 开头的 entry：`content.itemContent.tweet_results.result` 是 Tweet 对象。
- `profile-conversation-` 开头：用户自己的 thread，items 数组里逐条取。
- `cursor-bottom-` 开头：下页 cursor。
- `TimelinePinEntry` 类型：置顶（按需跳过，避免乱序）。

时间字段 `legacy.created_at` 是 `Sun May 04 13:39:49 +0000 2026` 风格，`new Date(s).toISOString()` 即可标准化。

### 严格 GQL（推荐 adapter 默认形态）

不要在 GQL 失败时回退 DOM 滚动。一旦被 X 限流（429）：
- DOM 滚动只会让 Chrome 继续向 X 发请求，**加剧限流**，把短时限流变成长时限流。
- 调用方拿到一份"用 DOM 兜底但残缺"的结果，容易误以为同步成功，让中间漏的 post 永远补不上。

正确做法：GQL 失败直接抛错，由调用方决定退出还是延后重试：

```js
const posts = await fetchUserTimelineGql(username, limit);
if (!posts.length) {
  throw new Error(`UserTweets returned 0 posts for ${username}`);
}
```

`twitter/user-posts.yaml` 已删除 DOM 滚动路径和相关 `extractFromArticle` / `collectVisiblePosts` helpers。

### GQL 节流（必做）

X web GQL 在短时间窗口内会触发 429，恢复时间从几分钟到几十分钟不等。adapter 内部需要在翻页之间加最小间隔；外层批量脚本需要在用户间也加间隔。

**adapter 内部**（cursor 分页）：

```js
const rateLimitMs = Math.max(0, Number(args['rate-limit-ms'] || 1200));
for (let page = 0; page < maxPages; page++) {
  if (page > 0 && rateLimitMs > 0) await sleep(rateLimitMs);
  // gqlGet UserTweets ...
}
```

**外层批量脚本**：
- 用户间至少 sleep 1-2 秒
- 检测到 stderr 含 `HTTP 429` 立刻中止后续，不要继续打 X
- 失败别立刻重试，先冷却 5-15 分钟

### 批量复用（一次 navigate 服务多用户/多目标）

批量场景里每个目标都重 navigate `/home` 是浪费——cookies 几小时才过期。推荐 adapter **直接接受批量参数**，在一次 evaluate 内部循环跑 GQL：

```yaml
args:
  username:
    description: 单个 handle（单目标模式）
  usernames:
    type: str
    default: ""
    description: comma-separated handles（批量；提供时优先于 username）

pipeline:
  - navigate:                      # 单次 navigate，整批共享
      url: https://x.com/home
      settleMs: 3000
  - evaluate: |
      (async () => {
        const handles = String(args.usernames || '').split(',').map(s => s.trim()).filter(Boolean);
        const targets = handles.length ? handles : [args.username];
        const interUserMs = Number(args['inter-user-ms'] || 1500);
        const all = [];
        for (let i = 0; i < targets.length; i++) {
          if (i > 0 && interUserMs > 0) await sleep(interUserMs);
          try {
            const posts = await fetchTimelineGql(targets[i]);
            posts.forEach(p => p.requested_username = targets[i]);
            all.push(...posts);
          } catch (e) {
            warnings.push(`fetch failed for ${targets[i]}: ${e.message}`);
            if (String(e.message).includes('HTTP 429')) throw e;  // 整批退出
          }
        }
        return all;
      })()
```

外层脚本一次调用拿到带 `requested_username` 标签的混合数组，按 handle 切分各自做后续处理（cache / 落盘 / 渲染）。参考实现 [stock-infos/scripts/fetch_all_v2.sh](../Desktop/Company/person/skills/stock-infos/scripts/fetch_all_v2.sh) + [_process_user.sh](../Desktop/Company/person/skills/stock-infos/scripts/_process_user.sh)。

> 早期方案是拆"cold（含 navigate）+ fast（无 navigate）" 两个 adapter，靠外层 warm-file 调度。复杂、状态多。改成"adapter 内批量循环"后维护一个 adapter 即可，且更稳。

---

## DOM 滚动调优要点（仅供新接入站点参考）

> Twitter/X adapter 已**不再使用** DOM 滚动——GQL 严格模式优先，失败抛错。下面是新接入其它站点时如果只能走 DOM 路径的调优要点：

- **小步滚动**：`window.scrollBy(0, innerHeight*0.8)`，避免跳到底导致虚拟列表漏渲染。
- **周期到底触发懒加载**：每 N 步做一次 `window.scrollTo(0, scrollHeight)`。
- **多次 harvest**：滚后立刻采一次，再延迟 500-800ms 二采。
- **stagnant 阈值**：`scrollHeight` 不增 + `scrollY+innerHeight ≥ scrollHeight - 300` 才算真停滞，连续 4-6 次再退出。
- **总耗时控制在 daemon 单次 evaluate 超时（~30s）以内**：sleep 别叠加超过 25s，否则触发 cli 重试 + 总 60s 超时。
- **不要把 DOM 滚动当做 GQL 的"自动兜底"**：429 / 限流场景下 DOM 抓取本身也会打站点接口，反而加剧限流。

---

## 时间窗口过滤约定

涉及 `since/until` 的 adapter 统一用 ISO8601 UTC，命令行支持 `YYYY-MM-DD` 简写：
- `since` 不带时间默认补 `T00:00:00.000Z`
- `until` 不带时间默认补 `T23:59:59.999Z`
- 入参非法时 `warnings.push('Ignored invalid ... value')`，不抛错。

cursor 分页里命中 `created_at < since` 时**立即 break**，避免无意义翻页。

---

## 缓存策略：list-then-diff（推荐）

外层脚本（如 `stock-infos/scripts/fetch_user_v2.sh`）做增量缓存时，**不要**用"窗口内最新一条 id 是否在缓存"做命中判断——缓存中间漏的 post 会永远补不上。

正确做法：每次都先拉一次完整 list，再按 id 与本地 cache **逐条** diff，命中跳过、未命中必须 fetch。

### 标准流程

```
1. scan_cache_window：列出 cache/<handle>/*.json 里 created_at >= SINCE 的全部 id
2. 一次 GQL list 调用（不开 --include-detail，便宜）
   autocli twitter user-posts --username <h> --limit N --format json
3. 过滤到窗口内：list_in_window = list[].select(created_at >= SINCE)
4. diff：need_ids = list_in_window[].id - cached_ids
   - 命中（cache 文件已存在）→ 跳过 fetch
   - 未命中 → 进入步骤 5
5. 把 need_ids 对应的 post 从 list_json 抽出（列表里已含 full_text + media_urls），
   pipe 给 download-media.py 下图，落 cache/<handle>/<id>.json
6. output_window：合并输出 cache 里所有窗口内的 post，按 created_at 倒序
```

### 收益对比

| 场景 | "命中即返回" 旧策略 | list-then-diff |
| --- | --- | --- |
| 冷启动 | 1 次探针 + 多轮翻倍 + 1 次 detail | 1 次 GQL list + 下图 |
| 全部命中 | 1 次探针 → 直接返回 | 1 次 GQL list → 0 次 fetch |
| 中间漏 3 条 | 探针看最新一条已命中 → **永远补不上** | 1 次 GQL list → 精确补 3 条 |
| 长尾命中（缓存有但不全） | 不可靠 | 始终窗口完整 |

### 关键不变量

- **cache 文件 = 已落地证明**：cache/<id>.json 存在就视为命中，不再做内容校验。  
  推论：删 cache 文件 = 强制重拉该 id；`STOCK_INFOS_REFRESH=1` = 全部重拉。
- **list 数据 = 真值来源**：对 list 里出现但本地缺的 id，必须 fetch；list 里没出现的旧 id，保留缓存（可能是上次窗口的，仍可由 `output_window` 输出）。
- **list 容量警戒**：`list_in_window.length == list.length && earliest_ts >= SINCE` 时打 warn——说明 LIST_LIMIT 不够，没拉到 SINCE 之前的 post，需要调大。

### 何时仍要走 detail GQL

list mode 已包含 `note_tweet.full_text`、`legacy.full_text`、`media_urls`、`metrics`，绝大多数场景够用。唯一需要 `--include-detail true` 的情况：
- 长帖 article（`tweet.article.article_results`），list 里只有摘要
- 部分 community-only 字段

把 detail 做成可选开关（比如外层脚本的 `STOCK_INFOS_DETAIL=1`），默认关闭，避免每条都多一次 `TweetResultByRestId` 调用。

---

## 调试与验证

```bash
# 检查 daemon / Chrome extension / 外部 CLI 状态
autocli doctor

# 单条命令带原始输出（不走表格化）
autocli twitter user-posts --username dmjk001 --limit 30 --format json 2>/tmp/err >/tmp/out

# 看 adapter 注入的 diag warning
jq '.[0].warnings // []' /tmp/out

# 清缓存重跑（端到端）
rm -rf <project>/cache/<handle>
bash <project>/scripts/fetch_user.sh <handle>
```

改 adapter 前先备份：

```bash
cp adapters/<site>/<cmd>.yaml adapters/<site>/<cmd>.yaml.bak.$(date +%Y%m%d_%H%M%S)
```

---

## 常见故障 → 处置

| 现象 | 多半原因 | 处置 |
| --- | --- | --- |
| GQL `No ct0 cookie - not logged into x.com` | Chrome 没登录 / 切到了未登录 profile | 用户在 Chrome 登录目标站点后重试 |
| GQL `<Operation> HTTP 404 / 401` | queryId 被站点轮换 | 更新 fallback queryId；优先靠 `resolveQueryId` 自动从 placeholder.json + JS bundle 解析 |
| `articles_in_dom: 5` 但用户实际页面有更多 | autocli tab 在后台被节流 / 虚拟列表没触发 | 切 GQL 路径（不依赖 DOM） |
| `title: ""` + `articles_in_dom: 0` | SPA hydration 未完成就 evaluate | 增大 navigate 步骤的 `settleMs` |
| 命令 60s 超时 | 单次 evaluate 内 sleep 总和过大 | 压缩 sleep；分页搬到多次 evaluate |
| `MAX_PROBE_LIMIT� unbound variable`（外层 bash 脚本） | `set -u` 下 `$VAR` 紧跟中文标点，bash 把多字节首字节当变量名 | 改成 `${VAR}` 包起来 |

---

## 参考实现

- [adapters/twitter/user-posts.yaml](adapters/twitter/user-posts.yaml) — GQL 主路径（`UserByScreenName` → `UserTweets` 分页） + DOM 滚动兜底，diag warning，`since/until/limit` 过滤
- [adapters/twitter/download-media.py](adapters/twitter/download-media.py) — stdin JSON → 下载 media 到本地、回写 `local_media_paths`
- 外层增量缓存脚本（项目侧）：
  - `stock-infos/scripts/fetch_user_v2.sh` — list-then-diff 标准实现，单次 GQL + 严格 id diff，命中跳过、未命中必补
  - `stock-infos/scripts/fetch_user.sh` — v1 旧实现（探针 + 翻倍 + 命中即返回），保留作 GQL 不可用时的对照参考，不再推荐
