"""Small, stage-agnostic guards for explicit no-evidence model outputs."""

from __future__ import annotations

import re
import unicodedata


_NO_EVIDENCE_TEXTS = frozenset(
    {
        "[]",
        "{}",
        "n/a",
        "no data",
        "no evidence found",
        "no evidence was found",
        "no relevant data",
        "no relevant evidence",
        "no relevant evidence found",
        "no relevant information",
        "no relevant information found",
        "no relevant results",
        "no results",
        "not found",
        "nothing found",
        "none",
        "null",
        "无",
        "无数据",
        "暂无",
        "暂无数据",
        "未找到",
        "未找到任何相关信息",
        "未找到任何相关结果",
        "未找到任何相关证据",
        "未找到相关信息",
        "未找到相关结果",
        "未找到相关证据",
        "无任何相关信息",
        "无任何相关结果",
        "无任何相关证据",
        "无可用信息",
        "无可用结果",
        "无可用证据",
        "无相关信息",
        "无相关结果",
        "无相关证据",
        "暂无相关信息",
        "暂无相关结果",
        "暂无相关证据",
        "没有找到相关信息",
        "没有找到相关结果",
        "没有找到相关证据",
        "搜索无结果",
    }
)
_NO_EVIDENCE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"no (?:relevant )?(?:data|evidence|information|results?)(?: (?:was|were))? found",
        r"(?:i )?(?:could|did) not find(?: any)?(?: relevant)? (?:data|evidence|information|results?)",
        r"(?:未|没有)找到(?:任何)?(?:相关)?(?:信息|结果|证据|数据|资料|内容)",
        r"(?:暂无|无)(?:任何|可用)?(?:相关)?(?:信息|结果|证据|数据|资料|内容)",
    )
)


def _normalize_no_evidence_candidate(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    normalized = re.sub(r"^[\s`#>*_=-]+", "", normalized)
    normalized = re.sub(r"[\s`#>*_=-]+$", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"[.!?。！？;；:：]+$", "", normalized).strip()
    return normalized


def is_no_evidence_text(text: str | None) -> bool:
    """Return True only when the complete response is an explicit no-result sentinel."""
    if not text or not str(text).strip():
        return False
    normalized = _normalize_no_evidence_candidate(str(text))
    return normalized in _NO_EVIDENCE_TEXTS or any(
        pattern.fullmatch(normalized) for pattern in _NO_EVIDENCE_PATTERNS
    )


def has_meaningful_semantic_text(text: str | None) -> bool:
    """Ignore Markdown shells and require at least one non-sentinel business line."""
    if not text or not str(text).strip():
        return False
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or set(line) <= {"-", "=", "_", "*"}:
            continue
        normalized = line.lstrip(">-* ").strip()
        if not normalized or normalized.lower().startswith(
            ("生成时间:", "采集时间:", "读取时间:", "generated_at:")
        ):
            continue
        if not is_no_evidence_text(normalized):
            return True
    return False
