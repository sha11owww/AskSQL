from __future__ import annotations

"""意图-结果结构比对。

预处理阶段已经抽出 metrics / dimensions / operations / time_expressions / filters
五个结构化字段，但此前只有 retrieval_terms 真正参与执行，其余字段写进 state 后
原样返回前端、对结果没有任何约束力。本模块把它们和最终 SQL 做纯规则比对，
用于回答一个模型自评容易漏掉的问题：SQL 没报错，但它算的东西是不是用户要的。

设计原则：

1. 纯函数、零额外模型调用。不联网、不读数据库，只看 SQL 文本、返回列和 Schema 定义。
2. 只抓强信号。每条规则都要求两个以上条件同时成立才判负，宁可漏报不可误伤。
3. 只当重写触发器，不当最终判决。规则判负会触发一次 SQL 重写；若重写后仍然判负，
   调用方会把结果恢复为模型的原判断并在说明里追加提示，不会把可用的数据判成失败。
"""

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from ..database import SCHEMA, physical_table_name


_AGGREGATE = re.compile(r"\b(sum|count|avg|min|max|median|stddev)\s*\(", re.I)
_GROUP_BY = re.compile(r"\bgroup\s+by\b", re.I)
_ORDER_BY = re.compile(r"\border\s+by\b", re.I)
_WHERE = re.compile(r"\bwhere\b", re.I)
_COUNT_STAR = re.compile(r"\bcount\s*\(\s*\*\s*\)", re.I)
_YEAR = re.compile(r"(?:19|20)\d{2}")
_TIME_HINT = re.compile(r"(date|dt|time|day|month|year|日期|时间|月份)", re.I)

# 明确要求"按某个顺序取前几名"的说法。
_RANKING_TERMS = (
    "排名", "排行", "top", "前十", "前五", "前三", "前几名", "前 10",
    "榜单", "降序", "升序", "从高到低", "从低到高", "最多的前",
)
# 明确要求"逐条列出"的说法。
_DETAIL_TERMS = ("明细", "逐条", "每一条", "每条记录", "原始记录", "原始数据", "清单", "流水")
# 出现这些词说明用户要的是整体口径，此时缺少 GROUP BY 是正常的。
_TOTAL_TERMS = ("总计", "合计", "整体", "总体", "全部", "所有", "一共", "总量", "全量", "不分")


def _norm(text: Any) -> str:
    """归一化文本：去大小写、下划线、空白和引号，用于中文别名和列名的宽松匹配。"""
    return re.sub(r"[\s_`\"'\\[\\]()（）]+", "", str(text or "").lower())


@lru_cache(maxsize=1)
def _field_index() -> dict[str, list[dict[str, str]]]:
    """把 Schema 的字段名、中文名和别名索引成 "叫法 -> 候选字段"。"""
    index: dict[str, list[dict[str, str]]] = {}
    for table in SCHEMA:
        table_name = physical_table_name(table)
        for item in table.get("fields", []):
            entry = {
                "column": str(item["name"]),
                "label": str(item.get("label") or ""),
                "role": str(item.get("role") or ""),
                "aggregation": str(item.get("aggregation") or "none"),
                "table": table_name,
            }
            keys = [item["name"], item.get("label") or "", *(item.get("aliases") or [])]
            for key in keys:
                normalized = _norm(key)
                if not normalized:
                    continue
                bucket = index.setdefault(normalized, [])
                if all(existing["column"] != entry["column"] for existing in bucket):
                    bucket.append(entry)
    return index


@dataclass
class IntentCheckResult:
    """一次意图-结果比对的结果。"""

    checked: bool = False
    valid: bool = True
    retryable: bool = False
    reason: str = ""
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "valid": self.valid,
            "retryable": self.retryable,
            "reason": self.reason,
            "problems": list(self.problems),
            "notes": list(self.notes),
        }


