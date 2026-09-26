"""差异发现根因聚合。

同一份审查的扁平发现列表里，"父节点整体替换 / 删除"通常会连带刷出一串
后代差异（例如响应 schema 由 object 变为 string 时，其下每个字段都会再报
一条 property-removed）。本模块把这些后代发现**吸收**到父级主因上，构建
有向影响组（directed impact groups）：

- 分桶键：``(endpoint, direction)`` —— 端点各自成组；请求 / 响应方向
  （以及 none / shared）严格独立，绝不跨方向吸收；
- 祖先关系：以发现项的文档内 JSON Pointer 判定（按 token 前缀，正确处理
  ``~0`` / ``~1`` 转义与数组下标），删除类发现用旧版指针、其余用新版指针；
- 吸收规则：只有"整体替换 / 整体删除"语义的规则可以作为父因（见
  ``REPLACEMENT_RULES`` / ``REMOVAL_RULES``），普通约束变化（必填性、
  枚举、边界等）不吸收后代；
- 每个后代只挂到**最近的**可吸收祖先；多个吸收者嵌套时整组折叠到最顶层
  祖先，组内通过 parent_id / depth 仍可还原有向层级；
- 循环引用安全：关系完全由（无环的）Pointer 字符串前缀推导，不遍历
  文档节点，且引擎已在 DFS 栈上打断 $ref 环，这里不引入任何递归。

未吸收任何后代的发现作为 standalone 返回，保持与扁平视图完全一致的可见性。
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from .diff import BREAKING, DEPRECATION, INFO, NON_BREAKING
from .refs import decode_token

# 父节点被"另一种结构整体替换"：后代契约不再独立存在
REPLACEMENT_RULES = frozenset({
    "type-changed",                  # 基础类型整体改变（object↔string 等）
    "schema-became-false",           # 变为不允许任何值
    "schema-false-relaxed",          # 由 false 变为具体结构
    "schema-constraints-tightened",  # true（任意值）→ 具体结构
    "schema-constraints-relaxed",    # 具体结构 → true（任意值）
})

# 父节点被整体删除：后代随之一并消失
REMOVAL_RULES = frozenset({
    "path-removed",
    "operation-removed",
    "parameter-removed",
    "request-body-removed",
    "response-removed",
    "media-type-removed",
    "response-media-type-removed",
    "property-removed",
    "property-removed-request",
    "property-removed-writeonly",
    "composition-removed",
})

_ABSORBING_RULES = REPLACEMENT_RULES | REMOVAL_RULES

_SEVERITY_RANK = {BREAKING: 0, DEPRECATION: 1, NON_BREAKING: 2, INFO: 3}

# 这些 token 是"集合容器"而非成员节点；锚点停在容器上的发现
# （如新增/删除参数时指针只到 /parameters）不能吸收容器内其他成员
_COLLECTION_TOKENS = frozenset({"parameters", "content"})


# ---------------------------------------------------------------------- #
# JSON Pointer 祖先关系
# ---------------------------------------------------------------------- #
def _pointer_tokens(pointer: str | None) -> list[str] | None:
    """规范化为解码后的 token 列表；空指针 / 非法输入返回 None。"""
    if not isinstance(pointer, str) or not pointer:
        return None
    return [decode_token(tok) for tok in pointer.strip("/").split("/")]


def _absorbable_anchor(pointer: str | None) -> list[str] | None:
    """可吸收后代的锚点 token 列表。

    锚点停在集合容器本身（``.../parameters`` 数组、``.../content`` 映射）时
    返回 None —— 那只代表"容器发生增删"，不代表某个具体成员被整体替换，
    不能把兄弟参数 / 兄弟媒体类型的差异误吸进来。成员锚点（如
    ``.../parameters/0/schema``、``/components/parameters/X/schema``）
    末尾不是容器 token，不受影响。
    """
    tokens = _pointer_tokens(pointer)
    if not tokens:
        return None
    if tokens[-1] in _COLLECTION_TOKENS:
        return None
    return tokens


def _is_proper_descendant(child: list[str], parent: list[str]) -> bool:
    """child 是否为 parent 的真后代（token 全等前缀，长度更长）。"""
    return len(child) > len(parent) and child[: len(parent)] == parent


# ---------------------------------------------------------------------- #
# 主流程
# ---------------------------------------------------------------------- #
def aggregate_findings(
    findings: list[dict[str, Any]], *, review_id: int | None = None
) -> dict[str, Any]:
    """把扁平发现聚合为根因影响组 + 独立发现。"""
    # 历史数据可能缺引擎序号：按 1 起始位置补齐（与 repro / 前端回退一致）
    if not any(isinstance(f.get("id"), int) for f in findings):
        for i, f in enumerate(findings, 1):
            f.setdefault("id", i)

    # 1) 按 (endpoint, direction) 分桶
    buckets: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for f in findings:
        buckets[(f.get("endpoint"), f.get("direction"))].append(f)

    raw_groups: list[dict[str, Any]] = []
    for (endpoint, direction), items in buckets.items():
        raw_groups.extend(_aggregate_scope(endpoint, direction, items))

    # 2) 稳定排序：主因严重度 → 端点 → 主因引擎序号
    raw_groups.sort(key=lambda g: (
        _SEVERITY_RANK.get(g["root_cause"].get("severity"), 9),
        g["endpoint"] or "",
        _fid(g["root_cause"]),
    ))

    # 3) 稳定组标识（基于主因语义身份，同一文档对重复审查结果一致）
    used_ids: dict[str, int] = {}
    groups: list[dict[str, Any]] = []
    involved: set[int] = set()
    for g in raw_groups:
        gid = _stable_group_id(g["root_cause"])
        if gid in used_ids:
            # 极小概率的哈希碰撞 / 同身份多组：追加序号保持唯一稳定
            used_ids[gid] += 1
            gid = f"{gid}-{used_ids[gid]}"
        else:
            used_ids[gid] = 1

        root = g["root_cause"]
        affected = g["affected"]
        involved.add(_fid(root))
        involved.update(_fid(e["finding"]) for e in affected)

        groups.append({
            "group_id": gid,
            "endpoint": g["endpoint"],
            "direction": g["direction"],
            "relation": g["relation"],
            "root_cause": root,
            "affected_count": len(affected),
            "affected_severity_counts": _severity_counts(
                e["finding"] for e in affected
            ),
            "affected": affected,
        })

    standalone = [f for f in findings if _fid(f) not in involved]
    standalone.sort(key=lambda f: (
        _SEVERITY_RANK.get(f.get("severity"), 9),
        f.get("endpoint") or "",
        _fid(f),
    ))

    return {
        "review_id": review_id,
        "total": len(findings),
        "group_count": len(groups),
        "grouped_finding_count": len(involved),
        "ungrouped_count": len(standalone),
        "groups": groups,
        "standalone": standalone,
    }


def _aggregate_scope(
    endpoint: str | None,
    direction: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """单个 (端点, 方向) 桶内构建有向影响组。"""
    absorbers = [f for f in items if f.get("rule_id") in _ABSORBING_RULES]
    if not absorbers:
        return []

    # 预计算每个吸收者两侧可用的锚点（删除侧 / 现存侧）
    anchors: dict[int, dict[str, list[str] | None]] = {}
    for a in absorbers:
        anchors[_fid(a)] = {
            "old": _absorbable_anchor(a.get("old_json_path")),
            "new": _absorbable_anchor(a.get("new_json_path")),
        }

    # child_fid -> (nearest parent fid, child side)
    parent_of: dict[int, tuple[int, str]] = {}
    for child in items:
        side = "old" if child.get("change") == "removed" else "new"
        child_tokens = _pointer_tokens(
            child.get("old_json_path") if side == "old"
            else child.get("new_json_path")
        )
        if child_tokens is None:
            continue
        best: tuple[int, list[str]] | None = None
        for a in absorbers:
            if _fid(a) == _fid(child):
                continue  # 不吸收自己
            parent_tokens = anchors[_fid(a)][side]
            if parent_tokens is None:
                continue
            if _is_proper_descendant(child_tokens, parent_tokens):
                # 最近祖先 = token 路径最长；并列取引擎序号最小（稳定）
                if best is None or len(parent_tokens) > len(best[1]) or (
                    len(parent_tokens) == len(best[1]) and _fid(a) < best[0]
                ):
                    best = (_fid(a), parent_tokens)
        if best is not None:
            parent_of[_fid(child)] = (best[0], side)

    if not parent_of:
        return []

    # 最近父边 fid -> parent fid；沿边向上必然终止（祖先 token 严格更短），
    # seen 再做一道保险：即便出现环也会停住。
    edges = {child: parent for child, (parent, _side) in parent_of.items()}

    def top_of(fid: int) -> int:
        seen: set[int] = set()
        cur = fid
        while cur in edges and cur not in seen:
            seen.add(cur)
            cur = edges[cur]
        return cur

    def depth_of(fid: int) -> int:
        seen: set[int] = set()
        cur, depth = fid, 0
        while cur in edges and cur not in seen:
            seen.add(cur)
            depth += 1
            cur = edges[cur]
        return depth

    by_id = {_fid(f): f for f in items}
    members: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for child_fid, parent_fid in edges.items():
        members[top_of(child_fid)].append({
            "finding": by_id[child_fid],
            "parent_id": parent_fid,
            "depth": depth_of(child_fid),
        })

    out: list[dict[str, Any]] = []
    for root_fid, affected in members.items():
        root = by_id[root_fid]
        relation = "ancestor-removed" if root.get("rule_id") in REMOVAL_RULES \
            else "ancestor-replaced"
        # 受影响条目：深度、指针、序号稳定排序
        affected.sort(key=lambda e: (
            e["depth"],
            e["finding"].get("old_json_path") or e["finding"].get("new_json_path") or "",
            _fid(e["finding"]),
        ))
        out.append({
            "endpoint": endpoint,
            "direction": direction,
            "relation": relation,
            "root_cause": root,
            "affected": affected,
        })
    return out


# ---------------------------------------------------------------------- #
# 小工具
# ---------------------------------------------------------------------- #
def _fid(finding: dict[str, Any]) -> int:
    return int(finding["id"])


def _stable_group_id(root: dict[str, Any]) -> str:
    basis = {
        "rule_id": root.get("rule_id"),
        "direction": root.get("direction"),
        "endpoint": root.get("endpoint"),
        "location": root.get("location"),
        "change": root.get("change"),
    }
    raw = json.dumps(basis, ensure_ascii=False, sort_keys=True)
    return "g-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _severity_counts(findings: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        sev = f.get("severity")
        counts[sev] = counts.get(sev, 0) + 1
    return dict(sorted(counts.items()))
