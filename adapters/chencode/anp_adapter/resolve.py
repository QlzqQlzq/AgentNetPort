"""模糊目标 → 候选 Agent → canonical Agent ID。

**这一层不是协议，是 Adapter 侧的一个普通库。**

为什么放在 Adapter 侧（而不是 Core 加一个 `/resolve` 端点）：

    Core 负责「把信送给谁」        → 只认 canonical Agent ID
    Adapter 负责「用户说的那个谁到底是谁」 → 模糊、平台语义、候选排序

Core 一旦学会解释「负责 Live2D 的」「昨天那个工作区」，分层就塌了
（Adapter Spec §8 同一条防腐规则）。所以本模块：

  · 只 import 标准库，★不 import 任何 Core 模块、不新增任何 Core API★
  · 数据来源就是 Core 的公开名册 `GET /agents`（免令牌：name / describe /
    skills / caps / last_seen）+ Adapter 自己知道的平台提示（alias / tags /
    workspace title）
  · 匹配是普通字符串算法（大小写/分隔符归一 + 词元命中 + 覆盖率），
    ★不用 AI、不要模型、不联网★
  · 第三方 Adapter 可以完全不支持模糊解析——不调这个模块就行了，
    没有哪条协议要求它必须能做

三个出口（与任务书 §9 的两个阶段对应）：

    RESOLVED   → target = canonical Agent ID，交给 `AnpClient.send()` 发
    AMBIGUOUS  → 返回 candidates，不猜；要上层/用户再指一次
    NOT_FOUND  → 名册里没有匹配（附最接近的几个，仅作提示）

resolve 与 send 是两件事：「不知道发给谁」≠「知道发给谁但投不出去」。
本模块只管前者，投递结果看 send 的返回（wake / attempts / next_retry_at…）。

★两个时间信号必须分清★（2026-09-23 查清，见 Spec §8.6）：

    last_seen   来自 Core 名册 `GET /agents` 的 `last_seen`
                = 账本里该身份最后一条记录的 ts = 心跳 / 续约 / 探活
                （`core.agent_table()[name]["ts"]`，`core.online()` 用的也是它）
                → 语义是「最近活跃」，★不是「最近被使用」★

    last_used   Adapter 自己记的「最近一次真的用过它」（平台侧记录）
                → 只有这个信号才允许用来**破并列**

`/agents` 给不出 last_used：Core 里真正代表"被使用"的是消息状态字段
（`delivered_at` 只在真签收时写 / `read_ts` / `read_by`），它们不在名册里，
聚合需要遍历收件箱。所以 Adapter 要么自己记（推荐），要么明确接受
"只有心跳活跃度"这个更弱的信号——**绝不允许悄悄拿 last_seen 冒充 last_used**。

`last_used` 只用来破并列，绝不覆盖歧义（任务书 §4.2）：只有当候选本来就
已经不相上下、且领先者确实比别人新得多（年龄差 ≥ recency_min_gap 秒 且
至少新 recency_ratio 倍）时，才允许收窄到它。默认参数下"三个候选人都是
5 秒内登记的 Live2D"是 AMBIGUOUS，不是偷偷挑一个。
"""
from __future__ import annotations

import math
import re
import time

__all__ = [
    "STATUSES", "RESOLVED", "AMBIGUOUS", "NOT_FOUND", "INVALID_TARGET",
    "DIRECTORY_UNAVAILABLE", "NETWORK_ERROR", "CONNECTION_FAILED",
    "ANP_OFFLINE", "ANP_MAINTENANCE",
    "Candidate", "Resolution", "LocalDirectory", "build_dir", "resolve",
    "merge_hints", "shape", "now",
]


# --------------------------------------------------------------------------- #
# 解析状态（任务书 §9 的第一阶段；第二阶段的投递结果仍走 send 的返回）
#
# 这些是 **Adapter 侧的解析结论**，不是新的 Core 错误码——Core 一行都不用改。
# --------------------------------------------------------------------------- #
RESOLVED = "RESOLVED"                              # 唯一目标，拿到 canonical id
AMBIGUOUS = "AMBIGUOUS"                            # 多个相近候选，不猜
NOT_FOUND = "NOT_FOUND"                            # 名册里没有匹配
INVALID_TARGET = "INVALID_TARGET"                  # 目标本身不合法（空 / 不是字符串）
DIRECTORY_UNAVAILABLE = "DIRECTORY_UNAVAILABLE"    # 名册取不到（连不上 / 被拒）
NETWORK_ERROR = "NETWORK_ERROR"                    # 传输层错误（保留：Send 阶段的分类）
CONNECTION_FAILED = "CONNECTION_FAILED"            # 连接建立失败（保留）
ANP_OFFLINE = "ANP_OFFLINE"                        # 对端 ANP 不在线（保留）
ANP_MAINTENANCE = "ANP_MAINTENANCE"                # 对端 ANP 维护中（保留）