class IntentChecker:
    """用预处理抽出的结构化意图校验 SQL 的执行结果。"""

    def check(
        self,
        extraction: dict[str, Any] | None,
        sql: str,
        columns: list[str] | None = None,
    ) -> IntentCheckResult:
        extraction = extraction or {}
        metrics = self._clean(extraction.get("metrics"))
        dimensions = self._clean(extraction.get("dimensions"))
        operations = self._clean(extraction.get("operations"))
        times = self._clean(extraction.get("time_expressions"))
        filters = self._clean(extraction.get("filters"))

        if not any((metrics, dimensions, operations, times, filters)):
            # 预处理没抽出任何结构，无从比对；保持沉默，不干预下游。
            return IntentCheckResult(checked=False)

        lowered = (sql or "").lower()
        column_text = _norm(" ".join(str(item) for item in (columns or [])))
        has_aggregate = bool(_AGGREGATE.search(lowered))
        has_group = bool(_GROUP_BY.search(lowered))

        problems: list[str] = []
        notes: list[str] = []

        problems.extend(self._check_metrics(metrics, lowered, column_text))
        problems.extend(self._check_dimensions(dimensions, has_aggregate, has_group))
        problems.extend(self._check_operations(operations, lowered, has_aggregate, has_group))
        problems.extend(self._check_time(times, lowered, notes))
        missing_filter = filters and not _WHERE.search(lowered)
        if missing_filter:
            notes.append(f"用户提到过滤条件「{'、'.join(filters)}」，SQL 中没有 WHERE")

        reason = "；".join(problems)
        if reason:
            reason = f"{reason}。请据此修正 SQL 后重新执行。"
        return IntentCheckResult(
            checked=True,
            valid=not problems,
            retryable=bool(problems),
            reason=reason,
            problems=problems,
            notes=notes,
        )

    # ------------------------------------------------------------------ 规则

    def _check_metrics(
        self,
        metrics: list[str],
        sql: str,
        column_text: str,
    ) -> list[str]:
        """用户点名的指标，SQL 里是不是真的算了。"""
        if not metrics:
            return []
        missing: list[str] = []
        mapped_hits = 0
        for metric in metrics:
            present, mapped = self._metric_present(metric, sql, column_text)
            if mapped:
                mapped_hits += 1
            if not present:
                missing.append(metric)
        # 只有至少一个指标能落到具体字段时，才认为这次抽取足够可靠；
        # 全部映射不上说明多半是抽取质量差，此时判负属于误伤。
        if missing and mapped_hits:
            return [
                f"用户要求的指标「{'、'.join(missing)}」没有出现在 SQL 或返回列中"
            ]
        return []

    @staticmethod
    def _check_dimensions(
        dimensions: list[str],
        has_aggregate: bool,
        has_group: bool,
    ) -> list[str]:
        """要求按维度拆分，却只有聚合没有分组。"""
        if not dimensions or not has_aggregate or has_group:
            return []
        wanted = [
            item for item in dimensions
            if not any(word in item for word in _TOTAL_TERMS)
        ]
        if not wanted:
            return []
        return [f"用户要求按「{'、'.join(wanted)}」拆分统计，SQL 有聚合但缺少 GROUP BY"]

    @staticmethod
    def _check_operations(
        operations: list[str],
        sql: str,
        has_aggregate: bool,
        has_group: bool,
    ) -> list[str]:
        problems: list[str] = []
        ranking = [item for item in operations if any(word in item.lower() for word in _RANKING_TERMS)]
        if ranking and not _ORDER_BY.search(sql):
            problems.append(
                f"用户要求「{'、'.join(ranking)}」，SQL 没有 ORDER BY，结果无法按预期排序取前几名"
            )
        detail = [item for item in operations if any(word in item for word in _DETAIL_TERMS)]
        if detail and has_aggregate and not has_group:
            problems.append(
                f"用户要求「{'、'.join(detail)}」，SQL 只做了聚合，没有返回逐条记录"
            )
        return problems

    @staticmethod
    def _check_time(times: list[str], sql: str, notes: list[str]) -> list[str]:
        """用户给了明确年份，SQL 是不是漏了时间窗口。"""
        if not times:
            return []
        years = {match.group(0) for item in times for match in _YEAR.finditer(item)}
        if not years or any(year in sql for year in years):
            return []
        window = "、".join(sorted(years))
        if not _WHERE.search(sql):
            return [f"用户限定时间范围（{window}），SQL 完全没有 WHERE 过滤"]
        if not _TIME_HINT.search(sql):
            return [f"用户限定时间范围（{window}），SQL 的过滤条件里没有出现任何时间字段"]
        # 有时间字段但年份对不上，可能是数据本身不含该年份，交给模型判断，不强制重写。
        notes.append(f"用户提到时间范围（{window}），SQL 使用了时间字段但没有出现该年份")
        return []

    # ------------------------------------------------------------------ 匹配

    def _metric_present(self, metric: str, sql: str, column_text: str) -> tuple[bool, bool]:
        """返回 (指标是否已体现, 是否成功映射到具体字段)。"""
        key = _norm(metric)
        candidates = _field_index().get(key)
        if candidates:
            for entry in candidates:
                if self._column_used(entry, sql, column_text):
                    return True, True
            return False, True
        # 映射不上时退化为字面匹配，命中即认为已体现，但不算"可靠映射"。
        if key and (key in _norm(sql) or key in column_text):
            return True, False
        return False, False

    @staticmethod
    def _column_used(entry: dict[str, str], sql: str, column_text: str) -> bool:
        column = entry["column"]
        if re.search(rf"\b{re.escape(column)}\b", sql):
            return True
        if _norm(column) and _norm(column) in column_text:
            return True
        # COUNT(*) 与"订单数"这类基于主键计数的指标等价。
        if entry.get("aggregation") == "count" and _COUNT_STAR.search(sql):
            return True
        return False

    @staticmethod
    def _clean(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()][:8]
