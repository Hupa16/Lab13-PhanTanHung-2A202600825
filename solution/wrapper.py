"""YOUR mitigation + observability layer. The simulator calls mitigate() around the
opaque agent (a REAL LLM) for every request. This is the ONLY place observability can
live -- the agent is silent. Legal moves: retry / cache / route / guardrail / sanitize
/ fallback / session-reset / PROMPT ROUTING, plus your own logging/tracing/metrics.
Illegal: hardcoding answers, importing the agent internals, reading instructor files,
network exfiltration.

  call_next(question, config) -> result   # the only way to reach the black box
  context = {"session_id","turn_index","qid","cache": <shared dict>, "cache_lock": <Lock>}
  result  = {"answer","status","steps","trace","meta":{latency_ms,usage,...}}
"""
from __future__ import annotations

import re
import time
import unicodedata
from telemetry.logger import logger, new_correlation_id, set_correlation_id
from telemetry.cost import cost_from_usage
from telemetry.redact import redact


def sanitize_question(q: str) -> str:
    """Loại bỏ prompt injection nằm trong phần Ghi chú đơn hàng."""
    if not isinstance(q, str):
        return q
    # Chuẩn hóa Unicode sang NFC
    q = unicodedata.normalize('NFC', q)
    # Loại bỏ bất kỳ nội dung nào từ 'Ghi chú', 'ghi chu', 'note', 'luu y' đến cuối chuỗi
    q = re.sub(
        r'(?i)(?:ghi\s*chú|ghi\s*chu|note|notes|g\.chú|g\.chu|gchú|gchu|lưu\s*ý|luu\s*y)\s*[:\-–—].*$',
        '',
        q
    )
    return q.strip()


def mitigate(call_next, question, config, context):
    # Sao chép config để tùy chỉnh động cho từng request
    conf = dict(config)

    # Thiết lập correlation ID cho request này
    cid = new_correlation_id()
    set_correlation_id(cid)

    qid = context.get("qid", "unknown")
    session_id = context.get("session_id", "unknown")
    turn_index = context.get("turn_index", 0)

    # Vệ sinh câu hỏi đầu vào
    sanitized_q = sanitize_question(question)

    if logger:
        logger.log_event("REQUEST_RECEIVED", {
            "qid": qid,
            "session_id": session_id,
            "turn_index": turn_index,
            "question": question,
            "sanitized_question": sanitized_q
        })

    # Cache check (Thread-safe)
    cache = context.get("cache")
    lock = context.get("cache_lock")
    if cache is not None and lock is not None:
        with lock:
            if sanitized_q in cache:
                cached_res = cache[sanitized_q]
                if logger:
                    logger.log_event("CACHE_HIT", {
                        "qid": qid,
                        "question": sanitized_q,
                        "answer": cached_res.get("answer")
                    })
                return cached_res

    # Thực hiện gọi LLM + Retry nếu lỗi
    t0 = time.time()
    result = None
    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            result = call_next(sanitized_q, conf)
            # Nếu status là ok hoặc không lỗi nghiêm trọng thì dừng retry
            if result.get("status") == "ok":
                break
        except Exception as e:
            if logger:
                logger.log_event("CALL_EXCEPTION", {
                    "qid": qid,
                    "attempt": attempt,
                    "error": str(e)
                })
            if attempt == max_attempts - 1:
                # Trả về kết quả lỗi mặc định nếu hết số lần retry
                result = {
                    "answer": "Rất tiếc, hệ thống đang bận. Vui lòng thử lại sau.",
                    "status": "wrapper_error",
                    "steps": 0,
                    "trace": [],
                    "meta": {"latency_ms": int((time.time() - t0) * 1000), "usage": {}, "tools_used": []}
                }

    # Đảm bảo có result hợp lệ
    if result is None:
        result = {
            "answer": "Rất tiếc, không có phản hồi từ hệ thống.",
            "status": "wrapper_error",
            "steps": 0,
            "trace": [],
            "meta": {"latency_ms": int((time.time() - t0) * 1000), "usage": {}, "tools_used": []}
        }

    duration_ms = int((time.time() - t0) * 1000)

    # Redact PII trong câu trả lời
    ans = result.get("answer")
    num_redact = 0
    if ans and isinstance(ans, str):
        redacted_ans, num_redact = redact(ans)
        if num_redact > 0:
            result["answer"] = redacted_ans

    # Lưu kết quả thành công vào Cache
    if result.get("status") == "ok" and cache is not None and lock is not None:
        with lock:
            cache[sanitized_q] = result

    # Log telemetry sau khi hoàn thành
    meta = result.get("meta", {}) or {}
    usage = meta.get("usage", {}) or {}
    cost = cost_from_usage(meta.get("model", ""), usage)
    tools = meta.get("tools_used", []) or []

    if logger:
        logger.log_event("REQUEST_COMPLETED", {
            "qid": qid,
            "session_id": session_id,
            "turn_index": turn_index,
            "duration_ms": duration_ms,
            "status": result.get("status"),
            "steps": result.get("steps"),
            "usage": usage,
            "cost_usd": cost,
            "tools_used": tools,
            "pii_redacted_count": num_redact
        })

    return result