STATUSES = (RESOLVED, AMBIGUOUS, NOT_FOUND, INVALID_TARGET,
            DIRECTORY_UNAVAILABLE, NETWORK_ERROR, CONNECTION_FAILED,
            ANP_OFFLINE, ANP_MAINTENANCE)

# 后五个是 **reserved**：当前协议里 Core 不会回这几种（`/agents` 免令牌，
# 只有"连不上"和"被拒"两种失败）。留着是为了让 Adapter 的作者有一处统一
# 的地方表达"投递阶段的网络/对端状态"，而不是各自发明字符串。
RESERVED_STATUSES = (NETWORK_ERROR, CONNECTION_FAILED, ANP_OFFLINE, ANP_MAINTENANCE)


def now() -> float:
    return time.time()


def _ts(value) -> float | None:
    """把任意"时间戳样子的东西"折成 float；折不动就是 None（**宁缺勿错**）。

    时间戳异常（字符串、NaN、未来时间）不该被当成"很久以前"或"刚刚"——
    它只是没有信息，交给 `recency()` 忽略掉。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:                    # NaN
        return None
    return out


# --------------------------------------------------------------------------- #
# 归一化与切词
# --------------------------------------------------------------------------- #
# 纯结构性虚词：它们出现在**所有**候选的描述里，不携带任何区分信息。
# 不剔掉的话「负责 Live2D 的」会同时命中每个 agent 的 "负责 xxx"，
# 把唯一候选变成 AMBIGUOUS（这就是"过度匹配"的样子）。
_STOP = frozenset({"的", "了", "是", "在", "和", "与", "或", "那个", "这个", "一个",
                   "负责", "做", "干", "搞", "给", "找", "要", "请", "帮", "把",
                   "和", "跟", "对", "for", "the", "a", "an", "to", "of", "who",
                   "please", "负责的", "相关", "有关"})

_SEP = re.compile(r"[\s\u3000,，。;；:：/\\|()（）\[\]【】<>《》\"'`~!！?？+*#@$%^&=_.\-]+")
# 一段里再切"拉丁串 / 中文串"两类：★中文串不切开★
# （`live2d素材` 要得到 `live2d` + `素材`，而不是 `live2d` + `素` + `材`；
#   中文里两字词是最小语义单位，切碎就再也匹配不上了——实测踩过）
_RUN = re.compile(r"[0-9a-z]+|[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _norm(text: str) -> str:
    """归一：小写 + 去分隔符 + 去空白。

    `agent-8f31` 与 `agent8f31`、`Live2D` 与 `live2d` 归一到同一个串。
    ★中英混排时不能把 CJK 也切碎★：`live2d材质` 要留成一个 token。
    """
    return _SEP.sub("", str(text or "").lower())


def _tokens(query: str) -> list[str]:
    """把查询切成词元：按分隔符切段，每段再切"拉丁串 / 中文串"。

    `负责 Live2D 的` → ["live2d"]（"负责""的"是虚词）
    `live2d 素材`    → ["live2d", "素材"]
    `minecraft材质`  → ["minecraft"]（"材质"与 "minecraft" 连在一段里，
                        按"位置重叠优先"归给拉丁串）
    """
    text = str(query or "").strip().lower()
    if not text:
        return []
    out: list[str] = []
    for part in _SEP.split(text):
        if not part:
            continue
        for run in _RUN.findall(part):
            if _CJK.search(run):
                continue                      # 同一段里的中文串归给拉丁串，见上
            out.append(run)
        if not _RUN.search(part):
            out.append(_norm(part))
        elif not any(not _CJK.search(r) for r in _RUN.findall(part)):
            # 整段都是中文：原样作为一个词元（`立绘整理` 不切碎）
            out.append(_norm(part))
    # 去虚词、去空、去重，保持顺序
    seen: list[str] = []
    for tok in out:
        tok = tok.strip()
        if not tok or tok in _STOP or tok in seen:
            continue
        seen.append(tok)
    return seen


# --------------------------------------------------------------------------- #
# 字段权重
#
# 顺序就是"可信度顺序"：名字 > alias > skills/caps > description。
# 一张表列全，谁想调优先级改这一处，不去翻散落的 if。
# --------------------------------------------------------------------------- #
FIELD_WEIGHTS: dict[str, tuple[float, float]] = {
    # 字段            精确词元   子串命中
    "name":         (12.0,      8.0),
    "alias":        (12.0,      8.0),
    "handle":       (10.0,      6.0),      # 平台侧短名（DSH 的 dsh-11451 这类）
    "skill":        (8.0,       6.0),
    "cap":          (4.0,       2.5),
    "workspace":    (6.0,       4.0),      # 平台工作区标题（Adapter 自己知道）
    "description":  (4.0,       2.1),
    "tag":          (5.0,       3.0),
    "client":       (5.0,       3.0),      # 平台名（codex / dsh / mimo）
    "agent_id":     (12.0,      6.0),
}

_MIN_COVERAGE = 0.5      # 子串命中要求"这个词元自己占整个查询的一半以上"
_MIN_FIELD_LEN = 4       # 字段值短于这个长度时，不再接受子串命中


def _match(value: str, tokens: list[str], scale: int) -> dict:
    """一个字段值对一组词元的命中情况：`{token: "exact" | "sub"}`。

    三类命中，都是普通字符串比较（无 AI）：

      ① 完全相同              `live2d` == `live2d`（skill 里就是声明的写法）
      ② 字段值 ⊂ 词元         `立绘` ⊂ `立绘稿`（别名/简称比用户写的短）
      ③ 词元 ⊂ 字段值         `live2d` ⊂ `live2d素材整理`（词出现在描述里）

    ★③ 的两道闸门，全是实测踩出来的★：

      · **字段值不能比查询短太多**（`_MIN_FIELD_LEN`）：曾经用
        `len(tok) / len(字段值)` 做覆盖率，于是查询 `Live2D`（6 字）会命中
        `demo-mc-01`（归一后 8 字）——6/8 ≥ 0.5，短字段被长词元"覆盖"。
        现在这类一律不算：谁都不会拿 `demo-mc-01` 当 Live2D 的候选。
      · **词元要占查询的一半以上**（`_MIN_COVERAGE`）：查询 `live2d 素材` 里
        `素材`（2/9）不单独算证据——它由 ② 那条路（工作区标题/别名整词相等）
        或更具体的说法来承载，否则两个字的词会在任何描述里到处命中。
    """
    text = _norm(value)
    if not text:
        return {}
    hits: dict[str, str] = {}
    for tok in tokens:
        if len(tok) < 2:
            continue                          # 单字不作证据（"的""整"这类到处都是）
        if tok == text:
            hits[tok] = "exact"
            continue
        if len(text) >= 2 and text in tok:    # ② 别名/简称比用户写的短
            hits[tok] = "sub"
            continue
        if len(text) >= _MIN_FIELD_LEN and tok in text:
            # ③ 词元出现在字段值里。覆盖率要求 0.5，但**两字中文词豁免**——
            # 中文里两字词就是最小语义单位，`素材`(2/9) 不该因为比值小被丢掉。
            # 拉丁词元（live2d、minecraft）没有这个豁免：它们本来就能写长。
            if _CJK.search(tok) and len(tok) >= 2:
                hits[tok] = "sub"
            elif len(tok) / max(scale, 1) >= _MIN_COVERAGE:
                hits[tok] = "sub"
    return hits


# 词元"区分力"的两个边界：
#   FLOOR 一个词元即使出现在所有候选里也不归零（只用一个词查时必须还能判定"命中了"）
#   CAP   再稀有的词元也不让它一票定案（前四名各命中一个不同词时仍应判 AMBIGUOUS）
_IDF_FLOOR = 0.25
_IDF_CAP = 2.5


def _token_weights(cands: list, tokens: list[str], scale: int) -> dict[str, float]:
    """每个词元的**区分力**权重：命中的候选越少，越能说明问题。

    ★为什么必须有这一步★（实测踩过）：查询 `live2d 素材` 时，三个候选都声明了
    skill `live2d`（命中 = 当然的），只有 `demo-live2d-b` 的描述里有「素材」。
    不加区分力的话三者同分 → 被"最近使用"强行挑走，而正确答案其实唯一。
    这正是任务书 §4.2 那句话的反面：
    **最近使用只能在真正并列时排序，不能替一个本来能区分的查询做决定。**

    权重 = 1 + log2(1 + N / df) 的**归一化**版本：`w = (raw - r3) / (r1 - r3)`，
    其中 r1/r3 是"只命中 1 个候选"和"命中 3 个候选"时的原始值。

      · 命中全体（df=N）  → 0.25（比如 4 个候选里 4 个都声明 live2d）
      · 命中一半          → 0.75
      · 只命中一个        → 1.0 起，最稀有的封顶 2.5

    为什么不用原始的 `1 + log2(1 + N/df)`：N=4 时它在 df=1..4 只落在
    [2.0, 2.58]，几乎区分不出"人人都命中"和"只有它命中"（实测：加了等于没加）。
    """
    n = len(cands)
    if n <= 1:
        return {tok: 1.0 for tok in tokens}

    def raw(df: int) -> float:
        return 1.0 + math.log2(1.0 + n / max(df, 1))

    r1, r3 = raw(1), raw(3)
    span = (r1 - r3) or 1.0
    out: dict[str, float] = {}
    for tok in tokens:
        df = 0
        for c in cands:
            if any(_match(v, [tok], scale) for vs in c.fields().values() for v in vs):
                df += 1
        w = (raw(df) - r3) / span if df <= 3 else raw(df) - r1
        out[tok] = round(min(max(_IDF_FLOOR + w, _IDF_FLOOR), _IDF_CAP), 4)
    return out


def _score_candidate(fields: dict[str, list[str]], tokens: list[str], scale: int,
                     weights: dict[str, float]) -> tuple[float, float, dict, list[str]]:
    """一个候选的得分 + 证据。

    返回 `(raw, weighted, evidence, reasons)`：

        raw       未加区分力的命中强度 —— 用来判"到底有没有真的匹配上"（门槛）
        weighted  加了区分力的强度     —— 用来判"谁更说得通"（并列 / 领先）

    ★两个数分开是有意的★：门槛问的是"命中了吗"，比较问的是"这个命中算不算证据"。
    合成一个数会出现这种荒谬结果：查询 `Live2D` 时三个候选都命中 `live2d`，
    区分力把它压到接近 0，于是**"确实命中了"变成"什么都没匹配上"→ NOT_FOUND**。
    """
    raw_total = 0.0
    weighted_total = 0.0
    evidence: dict[str, dict] = {}
    reasons: list[str] = []
    covered: set[str] = set()
    for field, (w_exact, w_sub) in FIELD_WEIGHTS.items():
        values = fields.get(field) or []
        best: dict[str, str] = {}
        for value in values:
            hits = _match(str(value), tokens, scale)
            # 同一字段的多个值取并集：agent_id 与 name 都命中同一个词只算一次
            for tok, kind in hits.items():
                if best.get(tok) != "exact":
                    best[tok] = kind
        if not best:
            continue
        field_raw = 0.0
        field_weighted = 0.0
        for tok, kind in best.items():
            w = weights.get(tok, 1.0)
            if kind == "exact":
                field_raw += w_exact
                field_weighted += w_exact * w
            else:
                field_raw += w_sub
                field_weighted += w_sub * w
        raw_total += field_raw
        weighted_total += field_weighted
        evidence[field] = {"score": round(field_weighted, 2),
                           "tokens": dict(best),
                           "weight": {t: weights.get(t, 1.0) for t in best}}
        for tok, kind in sorted(best.items()):
            reasons.append(f"{field}≈{tok}" + ("(整词)" if kind == "exact" else "(子串)"))
        covered.update(best)

    # ★多词元查询要求"词元覆盖率"★（实测踩过）：查询 `live2d 素材` 时，
    # demo-live2d-b 命中两个词、其余候选只命中 `live2d` 一个词。若只看分数，
    # "只中一个词"的候选照样能靠 `live2d` 拿高分，领先优势被稀释到 margin 以下，
    # 于是答案被交给"最近使用"去定——而它本来就该由"素材"定。
    # 规则：命中词元数必须 ≥ 总词元数的一半（3 个词里中 2 个 = 够）。
    if len(tokens) >= 2:
        need = (len(tokens) + 1) // 2
        if len(covered) < need:
            return 0.0, 0.0, {}, []
    return round(raw_total, 2), round(weighted_total, 2), evidence, reasons


# --------------------------------------------------------------------------- #
# 候选
# --------------------------------------------------------------------------- #
class Candidate:
    """一个候选 Agent。

    `agent_id` 就是 canonical ANP Agent ID（= Core 名册里的 `name`）。
    Adapter 侧的平台提示（alias / tags / workspace / handle）只存在于 Adapter，
    Core 一个字节都看不到它们——这正是 Core/Adapter 的边界。

    ★两个时间字段语义不同，不许混用★：

        last_seen       最近**活跃**（名册的 last_seen：心跳/续约）—— 弱信号
        last_used       最近**被真的用过**（Adapter 的平台记录）—— 强信号，可破并列
        last_used_source  这个 last_used 是哪来的：'platform' | 'heartbeat' | ''
    """

    __slots__ = ("agent_id", "name", "handle", "aliases", "description", "skills",
                 "caps", "tags", "workspace", "client",
                 "last_used", "last_used_source", "last_seen", "extra")

    def __init__(self, agent_id: str = "", *, name: str = "", handle: str = "",
                 aliases=(), description: str = "", skills=(), caps=None,
                 tags=(), workspace: str = "", client: str = "",
                 last_used: float | None = None, last_used_source: str = "",
                 last_seen: float | None = None, extra: dict | None = None):
        self.agent_id = str(agent_id or "").strip()
        self.name = str(name or self.agent_id)
        self.handle = str(handle or "")
        self.aliases = [str(a) for a in (aliases or []) if str(a or "").strip()]
        self.description = str(description or "")
        self.skills = [str(s) for s in (skills or []) if str(s or "").strip()]
        self.caps = caps if isinstance(caps, dict) else {}
        self.tags = [str(t) for t in (tags or []) if str(t or "").strip()]
        self.workspace = str(workspace or "")
        self.client = str(client or "")
        self.last_used = _ts(last_used)
        self.last_used_source = str(last_used_source or
                                    ("platform" if self.last_used is not None else ""))
        self.last_seen = _ts(last_seen)
        self.extra = extra or {}

    def recency(self, allow_seen: bool = False) -> tuple[float | None, str]:
        """给排序/破并列用的"最近"信号：`(时间戳, 来源)`。

        ★优先级是写死的★：真·last_used（platform）永远压过 last_seen。
        `allow_seen=False`（默认）时只认 platform —— 心跳活跃度不足以让人替用户
        猜一个目标；要退而求其次，调用方必须显式打开它（`recency_allow_seen=True`）。
        """
        if self.last_used is not None and self.last_used_source == "platform":
            return self.last_used, "platform"
        if allow_seen and self.last_used is not None:
            return self.last_used, self.last_used_source or "heartbeat"
        if allow_seen and self.last_seen is not None:
            return self.last_seen, "heartbeat"
        return None, ""

    def fields(self) -> dict[str, list[str]]:
        """各字段的取值。★同一个字符串只出现一次★——

        `name` 与 `agent_id` 平时是同一个串（`demo-live2d-a`），不去重会让
        "名字里带 live2d" 被算两遍，分数虚高一倍（实测踩过）。
        字段按权重从高到低排列，重复值留给权重最高的那个字段。
        """
        raw = {
            "name": [self.name],
            "alias": self.aliases,
            "handle": [self.handle] if self.handle else [],
            "skill": self.skills,
            "cap": [str(v) for value in self.caps.values() if isinstance(value, (list, tuple))
                    for v in value if str(v)]
                   + [str(k) for k, v in self.caps.items() if v],
            "workspace": [self.workspace] if self.workspace else [],
            "description": [self.description] if self.description else [],
            "tag": self.tags,
            "client": [self.client] if self.client else [],
            "agent_id": [self.agent_id],
        }
        out: dict[str, list[str]] = {}
        claimed: set[str] = set()
        for field, _weights in FIELD_WEIGHTS.items():
            values: list[str] = []
            for value in raw.get(field) or []:
                key = _norm(str(value))
                if not key or key in claimed:
                    continue
                claimed.add(key)
                values.append(str(value))
            if values:
                out[field] = values
        return out

    def view(self, score: float = 0.0, reasons=(), evidence=None) -> dict:
        out = {"agent_id": self.agent_id, "name": self.name, "score": score,
               "description": self.description, "skills": list(self.skills),
               "client": self.client, "workspace": self.workspace,
               "last_used": self.last_used, "last_used_source": self.last_used_source,
               "last_seen": self.last_seen, "reasons": list(reasons)}
        if evidence:
            out["evidence"] = evidence
        if self.aliases:
            out["aliases"] = list(self.aliases)
        if self.tags:
            out["tags"] = list(self.tags)
        return out


# --------------------------------------------------------------------------- #
# 目录
# --------------------------------------------------------------------------- #
def build_dir(roster: list[dict], hints: dict | None = None) -> list[Candidate]:
    """Core 公开名册（`GET /agents`）+ Adapter 自己的平台提示 → 候选表。

    `roster` 的每一项就是 `/agents` 返回的那一行（`name` / `description` /
    `skills` / `caps` / `last_seen`…）。**Core 不需要为这个功能改一个字段**：
    这些正是它本来就对外提供的公开身份信息。

    `hints` 按 canonical agent_id 索引，Adapter 自己维护（见 `LocalDirectory`）：

        {"demo-live2d-a": {"aliases": ["立绘"], "handle": "dsh-11451",
                           "tags": ["art"], "workspace": "Live2D 研究",
                           "last_used": 1790...}}      ← ★平台自己的"最近用过"记录★

    ★`last_seen` 只来自 `roster`，`last_used` 只来自 `hints`——两者不互相代填★：

        roster["last_seen"]  → Candidate.last_seen      最近活跃（心跳）
        hints["last_used"]   → Candidate.last_used      最近被用过（平台记录）

    没有平台记录时 `last_used` 就是 None（`recency()` 会退到 last_seen，
    但那要调用方显式允许）。
    """
    hints = hints or {}
    out: list[Candidate] = []
    for row in roster or []:
        if not isinstance(row, dict):
            continue
        aid = str(row.get("name") or row.get("agent_id") or "").strip()
        if not aid:
            continue
        h = hints.get(aid) or {}
        out.append(Candidate(
            aid,
            name=str(row.get("display_name") or h.get("name") or aid),
            handle=str(h.get("handle") or ""),
            aliases=h.get("aliases") or [],
            description=str(row.get("description") or h.get("description") or ""),
            skills=row.get("skills") or h.get("skills") or [],
            caps=row.get("caps") or h.get("caps") or {},
            tags=h.get("tags") or [],
            workspace=str(h.get("workspace") or ""),
            client=str(h.get("client") or ""),
            last_used=h.get("last_used"),            # ★只有 Adapter 能提供★
            last_used_source=str(h.get("last_used_source") or "platform"),
            last_seen=row.get("last_seen"),          # ★名册只给这个★
        ))
    return out


class LocalDirectory:
    """Adapter 自己知道、而 Core 不该知道的那些东西。

    平台工作区标题、短名字、同义词、标签——**全部留在 Adapter 进程里**。
    Core 永远看不到、也不需要看到它们（任务书 §6/§7/§8）。
    """

    def __init__(self, path=None):
        self.path = path
        self._hints: dict[str, dict] = {}

    def add(self, agent_id: str, **hint) -> "LocalDirectory":
        aid = str(agent_id or "").strip()
        if not aid:
            raise ValueError("agent_id 不能为空")
        slot = self._hints.setdefault(aid, {})
        for key, value in hint.items():
            if value is None:
                continue
            if key in ("aliases", "tags", "skills") and isinstance(value, (list, tuple, set)):
                merged = list(slot.get(key) or [])
                for item in value:
                    if str(item or "").strip() and str(item) not in merged:
                        merged.append(str(item))
                slot[key] = merged
            else:
                slot[key] = value
        self._save()
        return self

    def hints(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in self._hints.items()}

    def get(self, agent_id: str) -> dict:
        return dict(self._hints.get(str(agent_id or "").strip()) or {})

    # ---- 落盘（Adapter 自己的状态；丢了不要紧，能从 Core 恢复的才是真相） ----
    def _save(self) -> None:
        if not self.path:
            return
        import json
        from pathlib import Path
        p = Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(self._hints, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(p)

    @classmethod
    def load(cls, path) -> "LocalDirectory":
        import json
        from pathlib import Path
        d = cls(path)
        p = Path(path)
        if p.is_file():
            try:
                raw = json.loads(p.read_text(encoding="utf-8") or "{}")
                d._hints = {str(k): dict(v) for k, v in raw.items()
                            if isinstance(v, dict)}
            except (json.JSONDecodeError, OSError):
                d._hints = {}
        return d


def merge_hints(*dirs) -> dict[str, dict]:
    """合并若干份 hints（后面的覆盖前面的，列表字段取并集）。"""
    out: dict[str, dict] = {}
    for src in dirs:
        if src is None:
            continue
        entries = src.hints() if isinstance(src, LocalDirectory) else dict(src)
        for aid, hint in entries.items():
            slot = out.setdefault(str(aid), {})
            for key, value in (hint or {}).items():
                if key in ("aliases", "tags", "skills") and isinstance(value, (list, tuple)):
                    merged = list(slot.get(key) or [])
                    for item in value:
                        if item not in merged:
                            merged.append(item)
                    slot[key] = merged
                else:
                    slot[key] = value
    return out


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
class Resolution(dict):
    """解析结果（dict 子类，方便直接 `json.dumps` / 解包）。

    字段：

        status        RESOLVED / AMBIGUOUS / NOT_FOUND / INVALID_TARGET / …
        query         原始输入（原样回显，便于日志与 UI）
        target        canonical Agent ID（只有 RESOLVED 时非空）
        candidates    候选列表（AMBIGUOUS / NOT_FOUND 时给）
        reason        一句话解释（人看的）
        decided_by    "exact" | "unique" | "margin" | "recency" | ""
        resolve_stage 恒为 "resolve"——提醒调用方：**这不代表投递成功**
        detail        DIRECTORY_UNAVAILABLE 之类的补充说明
    """

    @property
    def status(self) -> str:
        return str(self.get("status") or "")

    @property
    def target(self) -> str:
        return str(self.get("target") or "")

    @property
    def ok(self) -> bool:
        return self.status == RESOLVED

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"Resolution(status={self.status!r}, target={self.target!r}, "
                f"candidates={len(self.get('candidates') or [])})")


def _make(status: str, query: str, **kw) -> Resolution:
    base = {"status": status, "query": query, "target": "",
            "candidates": [], "reason": "", "decided_by": "",
            "resolve_stage": "resolve", "detail": ""}
    base.update(kw)
    return Resolution(base)


def _order_key(cand: Candidate, allow_seen: bool):
    """排序键里那个"最近"分量：拿不到信号的一律排最后（-inf），不用 0 冒充。"""
    ts, _src = cand.recency(allow_seen)
    return -(ts if ts is not None else float("-inf"))


def resolve(query, candidates, *, strong_score: float = 8.0,
            top_floor: float = 4.0, margin: float = 1.15,
            strong_ratio: float = 0.8,
            recency_min_gap: float = 30.0, recency_ratio: float = 2.0,
            recency_allow_seen: bool = False,
            clock=now, limit: int = 8, ) -> Resolution:
    """模糊目标 → 候选 → canonical Agent ID。**纯字符串算法，无 AI。**

    判定顺序（每一步都能解释，没有黑箱）：

        0. 输入为空 / 非字符串                  → INVALID_TARGET
        1. 归一后与某个 Agent ID 完全相同        → RESOLVED（精确 ID 永远优先：
                                                  "输入 agent-8f31…"走的就是这条）
        2. 逐字段打分，按**词元区分力**加权       → (raw, weighted) 两个数
        3. raw < top_floor                      → NOT_FOUND（附最接近的，仅作提示）
        4. 只有它一个够格                       → RESOLVED(unique)
        5. weighted 领先 ≥ margin 倍            → RESOLVED(margin)
        6. 前两名不相上下 + 领先者**确实最近被用过**
           （last_used，平台记录）               → RESOLVED(recency)
        7. 其余                                 → AMBIGUOUS（给候选，不猜）

    ★第 6 步只认 last_used（Adapter 的平台记录），默认不认 last_seen★：

        last_seen  = 心跳活跃度（"它还活着"），不是"它最近被用过"。
                     只有心跳的 Adapter 不该据此替用户挑人 → 默认回到 AMBIGUOUS。
                     真要退而求其次，显式传 recency_allow_seen=True，
                     那时 decided_by 会写明 "recency.heartbeat"。

    ★`last_used` 只用来破并列，绝不覆盖歧义★（任务书 §4.2）：要求"本来就不相上下
    + 明显更新"两个条件同时成立。第 5 步排在前面的意义是：
    **本来能区分的查询不许被"最近用过"改写答案。**
    """
    if not isinstance(query, str):
        return _make(INVALID_TARGET, str(query),
                     reason=f"目标必须是字符串，收到 {type(query).__name__}")
    query = query.strip()
    if not query:
        return _make(INVALID_TARGET, "", reason="目标为空：没有可解析的对象")

    cands = [c for c in (candidates or []) if getattr(c, "agent_id", "")]
    if not cands:
        return _make(DIRECTORY_UNAVAILABLE, query,
                     reason="候选名册为空：Adapter 拿不到任何 Agent（检查 GET /agents）",
                     detail="directory.empty")

    # ---- 1. 精确 canonical ID（大小写/分隔符归一后相同也算精确） ----
    key = _norm(query)
    exact = [c for c in cands if _norm(c.agent_id) == key]
    if len(exact) == 1:
        c = exact[0]
        return _make(RESOLVED, query, target=c.agent_id, decided_by="exact",
                     reason=f"{query} 就是 canonical Agent ID",
                     candidates=[c.view(score=100.0, reasons=["agent_id 精确匹配"])])

    tokens = _tokens(query)
    if not tokens:
        return _make(INVALID_TARGET, query,
                     reason="目标去掉虚词后没有剩下任何可用于匹配的词")

    # ---- 2. 打分（区分力加权） ----
    scale = max((len(t) for t in tokens), default=2)
    weights = _token_weights(cands, tokens, scale)
    scored: list[tuple[float, float, Candidate, list[str], dict]] = []
    for c in cands:
        raw, weighted, evidence, reasons = _score_candidate(
            c.fields(), tokens, scale, weights)
        if raw > 0:
            scored.append((raw, weighted, c, reasons, evidence))
    # 排序：区分力强的在前；同分时"最近"的在前；再同就按 agent_id 稳定排序（可复现）。
    # ★这里的"最近"分量默认只认真 last_used★，不用 last_seen 顶替（见 Candidate.recency）。
    scored.sort(key=lambda row: (-row[1], _order_key(row[2], recency_allow_seen),
                                 row[2].agent_id))
    views = [c.view(score=w, reasons=r, evidence=e) for _raw, w, c, r, e in scored]

    # 门槛只看"有没有真的命中"（raw），不看区分力——否则"所有候选都命中的那个词"
    # 会把整个查询压成 NOT_FOUND。
    if not scored or scored[0][0] < top_floor:
        return _make(NOT_FOUND, query,
                     reason=(f"名册里没有匹配「{query}」的 Agent"
                             + (f"（最接近的是 {scored[0][2].agent_id}，"
                                f"命中强度 {scored[0][0]} < {top_floor}）" if scored else "")),
                     candidates=views[:limit])

    raw_top, w_top, top, top_reasons, top_evidence = scored[0]

    # ---- 4. 只有它一个够格 ----
    if len(scored) == 1:
        return _make(RESOLVED, query, target=top.agent_id, decided_by="unique",
                     reason=f"唯一匹配：{top.agent_id}（命中强度 {raw_top}）",
                     candidates=[top.view(score=w_top, reasons=top_reasons,
                                          evidence=top_evidence)])

    w_second, second = scored[1][1], scored[1][2]

    # ---- 5. 区分力上的领先优势（"本来就能区分"的查询在这里定案） ----
    if w_second <= 0 or w_top >= w_second * margin:
        return _make(RESOLVED, query, target=top.agent_id, decided_by="margin",
                     reason=(f"领先第二名 {second.agent_id} 足够多"
                             f"（加权 {w_top} vs {w_second}）"),
                     candidates=[v for v in views if v["score"] >= max(w_second, 0.01)][:limit])

    # ---- 6. 破并列：只认"最近被用过"（last_used），且要求"确实不相上下 + 明显更新" ----
    tied = [row for row in scored if row[1] >= max(w_top * strong_ratio, 1e-9)]
    if len(tied) >= 2 and w_top >= strong_score * 0.25:
        t = clock()
        # ★并列的每个候选都得有"同一来源"的最近信号，才允许用它破并列★：
        #   · 全都有 platform last_used → 可以用（真·最近用过）
        #   · 只有一部分有              → 不许用。缺信号的人会被当成"永远没用过"，
        #                                 选出来的其实是"唯一有记录的那个"，不是最近的
        #   · 都只有 last_seen（心跳）  → 默认不许用（那是活跃度，不是"用过"）；
        #                                 recency_allow_seen=True 才允许
        signals = [c.recency(recency_allow_seen) for _r, _w, c, _rs, _ev in tied]
        ages = [(t - ts) if ts is not None else None for ts, _src in signals]
        srcs = {src for _ts, src in signals}
        usable = all(a is not None and a >= 0 for a in ages) and len(srcs) == 1
        if usable:
            order_ids = sorted(range(len(tied)), key=lambda i: ages[i])
            i_top, i_second = order_ids[0], order_ids[1]
            top_age, second_age = ages[i_top], ages[i_second]
            gap = abs(second_age - top_age)
            ratio = (second_age / top_age) if top_age > 0 else float("inf")
            if gap >= recency_min_gap and (ratio >= recency_ratio or top_age <= 0):
                c = tied[i_top][2]
                other = tied[i_second][2].agent_id
                src = signals[i_top][1]
                # ★措辞跟着信号走，不许夸大★：
                #   platform  = 平台记的"最近用过" → 可以说"最近用过"
                #   heartbeat = 只是心跳活跃度     → 只能说"最近活跃"（且默认走不到这里）
                why = (f"{c.agent_id} 最近用过（比 {other} 近 {gap:.0f}s）" if src == "platform"
                       else f"{c.agent_id} 最近活跃（比 {other} 近 {gap:.0f}s；"
                            f"★这是心跳活跃度，不是「用过」★）")
                return _make(RESOLVED, query, target=c.agent_id,
                             decided_by=f"recency.{src}",
                             reason=f"{len(tied)} 个候选不相上下，但 {why}；信号={src}",
                             candidates=views[:limit])

    # 走到这里就是"不猜"。候选表仍是按（区分力，最近）排过序的——
    # 排序只帮人看，不替人决定。
    strong_views = [v for v in views if v["score"] >= w_top * strong_ratio]
    return _make(AMBIGUOUS, query,
                 reason=(f"{len(strong_views)} 个候选不相上下（加权 {w_top} / {w_second}），"
                         f"最近使用不足以区分——不猜"),
                 candidates=(strong_views or views)[:limit])


def shape(result) -> dict:
    """把结果收成"可直接返回给上层/模型"的最小形状（保持 resolve/send 两段分明）。"""
    status = str(result.get("status") or "")
    out = {"stage": "resolve", "status": status, "query": str(result.get("query") or ""),
           "reason": str(result.get("reason") or "")}
    if status == RESOLVED:
        out["agent_id"] = str(result.get("target") or "")
        out["decided_by"] = str(result.get("decided_by") or "")
        out["next"] = "把 agent_id 交给 send() —— 解析成功不等于投递成功"
    elif status == AMBIGUOUS:
        out["candidates"] = [
            {k: v for k, v in c.items() if k in ("agent_id", "name", "score",
                                                 "description", "skills",
                                                 "last_used", "last_used_source",
                                                 "last_seen", "reasons")}
            for c in (result.get("candidates") or [])]
        out["next"] = "让用户/上层从 candidates 里指一个，再 resolve 一次"
    elif status == NOT_FOUND:
        out["candidates"] = [
            {k: v for k, v in c.items() if k in ("agent_id", "name", "score")}
            for c in (result.get("candidates") or [])]
        out["next"] = "没有这个 Agent：换关键词，或先确认它注册过"
    else:
        out["detail"] = str(result.get("detail") or "")
        out["next"] = "解析阶段就失败了，不要进入 send"
    return out
