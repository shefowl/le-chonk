"""Claude through the Agent SDK, on the Claude subscription the bundled CLI is logged in
with (no API key). One call = one model turn: the conversation so far goes in as text
(and the map as an image), the reply comes back as JSON naming the tool calls, and is
turned into an OpenAI-style message so the agents' loop does not change (20.10).

The reasoning knob maps to Claude's own: a number is a thinking budget in tokens (a hard
cap, unlike DeepSeek on OpenRouter, which took it as a hint), "high" is adaptive thinking
at high effort, none is no thinking at low effort.
"""
import asyncio
import base64
import json
import os
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# an empty folder: the CLI must not pick up a project's CLAUDE.md or settings
CWD = os.path.join(ROOT, "logs", "claude_cwd")

REPLY = {
    "type": "object",
    "properties": {
        "thought": {"type": "string", "description": "a sentence or two: why these calls"},
        "calls": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"},
                           "arguments": {"type": "object"}},
            "required": ["name", "arguments"]}},
    },
    "required": ["thought", "calls"],
}


def _tools_text(tools):
    out = []
    for t in tools or []:
        f = t.get("function", t)
        out.append("- %s: %s\n  parameters: %s" % (
            f["name"], f.get("description", ""),
            json.dumps(f.get("parameters", {}), ensure_ascii=False)))
    return "\n".join(out)


def _text(content):
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
    return content or ""


def _images(content):
    out = []
    if isinstance(content, list):
        for c in content:
            if c.get("type") != "image_url":
                continue
            u = c.get("image_url") or ""
            url = u.get("url", "") if isinstance(u, dict) else u   # the map comes as a string
            if url.startswith("data:") and ";base64," in url:
                head, data = url.split(";base64,", 1)
                out.append({"type": "image", "source": {
                    "type": "base64", "media_type": head[5:], "data": data}})
    return out


def _transcript(messages):
    """The conversation as text blocks (and the images it carried)."""
    parts, images = [], []
    for m in messages:
        r = m.get("role")
        if r == "system":
            continue
        if r == "user":
            parts.append("=== MESSAGE ===\n" + _text(m.get("content")))
            images += _images(m.get("content"))
        elif r == "assistant":
            calls = ["%s(%s)" % (c["function"]["name"], c["function"].get("arguments", ""))
                     for c in m.get("tool_calls") or []]
            parts.append("=== YOUR EARLIER STEP ===\n%s%s" % (
                _text(m.get("content")) or "",
                ("\ncalls: " + "; ".join(calls)) if calls else ""))
        elif r == "tool":
            parts.append("=== RESULT (%s) ===\n%s" % (m.get("name") or m.get("tool_call_id", ""),
                                                     _text(m.get("content"))))
    return "\n\n".join(parts), images


def _thinking(reasoning):
    if reasoning in (None, "none", False):
        return {"type": "disabled"}, "low"
    if str(reasoning).isdigit():
        n = int(reasoning)
        return {"type": "enabled", "budget_tokens": max(1024, n)}, "medium" if n >= 8000 else "low"
    return {"type": "adaptive"}, str(reasoning) if reasoning in ("low", "medium", "high") else "high"


async def _ask(model, system, prompt, images, thinking, effort, max_tokens=None):
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
    from claude_agent_sdk.types import RateLimitEvent
    os.makedirs(CWD, exist_ok=True)
    # no tools but the reply: 20.10 the account's claude.ai connectors (Claude Docs and
    # others) came into the session, Haiku called answer_event as a real tool, got "No
    # such tool" and reported the event unanswerable
    env = {"ENABLE_CLAUDEAI_MCP_SERVERS": "false", "MAX_STRUCTURED_OUTPUT_RETRIES": "1"}
    if max_tokens is not None:
        max_tokens = max(1, int(max_tokens))
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)
        # Claude requires enabled thinking to fit inside the output budget.
        if thinking.get("type") == "enabled":
            thinking = dict(thinking)
            if max_tokens <= 1024:
                thinking = {"type": "disabled"}
            else:
                thinking["budget_tokens"] = min(thinking["budget_tokens"], max_tokens - 1)
    opts = ClaudeAgentOptions(model=model, system_prompt=system, tools=[],
                              mcp_servers={}, strict_mcp_config=True,
                              env=env,
                              setting_sources=[], max_turns=6, cwd=CWD,
                              thinking=thinking, effort=effort,
                              output_format={"type": "json_schema", "schema": REPLY})

    async def stream():
        yield {"type": "user", "message": {"role": "user", "content":
               images + [{"type": "text", "text": prompt}]}, "parent_tool_use_id": None}

    result, windows = None, {}
    async for m in query(prompt=stream() if images else prompt, options=opts):
        if isinstance(m, ResultMessage):
            result = m
        elif isinstance(m, RateLimitEvent):
            raw = getattr(m.rate_limit_info, "raw", None) or {}
            windows = {k: v.get("utilization") for k, v in
                       (raw.get("unifiedWindows") or {}).items()}
    return result, windows


