# ANP 全局只读管理 API（WebUI 数据面）

> 状态：**已实现**（`GET /admin/messages`）  
> 范围：给 WebUI / 管理 App 用的**全局只读**消息浏览。  
> 本阶段：**不迁 SPA**。  
> 约束：只读、不 mark_read、不发信、不改投递状态。

---

## 1. 评估：扩展 `/messages/query` 是否足够？

**结论：不够。不要把全局浏览塞进 `/messages/query`。**

| 维度 | `/messages/query`（现状） | WebUI 需要的全局浏览 | 冲突？ |
|---|---|---|---|
| 主键 | **必须** `correlation_id` | 全部收件箱、可无关联线 | ✅ 冲突 |
| 数据源 | `journals/<cid>.jsonl` | 各 `inbox/<name>.jsonl` | ✅ 冲突 |
| 覆盖面 | 仅「有人用该 id 发过信」的线 | **无 correlation 的私信也必须可见** | ✅ 冲突 |
| 授权 | **owner 作用域**（创建者才能读） | **admin 全局** | ✅ 冲突 |
| 过滤 | 无 inbox/unread/全文搜索 | inbox / from / unread / q / kind… | ✅ 冲突 |
| 协议定位 | canonical Adapter API（§6.1） | 管理面，不是 Agent API | ✅ 冲突 |

### 1.1 为何不能「加个 admin 参数」糊弄过去

1. **owner 作用域是协议不变量**（Adapter Spec / Plugin Spec / `query_journal` 注释）。在 canonical API 上叠 admin 全局模式，第三方会误以为 Agent 也能全局查信。  
2. **无关联线的消息根本不在 journal**。扩了 `/messages/query` 仍扫不到 WebUI 要的大部分正文。  
3. **鉴权模型分叉**：现在 `POST /messages/query` 走 `_auth(from)`（身份密钥），与 `/sessions` 的 admin 令牌不是一条门。错误文案里的「管理员请用 /sessions 同款令牌」在实现上**并未**给 `/messages/query` 开 admin 旁路——再扩只会把这处文案债做成真漏洞。  
4. **职责**：`/messages/query` =「一条协作线的过程回放」；管理 API =「运维/看板的信箱全景」。

### 1.2 `/messages/query` 保持不变

- 仍要求 `correlation_id`  
- 仍 **owner only**（`sender == "hub"` 内部除外）  
- 仍可选 `state=true` 附投递状态  
- **不**增加 `admin` / `all` / 无 cid 模式  

---

## 2. 设计：`GET /admin/messages`

### 2.1 定位

| 项 | 内容 |
|---|---|
| 能力 | **全局只读**消息浏览（跨所有收件箱） |
| 调用方 | 管理 App / WebUI（**不是** Agent / Adapter canonical API） |
| 鉴权 | **管理员令牌**（与 `/admin/panel`、`/sessions` 同门） |
| 副作用 | **无**（不签收、不重投、不改状态） |
| 与其它 API | 策略/完整性 → `GET /health`；身份 → `GET /agents`；插件面板 → `GET /admin/panel*` |

### 2.2 请求

```http
GET /admin/messages?inbox=&from=&to=&unread=1&kind=&correlation_id=&q=&limit=200&offset=0&sig=1
x-anp-admin: <admin token>
# 或 x-mailbox-key: <admin token> 或 ?token=<admin token>
```

| 参数 | 含义 | 默认 |
|---|---|---|
| `inbox` | 只看某收件箱（身份名） | 全部 |
| `from` | 发信人精确匹配 | — |
| `to` | 收信人精确匹配（信封 `to` 字段；多人信按原始值） | — |
| `unread` | `1`/`true` 只看未读 | 全部 |
| `kind` | 信封 `kind` 精确匹配 | — |
| `correlation_id` | 关联线精确匹配 | — |
| `q` | 子串搜索（大小写不敏感）：subject / body / from / to / id / meta.context_id | — |
| `limit` | 返回条数上限 | `200`（硬顶 `1000`） |
| `offset` | 跳过条数（按 `ts` 降序后的偏移） | `0` |
| `sig` | `0` 关闭验签（省 CPU；默认开） | `1` |

### 2.3 响应

```jsonc
{
  "ok": true,
  "mode": "admin",
  "service": "AgentNetPort-hub",
  "total": 123,          // 过滤后命中总数
  "returned": 50,        // 本页条数
  "offset": 0,
  "limit": 200,
  "messages": [
    {
      "inbox": "codex",           // 所在收件箱
      "id": "m-…", "ts": 1…, "from": "…", "to": "…",
      "subject": "…", "body": "…", "kind": "message",
      "reply_to": null, "correlation_id": "…",
      "meta": { },
      "read": false, "read_by": null, "read_ts": null,
      "attempts": 1, "delivered_at": …, "next_retry_at": …, "last_error": null,
      "replied_at": null, "replied_by": null,
      "claimed_at": null, "claimed_by": null,
      "wake_skipped": true,        // true | "no_push_target" | 缺省
      "sig": "…",
      "sig_ok": true,              // sig=0 时不出现
      "sig_why": "ok"
    }
  ],
  "tampered": ["m-…"],            // 本页验签失败的 id
  "totals": {                     // 全库 inbox_health 口径（未过滤）
    "messages": 0, "unread": 0, "undelivered": 0,
    "retry_due": 0, "claimed": 0, "replied": 0, "cached": 0
  }
}
```

### 2.4 明确不做

- 不提供写接口（ack / redeliver / send）  
- 不暴露 `keys.json` / `hub.secret` / 他人 token  
- 不把本 API 放进 Adapter canonical 8 接口  
- 不在 `/messages/query` 上开后门  

---

## 3. 实现落点

| 层 | 位置 | 职责 |
|---|---|---|
| Core | `anp_core.browse_messages()` | 扫 inbox、过滤、排序、分页、可选验签 |
| Hub | `GET /admin/messages` | admin 鉴权 + query 解析 + 调 core |
| 测试 | `tests/selftest_admin_api.py` | 鉴权 / 过滤 / 无 cid 覆盖 / 只读 / 与 query 隔离 |

WebUI 侧数据拼装（迁移 SPA 时）：

```text
GET /admin/messages     → 列表 + 详情 + 体检 totals
GET /agents             → 身份灯（含 unread/process_api/caps/skills）
GET /health             → policy / integrity
GET /admin/panel*       → 插件面板
```

不再直读 `inbox/` / `ledger.jsonl`。

---

## 4. 验收

- [x] 评估写明 `/messages/query` 不扩  
- [x] 无 `correlation_id` 的信能出现在 `/admin/messages`  
- [x] 无 admin → 401  
- [x] 错误 admin → 401  
- [x] `unread` / `inbox` / `from` / `q` 过滤正确  
- [x] 调用前后 `read` / `delivered_at` / `attempts` 不变  
- [x] `/messages/query` 行为与 owner 作用域不变  
