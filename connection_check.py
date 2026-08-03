"""Lightweight live connectivity checks for admin-configured LLM/reranker
endpoints, used by the System Settings page's "Test" buttons. Deliberately
independent of EmailTriageEngine -- these are one-off sanity pings (a single
trivial completion / rerank call with a short timeout), not real triage
traffic, so they shouldn't need a DB-backed engine or full Settings object.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

_TIMEOUT_SECONDS = 20.0


def test_chat_completion(base_url: Optional[str], api_key: Optional[str], model: Optional[str]) -> Dict[str, Any]:
    if not base_url or not model:
        return {"ok": False, "error": "Base URL and model are required"}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        # Generous headroom, not a real budget concern for a one-off ping --
        # reasoning models can burn through a small max_tokens entirely on
        # hidden reasoning tokens and return zero visible content, which some
        # proxies reject as a bad upstream response (502) rather than an
        # empty-but-valid completion.
        "max_tokens": 64,
        "stream": False,
    }
    url = f"{base_url.rstrip('/')}/chat/completions"
    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
        reply = data["choices"][0]["message"]["content"] or ""
        return {"ok": True, "error": None, "detail": reply.strip()[:200] or "(empty completion, but request succeeded)"}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "error": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def test_rerank(url: Optional[str], api_key: Optional[str], model: Optional[str]) -> Dict[str, Any]:
    if not url:
        return {"ok": False, "error": "Reranker URL is required"}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {"model": model, "query": "test", "documents": ["test document"]}
    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=_TIMEOUT_SECONDS)
        response.raise_for_status()
        results = response.json().get("results", [])
        score = results[0].get("relevance_score") if results else None
        score_note = ""
        if score is not None and not (0.0 <= score <= 1.0):
            score_note = " -- outside [0,1]: this looks like a raw logit, consider enabling tei_score_normalize"
        return {"ok": True, "error": None, "detail": f"{len(results)} result(s) returned, score={score}{score_note}"}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "error": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