def chat(model, messages, tools, tool_choice, role, reasoning, budget_s, **extra):
    system = next((_text(m["content"]) for m in messages if m.get("role") == "system"), "")
    must = tool_choice in ("any", "required")
    # 20.10: under a "TOOLS" heading Haiku called the orders as real tools, got "No such
    # tool", and wrote in its summaries that nothing could be carried out (they all were)
    system += ("\n\nTHE GAME'S ORDERS. They are not tools of this session: you give them by "
               "listing them in your reply, and the game carries them out in order as soon as "
               "you answer; their results come back to you in the next message. Each order "
               "and its arguments:\n" + _tools_text(tools) +
               "\n\nREPLY: your only tool is the structured reply, one JSON object: "
               "{\"thought\": ..., \"calls\": [{\"name\": <an order above>, \"arguments\": "
               "{...}}]}. %s" % (
                   "List at least one order." if must else
                   "An empty calls list means you have nothing more to order."))
    prompt, images = _transcript(messages)
    thinking, effort = _thinking(reasoning)
    t0 = time.time()
    async def request():
        pending = _ask(model, system, prompt, images, thinking, effort,
                       max_tokens=extra.get("max_tokens"))
        if budget_s is None:
            return await pending
        # The deadline encloses the SDK session, so cancellation also closes its CLI.
        return await asyncio.wait_for(pending, timeout=budget_s)
    from claude_agent_sdk import ResultError
    try:
        res, windows = asyncio.run(request())
    except TimeoutError as exc:
        from .llm import LLMOverBudget
        raise LLMOverBudget("claude: %s exceeded %.1f s" % (role, budget_s)) from exc
    except ResultError as exc:
        if "structured output" not in str(exc).lower():
            raise
        from .llm import LLMOutputError
        raise LLMOutputError("claude: invalid structured output; no orders executed") from exc
    dt = time.time() - t0
    if res is None or res.is_error:
        from .llm import LLMError
        raise LLMError("claude: %s" % ((res and (res.errors or res.result)) or "no result"))
    out = res.structured_output
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except ValueError:
            out = None
    if not isinstance(out, dict):
        out = {"thought": res.result or "", "calls": []}
    calls = []
    for c in out.get("calls") or []:
        if isinstance(c, str):             # a call given as a JSON string
            try:
                c = json.loads(c)
            except ValueError:
                continue
        if not isinstance(c, dict) or not c.get("name"):
            continue
        a = c.get("arguments") or {}
        if isinstance(a, str):             # arguments given as a JSON string
            try:
                a = json.loads(a)
            except ValueError:
                a = {}
        calls.append({"id": "call_" + uuid.uuid4().hex[:12], "type": "function",
                      "function": {"name": c["name"], "arguments": json.dumps(
                          a if isinstance(a, dict) else {}, ensure_ascii=False)}})
    msg = {"role": "assistant", "content": out.get("thought") or ""}
    if calls:
        msg["tool_calls"] = calls
    u = res.usage or {}
    inp = (u.get("input_tokens") or 0)
    cached = (u.get("cache_read_input_tokens") or 0)
    created = (u.get("cache_creation_input_tokens") or 0)
    usage = {"prompt_tokens": inp + cached + created,
             "completion_tokens": u.get("output_tokens") or 0}
    row = {"latency_s": round(dt, 3), "prompt_tokens": usage["prompt_tokens"],
           "completion_tokens": usage["completion_tokens"], "cached_tokens": cached,
           "cache_write_tokens": created,
           # what the same tokens would cost on the API; the subscription pays nothing per call
           "equiv_usd": res.total_cost_usd, "cost_usd": 0.0,
           "finish": "tool_calls" if calls else "stop", "effort": effort,
           "thinking": thinking.get("budget_tokens") or thinking["type"],
           # how full the subscription's windows are: the same limits as the user's own
           # Claude sessions, so a run that eats them stops those too
           "windows": windows}
    return msg, usage, dt, row


def search(model, question):
    """One question to the web through Claude's own WebSearch tool (on the subscription):
    (answer, source urls). OpenRouter's web plugin does not exist here (21.10: Haiku
    answered three planner searches with "I have no search tool")."""
    import re
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    async def ask():
        os.makedirs(CWD, exist_ok=True)
        opts = ClaudeAgentOptions(
            model=model, tools=["WebSearch"], allowed_tools=["WebSearch"],
            mcp_servers={}, strict_mcp_config=True,
            env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}, setting_sources=[],
            max_turns=6, cwd=CWD, effort="low",
            system_prompt="Search the web and answer the question in at most 200 words. Name "
                          "the game version or the date of each source when it shows; say "
                          "plainly when the results do not answer the question or may describe "
                          "an older version. End with the source URLs, one per line.")
        res = None
        async for m in query(prompt=question, options=opts):
            if isinstance(m, ResultMessage):
                res = m
        return res
    res = asyncio.run(ask())
    text = (res.result if res and not res.is_error else "") or ""
    urls = list(dict.fromkeys(re.findall(r"https?://[^\s)\]>\"']+", text)))
    return text.strip(), urls[:6], res
