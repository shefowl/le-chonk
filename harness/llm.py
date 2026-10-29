"""Chat completions with tool calling, for Mistral Studio, OpenRouter or a local server.

All three speak the OpenAI-style /chat/completions protocol. Every call is appended to
logs/llm.jsonl with model, latency and token usage (never the key), so cost and speed
can be reported per run.
"""
import json
import os
import random
import threading
import time

import requests

from . import credentials

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROVIDERS = {
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "local": (None, None),       # LOCAL_LLM_URL, e.g. http://127.0.0.1:8081/v1
    # Claude through the Agent SDK on the user's Claude subscription (the CLI's own
    # login), not a paid key (the user, 20.10: $10 burned on OpenRouter)
    "claude": (None, None),
    "custom": (None, None),
}


def load_env(path=os.path.join(ROOT, ".env")):
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass


class LLMError(Exception):
    pass


class LLMOutputError(LLMError):
    """The provider exhausted structured output repair; no order was returned."""


class LLMOverBudget(LLMError):
    """The answer was still streaming when its wall-clock budget ran out. `partial` holds
    the reasoning received so far."""

    def __init__(self, msg, partial=""):
        super().__init__(msg)
        self.partial = partial


class LLM:
    def __init__(self, provider=None, log_path=os.path.join(ROOT, "logs", "llm.jsonl")):
        load_env()
        self.provider = provider or os.environ.get("LLM_PROVIDER", "mistral")
        config = credentials.load()
        base, key_var = PROVIDERS[self.provider]
        self.base = (base or os.environ.get("LOCAL_LLM_URL", "")).rstrip("/")
        self.key = os.environ.get(key_var, "") if key_var else os.environ.get("LOCAL_LLM_KEY", "")
        if config.get('provider') == self.provider:
            self.base = config['base_url']
            self.key = config.get('api_key','') if self.provider == 'custom' else (config.get('api_key') or self.key)
        if self.provider == 'custom' and config.get('provider') != 'custom':
            raise LLMError('Custom provider requires BYOK configuration')
        if key_var and not self.key:
            raise LLMError("%s is not set (put it in .env)" % key_var)
        self.log_path = log_path
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        self._lock = threading.Lock()
        self.session = requests.Session()
        # At most this many requests in flight per model: three marshals waking on the
        # same game day otherwise burst into the provider's rate limit (seen: 429 from
        # OpenRouter's shared upstream for mistral-small-2603).
        self._slots = {}
        # on the subscription every agent shares one model: two at a time let a war's
        # marshals hold the ministers back for days (21.10)
        self.per_model = int(os.environ.get("LLM_MAX_PARALLEL_PER_MODEL",
                                            "4" if self.provider == "claude" else "2"))

    def chat(self, model, messages, tools=None, tool_choice="auto", temperature=0.4,
             max_tokens=2048, role="", reasoning=None, budget_s=None, **extra):
        if self.provider == "claude":
            from .claude_sub import chat as claude_chat
            slot = self._slots.setdefault(model, threading.Semaphore(self.per_model))
            queued = time.monotonic()
            with slot:
                queue_s = time.monotonic() - queued
                started = time.monotonic()
                try:
                    msg, usage, dt, row = claude_chat(
                        model, messages, tools, tool_choice, role, reasoning, budget_s,
                        max_tokens=max_tokens, **extra)
                except (LLMOverBudget, LLMOutputError) as exc:
                    # The SDK has not returned final usage. Null means unknown, not free.
                    self._log(dict(event="timeout" if isinstance(exc, LLMOverBudget)
                                   else "invalid_output", t=time.time(), role=role,
                                   provider="claude", model=model,
                                   latency_s=round(time.monotonic() - started, 3),
                                   queue_s=round(queue_s, 3), budget_s=budget_s,
                                   prompt_tokens=None, completion_tokens=None, cost_usd=None))
                    raise
                row["queue_s"] = round(queue_s, 3)
            self._log(dict(row, t=time.time(), role=role, provider="claude", model=model))
            return msg, usage, dt
        body = {"model": model, "messages": messages, "temperature": temperature,
                "max_tokens": max_tokens}
        if reasoning:            # same knob, two dialects
            # a number is a token cap (18.10: DeepSeek on "low" still thought 24k characters
            # and ran out of tokens before ordering anything); Large 4 knows only none and high
            cap = int(reasoning) if str(reasoning).isdigit() else None
            if self.provider == "openrouter":
                body["reasoning"] = {"max_tokens": cap} if cap else {"effort": reasoning}
            else:
                effort = "high" if cap else reasoning
                # Studio Large4 rejects none/minimal: confirmed HTTP400 supported low/medium/high.
                if self.provider == 'mistral' and model == credentials.LECHONK:
                    effort = {'none':'low','minimal':'low','xhigh':'high'}.get(effort,effort)
                body["reasoning_effort"] = effort
        if tools:
            body["tools"] = tools
            # Mistral says "any" for "must call a tool"; OpenAI-style APIs say "required"
            if tool_choice == "any" and self.provider != "mistral":
                tool_choice = "required"
            body["tool_choice"] = tool_choice
        body.update(extra)
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        body["stream"] = True
        if self.provider == "openrouter":
            body["usage"] = {"include": True}
        slot = self._slots.setdefault(model, threading.Semaphore(self.per_model))
        t0 = time.time()
        last = None
        with slot:
            for attempt in range(7):
                if budget_s and time.time() - t0 > budget_s:
                    raise LLMOverBudget("%s: no answer within %.0f s (%s)"
                                        % (role, time.time() - t0, last))
                try:
                    # streamed: a long answer (Large 4 reasons for minutes) is not a timeout;
                    # silence is. The queue before the first byte gets a pulse too.
                    waiting = threading.Event()
                    threading.Thread(target=self._pulse_queue, args=(role, t0, waiting),
                                     daemon=True).start()
                    try:
                        r = self.session.post(self.base + "/chat/completions", headers=headers,
                                              data=json.dumps(body), stream=True, allow_redirects=False,
                                              timeout=(15, 60 if not budget_s else
                                                       max(5, min(60, budget_s - (time.time() - t0)))))
                    finally:
                        waiting.set()
                    if r.status_code == 429 or r.status_code >= 500:
                        last = "HTTP %d" % r.status_code
                        r.close()
                        self._log({"t": time.time(), "role": role, "model": model,
                                   "retry": attempt, "status": r.status_code})
                        time.sleep(min(60, 2 ** (attempt + 1)) * (0.5 + random.random()))
                        continue
                    if r.status_code != 200:
                        status = r.status_code
                        detail = self._error_message(r)
                        r.close()
                        self._log(dict(t=time.time(),role=role,provider=self.provider,model=model,
                                       event='http_error',status=status,latency_s=round(time.time()-t0,3),
                                       prompt_tokens=None,completion_tokens=None,cost_usd=None))
                        raise LLMError("Provider returned HTTP %d: %s" % (status,detail))
                    try:
                        msg, usage, finish = self._read_stream(r, role, t0, budget_s)
                    finally:
                        r.close()
                    break
                except requests.RequestException as e:
                    last = type(e).__name__
                    self._log({"t": time.time(), "role": role, "model": model,
                               "retry": attempt, "status": type(e).__name__})
                    time.sleep(min(60, 2 ** attempt) * (0.5 + random.random()))
                    continue
            else:
                raise LLMError("gave up after retries: %s" % last)
        dt = time.time() - t0
        self._log({"t": time.time(), "role": role, "provider": self.provider, "model": model,
                   "latency_s": round(dt, 3), "prompt_tokens": usage.get("prompt_tokens"),
                   "completion_tokens": usage.get("completion_tokens"),
                   "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                   "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                   "cost_usd": usage.get("cost"), "finish": finish})
        return msg, usage, dt

    def _error_message(self, response):
        try:
            value=response.json()
            message=value.get('message') or value.get('detail') or value.get('error')
            if isinstance(message,dict):message=message.get('message')
            if not isinstance(message,str):return 'Check request format, API key, model ID and account access'
            if self.key:message=message.replace(self.key,'[REDACTED]')
            return ' '.join(message.split())[:400]
        except (ValueError,AttributeError):
            return 'Check request format, API key, model ID and account access'

    PULSE_S = 15

    def _pulse_queue(self, role, t0, done):
        while not done.wait(self.PULSE_S):
            print("    .. %s %3.0fs: queued at the provider, no answer yet"
                  % (role, time.time() - t0), flush=True)

    def _read_stream(self, r, role, t0, budget_s):
        """Assemble a streamed (SSE) chat completion into one assistant message. Content
        comes as text deltas or, from Mistral's reasoning models, as chunk lists with
        "thinking" parts; tool calls come as deltas keyed by index.

        A watchdog thread prints a pulse every PULSE_S seconds and closes the connection
        when budget_s (wall clock) runs out, also while nothing arrives at all: Mistral
        sometimes queues a request for 90 s before the first byte."""
        thinking, text, calls, usage, finish = [], [], {}, {}, None
        sources = []
        state = {"done": False, "over": False}

        def watch():
            pulse = t0 + self.PULSE_S
            while not state["done"]:
                time.sleep(0.5)
                now = time.time()
                if budget_s and now - t0 > budget_s:
                    state["over"] = True
                    r.close()
                    return
                if now >= pulse:
                    pulse = now + self.PULSE_S
                    print("    .. %s %3.0fs: %s" % (
                        role, now - t0,
                        "%d chars of reasoning, %d of text%s" % (
                            sum(map(len, thinking)), sum(map(len, text)),
                            ", writing tool calls" if calls else "")
                        if thinking or text or calls else "waiting for the first byte"),
                        flush=True)

        threading.Thread(target=watch, daemon=True).start()
        try:
            # OpenRouter's event stream has no charset, and requests then decodes it as
            # Latin-1 ("NowogrÃ³dek", 17.10)
            r.encoding = "utf-8"
            for line in r.iter_lines(decode_unicode=True):
                # checked on every line, keep-alive comments included: OpenRouter sends
                # ": OPENROUTER PROCESSING" while a stuck upstream holds the request, and
                # the closed connection went on yielding them (18.10: four turns hung for
                # days and held both of the model's slots)
                if state["over"] or (budget_s and time.time() - t0 > budget_s + 5):
                    state["over"] = True
                    break
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                ev = json.loads(data)
                usage = ev.get("usage") or usage
                for ch in ev.get("choices") or []:
                    finish = ch.get("finish_reason") or finish
                    d = ch.get("delta") or {}
                    c = d.get("content")
                    if isinstance(c, str):
                        text.append(c)
                    elif isinstance(c, list):
                        for part in c:
                            if part.get("type") == "thinking":
                                thinking += [t.get("text", "") for t in part.get("thinking") or []]
                            elif part.get("type") == "text":
                                text.append(part.get("text", ""))
                    if d.get("reasoning"):              # OpenRouter's name for the same
                        thinking.append(d["reasoning"])
                    # the web plugin's sources (url_citation), for the planner's searches
                    sources += d.get("annotations") or ch.get("annotations") or []
                    for tc in d.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", len(calls)),
                                                {"id": None, "type": "function",
                                                 "function": {"name": "", "arguments": ""}})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["function"]["name"] += fn.get("name") or ""
                        args = fn.get("arguments")
                        slot["function"]["arguments"] += (args if isinstance(args, str)
                                                          else json.dumps(args) if args else "")
        except Exception as e:
            if state["over"]:
                raise LLMOverBudget("%s still answering after %.0f s (%d chars of reasoning)"
                                    % (role, time.time() - t0, sum(map(len, thinking))),
                                    "".join(thinking))
            raise
        finally:
            state["done"] = True
        if state["over"]:
            raise LLMOverBudget("%s still answering after %.0f s" % (role, time.time() - t0),
                                "".join(thinking))
        t = "".join(text)
        if thinking:
            content = [{"type": "thinking", "thinking": [{"type": "text", "text": "".join(thinking)}]}]
            if t:
                content.append({"type": "text", "text": t})
        else:
            content = t
        msg = {"role": "assistant", "content": content}
        if sources:
            msg["annotations"] = sources
        if calls:
            msg["tool_calls"] = [calls[i] for i in sorted(calls)]
        return msg, usage, finish

    def _log(self, rec):
        with self._lock, open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec).replace(self.key, "[REDACTED]") + "\n" if self.key else json.dumps(rec) + "\n")
