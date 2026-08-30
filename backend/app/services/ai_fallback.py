"""
AI fallback classifier for rejected vector-PDF spans.

Uses OpenAI or Anthropic (Claude) text models — no full-page raster/OCR.
Input is text + neighbors + position hints from the vector layer.
Human feedback examples are injected as few-shot guidance.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any


def _http_json(url: str, headers: dict[str, str], payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body[:500]}") from e


def _strict_dim_only() -> bool:
    return os.environ.get("SMORX_STRICT_DIM", "1").strip().lower() not in {"0", "false", "no", "off"}


def _build_prompt(
    candidates: list[dict[str, Any]],
    few_shot: list[dict[str, str]],
) -> str:
    strict = _strict_dim_only()
    lines = [
        "You classify engineering-drawing text spans for First Article Inspection ballooning.",
        "Return ONLY valid JSON: {\"results\":[{\"id\":0,\"label\":\"dimension|ignore\",\"confidence\":0.0}]}",
        "",
    ]
    if strict:
        lines.extend([
            "STRICT MODE (mandatory — dimensions on main drawing views ONLY):",
            "label=dimension → real measurable geometry on the part/view (linear, diameter, radius, angle, tolerance, thread, chamfer, fit).",
            "label=ignore → ALWAYS for:",
            "  • BOM / bill-of-materials tables, material lists, item numbers, qty (nos/pcs), cut lengths, plate/BUSH/THK rows in BOM columns",
            "  • title block, revision table, drawing/part numbers, dates, stamps, company names",
            "  • notes, general tolerance blocks, welds, surface finish (Ra), GD&T, reference dims in parentheses",
            "  • item balloon numbers, border grid (A/B/1/2), sheet zone marks",
            "If zone=bom, zone=notes, or zone=title → label=ignore always.",
            "When unsure → label=ignore.",
            "",
        ])
    else:
        lines.extend([
            "label=dimension → real measurable callout (linear, diameter, radius, angle, weld size, plate size, fit, THK, etc.)",
            "label=ignore → item balloon numbers, border grid (A/B/1/2), qty, notes noise, title-block meta, dates, stamps",
            "Prefer ignore when unsure about circled item numbers or sheet zone marks.",
            "",
        ])
    if few_shot:
        lines.append("Human feedback examples (learn from these):")
        for fs in few_shot[:16]:
            lines.append(
                f"- text={fs.get('text')!r} neighbors={fs.get('neighbors')!r} → {fs.get('label')}"
            )
        lines.append("")
    lines.append("Candidates:")
    for c in candidates:
        lines.append(
            f"id={c['id']} text={c.get('text')!r} zone={c.get('zone','?')} "
            f"reason={c.get('reason','')} neighbors={c.get('neighbors','')!r} "
            f"page={c.get('page')} xy=({c.get('x')},{c.get('y')})"
        )
    return "\n".join(lines)


def _parse_results(raw: str, n: int) -> list[dict[str, Any]]:
    raw = raw.strip()
    # Extract JSON object if wrapped in markdown
    m = re.search(r"\{[\s\S]*\}", raw)
    if m:
        raw = m.group(0)
    data = json.loads(raw)
    results = data.get("results") if isinstance(data, dict) else data
    if not isinstance(results, list):
        return []
    by_id: dict[int, dict] = {}
    for r in results:
        try:
            i = int(r.get("id"))
        except Exception:
            continue
        label = str(r.get("label") or "ignore").lower().strip()
        if label not in {"dimension", "ignore"}:
            label = "ignore"
        conf = float(r.get("confidence") or 0.5)
        by_id[i] = {"id": i, "label": label, "confidence": conf}
    out = []
    for i in range(n):
        out.append(by_id.get(i, {"id": i, "label": "ignore", "confidence": 0.0}))
    return out


def classify_with_openai(
    candidates: list[dict[str, Any]],
    *,
    api_key: str,
    model: str = "gpt-4o-mini",
    few_shot: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    prompt = _build_prompt(candidates, few_shot or [])
    data = _http_json(
        "https://api.openai.com/v1/chat/completions",
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        {
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a precise FAI ballooning classifier. Reply with JSON only. "
                        "In strict mode: balloon only main-view geometry dimensions; "
                        "never BOM tables, title block, notes, or item numbers."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        },
    )
    content = data["choices"][0]["message"]["content"]
    return _parse_results(content, len(candidates))


def classify_with_claude(
    candidates: list[dict[str, Any]],
    *,
    api_key: str,
    model: str = "claude-sonnet-4-20250514",
    few_shot: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    prompt = _build_prompt(candidates, few_shot or [])
    data = _http_json(
        "https://api.anthropic.com/v1/messages",
        {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        {
            "model": model,
            "max_tokens": 4096,
            "temperature": 0,
            "messages": [
                {"role": "user", "content": prompt},
            ],
        },
    )
    parts = data.get("content") or []
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    return _parse_results(text, len(candidates))


def classify_candidates(
    candidates: list[dict[str, Any]],
    *,
    provider: str,
    api_key: str | None = None,
    model: str | None = None,
    few_shot: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    provider = (provider or "openai").lower().strip()
    if provider in {"openai", "gpt"}:
        key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError("OPENAI_API_KEY missing")
        return classify_with_openai(
            candidates,
            api_key=key,
            model=model or "gpt-4o-mini",
            few_shot=few_shot,
        )
    if provider in {"claude", "anthropic"}:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY missing")
        return classify_with_claude(
            candidates,
            api_key=key,
            model=model or "claude-sonnet-4-20250514",
            few_shot=few_shot,
        )
    raise ValueError(f"Unknown AI provider: {provider}")
