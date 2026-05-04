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

### 双路径混合（推荐 adapter 默认形态）

```js
let posts = [];
let usedPath = 'gql';
try {
  posts = await fetchUserTimelineGql(username, limit);
} catch (error) {
  warnings.push(`gql_path_failed: ${error?.message || error}; falling back to scroll`);
  usedPath = 'scroll';
}
if (posts.length === 0) { usedPath = 'scroll'; /* DOM 滚动逻辑 */ }
```

调用方通过 `warnings[]` 里有没有 `gql_path_failed:` 就能知道走的哪条。

---

## DOM 滚动调优要点（兜底路径）

仅当目标站点没有可用 GQL，或本地 ct0 不可用时使用。原则：

- **小步滚动**：`window.scrollBy(0, innerHeight*0.8)`，避免跳到底导致虚拟列表漏渲染。
- **周期到底触发懒加载**：每 N 步做一次 `window.scrollTo(0, scrollHeight)`。
- **多次 harvest**：滚后立刻采一次，再延迟 500-800ms 二采（X 经常分两批渲染）。
- **stagnant 阈值**：`scrollHeight` 不增 + `scrollY+innerHeight ≥ scrollHeight - 300` 才算真停滞，连续 4-6 次再退出。
- **总耗时控制在 daemon 单次 evaluate 超时（~30s）以内**：sleep 别叠加超过 25s，否则触发 cli 重试 + 总 60s 超时。

---

## 时间窗口过滤约定

涉及 `since/until` 的 adapter 统一用 ISO8601 UTC，命令行支持 `YYYY-MM-DD` 简写：
- `since` 不带时间默认补 `T00:00:00.000Z`
- `until` 不带时间默认补 `T23:59:59.999Z`
- 入参非法时 `warnings.push('Ignored invalid ... value')`，不抛错。

cursor 分页里命中 `created_at < since` 时**立即 break**，避免无意义翻页。

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

- [adapters/twitter/user-posts.yaml](adapters/twitter/user-posts.yaml) — GQL 主路径 + DOM 滚动兜底，diag warning，`since/until/limit` 过滤
- [adapters/twitter/download-media.py](adapters/twitter/download-media.py) — stdin JSON → 下载 media 到本地、回写 `local_media_paths`
