#!/usr/bin/env python3
"""Hearth + Habitat bridge (EXPERIMENT, not part of frozen Habitat 6.2).

What this is
------------
A separate wrapper around the unmodified ``hearth_combined.py``. It puts the Habitat
safety pattern in front of the one decision Hearth lets its proxy being "Claude" make:

  proposal (from a pluggable source)  ->  strict validation  ->  Hearth's own apply()
                                              |
                                  refused / unavailable  ->  deterministic fallback

Habitat ideas reused: strict whitelist, exact-key JSON, bounded text, a refusal log, a
deterministic fallback after repeated failures, and a handback only after a stable run
of good proposals. Nothing here edits ``hearth_combined.py`` or the Habitat 6.2 modules.

What it does NOT do (honest limits)
-----------------------------------
* It does not use Habitat's real bond, sleep or lifeboat modules; it copies the pattern.
* It has never talked to a real language model. The Ollama path is tested only against a
  stand-in local server.
* The browser viewer does not show bridge status; use the printed report or --log-json.
* It only controls Claude's choices (go / talk / stay by default). Births and the portal
  are switched off unless you pass --allow-births / --allow-portal.

No cloud model is ever used, and ANTHROPIC_API_KEY is ignored.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.util
import json
import os
import random
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

# hearth_combined.py builds this bridge was written and checked against.
PINNED = {
    "39c5387dffca346f2d0849402505d468d6d2454c7dc0a3707e0080ea449a1765":
        "earlier combined build, reviewed 2026-11-08 (before model births)",
}

ALL_ACTIONS = ("go", "talk", "propose_child", "use_bulb", "stay")
DEFAULT_ALLOWED = ("go", "talk", "stay")
KEYS = frozenset({"action", "target", "reason"})
MAX_TEXT = 2000
MAX_REASON = 200          # accepted length; Hearth itself keeps only the first 120
MAX_SUMMARY = 350
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
MAX_RESPONSE_BYTES = 256 * 1024


class BridgeError(RuntimeError):
    """Setup problem (wrong file, bad option)."""


class Refusal(ValueError):
    """A proposal that was not allowed through."""


# --------------------------------------------------------------------------
# Loading the unmodified Hearth file
# --------------------------------------------------------------------------
def load_hearth(path=None, allow_unpinned=False):
    path = path or os.path.join(HERE, "hearth_combined.py")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise BridgeError(f"cannot read {path}: {exc}") from exc
    digest = hashlib.sha256(data).hexdigest()
    if digest not in PINNED and not allow_unpinned:
        raise BridgeError(
            f"{os.path.basename(path)} has SHA-256 {digest}, which this bridge was not checked against. "
            "Review that file first, then pass --allow-unpinned to use it anyway.")
    spec = importlib.util.spec_from_file_location("hearth_combined", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hearth_combined"] = module
    spec.loader.exec_module(module)
    return module, digest


# --------------------------------------------------------------------------
# Strict validation
# --------------------------------------------------------------------------
def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise Refusal("duplicate key in reply")
        out[key] = value
    return out


def _clean_str(value, name, limit):
    if not isinstance(value, str):
        raise Refusal(f"{name} must be a string")
    if len(value) > limit:
        raise Refusal(f"{name} is too long")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise Refusal(f"{name} has control characters")
    return value


def _parse_object(text):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise Refusal("reply is missing or too long")
    try:
        data = json.loads(text.strip(), object_pairs_hook=_no_duplicates)
    except Refusal:
        raise
    except (ValueError, RecursionError):
        raise Refusal("reply is not JSON") from None
    if not isinstance(data, dict):
        raise Refusal("reply is not a JSON object")
    return data


def validate_proposal(text, world, hc, allowed=DEFAULT_ALLOWED):
    """Return a clean {"action","target","reason"} dict or raise Refusal."""
    data = _parse_object(text)
    if set(data) != KEYS:
        raise Refusal("reply must have exactly action, target and reason")
    action = _clean_str(data["action"], "action", 20)
    if action not in ALL_ACTIONS:
        raise Refusal("unknown action")
    if action not in allowed:
        raise Refusal(f"action '{action}' is switched off")
    target = _clean_str(data["target"], "target", 80)
    reason = _clean_str(data["reason"], "reason", MAX_REASON)
    proxy, realm = world.proxy, world.at
    here = [o for o in realm.alive() if o is not proxy and o.place == proxy.place]
    if action == "stay":
        if target != "":
            raise Refusal("stay takes an empty target")
    elif action == "go":
        if target not in hc.PLACES:
            raise Refusal("unknown place")
    elif action == "talk":
        if not any(o.name == target for o in here):
            raise Refusal("that being is not here")
    elif action == "propose_child":
        names = [n.strip() for n in target.split(",")]
        if not 1 <= len(names) <= 2 or len(set(names)) != len(names):
            raise Refusal("propose_child needs one or two different beings")
        if not all(any(o.name == n for o in here) for n in names):
            raise Refusal("those beings are not all here")
    elif action == "use_bulb":
        if target != "":
            raise Refusal("use_bulb takes an empty target")
        if proxy.place != realm.bulb_place:
            raise Refusal("the bulb is not here")
    return {"action": action, "target": target, "reason": reason[:120]}


def validate_summary(text):
    data = _parse_object(text)
    if set(data) != {"summary"}:
        raise Refusal("notes reply must have exactly one key, summary")
    return _clean_str(data["summary"], "summary", MAX_SUMMARY)


def lifeboat_action(world, hc):
    """Deterministic, safe choice: go where the lowest need is restored, or stay."""
    proxy = world.proxy
    need = min(hc.NEEDS, key=lambda n: proxy.needs[n])
    place = next(p for p, restores in hc.PLACES.items() if restores == need)
    if proxy.place == place:
        return {"action": "stay", "target": "", "reason": f"fallback: resting where {need} is restored"}
    return {"action": "go", "target": place, "reason": f"fallback: lowest need is {need}"}


# --------------------------------------------------------------------------
# Proposal sources
# --------------------------------------------------------------------------
class ScriptedProposer:
    """Offline source for demos and tests. Optionally sends a bad reply every Nth time."""

    def __init__(self, seed=7, noise=0):
        self.rng = random.Random(seed)
        self.noise = noise
        self.calls = 0
        self.bad = ['not json at all',
                    '{"action":"fly","target":"","reason":"x"}',
                    '{"action":"go","target":"Mars","reason":"x"}',
                    '{"action":"stay","target":"","reason":"x","extra":1}',
                    '["go"]']

    def poll(self, world, hc, system, prompt):
        self.calls += 1
        if prompt.startswith("REFLECT"):
            return "ready", json.dumps({"summary": f"Scripted notes at tick {world.tick}: "
                                                   "visit the places that restore my lowest need."})
        if self.noise and self.calls % self.noise == 0:
            return "ready", self.bad[(self.calls // self.noise) % len(self.bad)]
        proxy, realm = world.proxy, world.at
        here = [o for o in realm.alive() if o is not proxy and o.place == proxy.place]
        if here and self.rng.random() < 0.3:
            return "ready", json.dumps({"action": "talk", "target": self.rng.choice(here).name,
                                        "reason": "scripted: say hello"})
        return "ready", json.dumps({**lifeboat_action(world, hc), "reason": "scripted: follow my lowest need"})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OllamaClient:
    """Talks to an Ollama-style server on THIS machine only. Never follows redirects or proxies."""

    def __init__(self, model, url="http://127.0.0.1:11434"):
        if not isinstance(model, str) or not model.strip() or len(model) > 100 \
                or any(ord(c) < 32 for c in model):
            raise ValueError("model name is missing or invalid")
        if not isinstance(url, str):
            raise ValueError("url must be text")
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "http" or "@" in parts.netloc or parts.path not in ("", "/") \
                or parts.query or parts.fragment:
            raise ValueError("model server must be a plain http://127.0.0.1:PORT address")
        try:
            host, port = parts.hostname, parts.port
        except ValueError:
            raise ValueError("model server address is invalid") from None
        if host not in LOCAL_HOSTS:
            raise ValueError("only http://127.0.0.1, http://localhost or http://[::1] model servers are allowed")
        shown = f"[{host}]" if ":" in host else host
        self.model = model
        self.base = f"http://{shown}" + (f":{port}" if port else "")

    def complete(self, system, prompt, timeout):
        body = json.dumps({"model": self.model, "system": system, "prompt": prompt,
                           "stream": False, "format": "json"}).encode("utf-8")
        req = urllib.request.Request(self.base + "/api/generate", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, OSError) as exc:
            raise OSError(f"model server unreachable: {exc}") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("model reply too large")
        try:
            text = json.loads(raw).get("response")
        except (ValueError, AttributeError):
            raise ValueError("model server sent something unexpected") from None
        if not isinstance(text, str):
            raise ValueError("model server sent no text")
        return text


class OllamaProposer:
    """Asks a local model in the background so a slow model never stalls the world's tick."""

    def __init__(self, client, timeout=8.0):
        self.client, self.timeout = client, timeout
        self._lock = threading.Lock()
        self._busy = False
        self._result = None          # ("ready", text) or ("error", message)

    def _work(self, system, prompt):
        try:
            result = ("ready", self.client.complete(system, prompt, self.timeout))
        except (OSError, ValueError) as exc:
            result = ("error", str(exc)[:200])
        with self._lock:
            self._result, self._busy = result, False

    def poll(self, world, hc, system, prompt):
        with self._lock:
            if self._result is not None:
                result, self._result = self._result, None
                return result
            if self._busy:
                return "pending", None
            self._busy = True
        threading.Thread(target=self._work, args=(system, prompt), daemon=True).start()
        return "pending", None


# --------------------------------------------------------------------------
# The stand-in "client" that Hearth already knows how to call
# --------------------------------------------------------------------------
class BridgeClient:
    """Looks like the cloud client Hearth's decide() expects, but answers only with validated text."""

    def __init__(self, world, hc, proposer, allowed=DEFAULT_ALLOWED, max_failures=3, handback_after=2,
                 log_limit=500):
        self.world, self.hc, self.proposer = world, hc, proposer
        self.allowed = tuple(allowed)
        self.max_failures, self.handback_after = max_failures, handback_after
        self.mode = "model"
        self.failures = self.good_streak = 0
        self.counts = collections.Counter()
        self.handbacks = 0
        self.log = collections.deque(maxlen=log_limit)
        self.messages = types.SimpleNamespace(create=self._create)

    # Hearth calls client.messages.create(model=..., max_tokens=..., system=..., messages=[...])
    def _create(self, model=None, max_tokens=None, system=None, messages=None, **_ignored):
        prompt = ""
        if messages:
            last = messages[-1]
            prompt = last.get("content", "") if isinstance(last, dict) else ""
        text = self._reflect(system or "", prompt) if prompt.startswith("REFLECT") \
            else self._decide(system or "", prompt)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)])

    def _note(self, outcome, detail, action):
        self.counts[outcome] += 1
        if self.mode == "fallback":
            self.counts["fallback_ticks"] += 1
        self.log.append({"tick": self.world.tick, "mode": self.mode, "outcome": outcome,
                         "detail": detail, "action": action["action"], "target": action["target"]})

    def _decide(self, system, prompt):
        status, text = self.proposer.poll(self.world, self.hc, system, prompt)
        proposal, outcome, detail = None, "accepted", ""
        if status == "pending":
            outcome, detail = "pending", "no answer yet"
        elif status == "error":
            outcome, detail = "error", text or "model unavailable"
        else:
            try:
                proposal = validate_proposal(text, self.world, self.hc, self.allowed)
            except Refusal as exc:
                outcome, detail = "refused", str(exc)
        if outcome in ("refused", "error"):
            self.failures += 1
            self.good_streak = 0
            if self.mode == "model" and self.failures >= self.max_failures:
                self.mode = "fallback"
        elif outcome == "accepted":
            self.failures = 0
            self.good_streak += 1
            if self.mode == "fallback" and self.good_streak >= self.handback_after:
                self.mode = "model"
                self.handbacks += 1
        if outcome == "accepted" and self.mode == "model":
            chosen = proposal
        else:
            chosen = lifeboat_action(self.world, self.hc)
        self._note(outcome, detail, chosen)
        return json.dumps(chosen)

    def _reflect(self, system, prompt):
        status, text = self.proposer.poll(self.world, self.hc, system, prompt)
        if status == "ready":
            try:
                return json.dumps({"summary": validate_summary(text)})
            except Refusal as exc:
                self.counts["notes_refused"] += 1
                self.log.append({"tick": self.world.tick, "mode": self.mode, "outcome": "notes_refused",
                                 "detail": str(exc), "action": "", "target": ""})
        return json.dumps({"summary": ""})

    def report(self):
        return {"mode": self.mode, "handbacks": self.handbacks, **dict(self.counts)}


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------
def check_state_dir(state_path):
    folder = os.path.dirname(os.path.abspath(state_path))
    try:
        os.makedirs(folder, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".bridge_write_test_"):
            pass
    except OSError as exc:
        print(f"Cannot save files in: {folder} ({exc.strerror or exc}).\n"
              "Pick a folder you own with --state <path>.", file=sys.stderr)
        sys.exit(4)


def build_world(hc, proposer, args, allowed):
    world = hc.Together(seed=args.seed, state_path=args.state, fresh=args.fresh, llm=False,
                        model=None, budget=0, every=args.every, max_pop=args.max_pop, voices=False)
    client = BridgeClient(world, hc, proposer, allowed)
    world.client = client
    world.budget = 10 ** 9      # Hearth stops asking when budget runs out; the bridge has no paid calls
    return world, client


def write_log(path, client):
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".bridge_log_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for entry in client.log:
                fh.write(json.dumps(entry) + "\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def main(argv=None):
    p = argparse.ArgumentParser(description="Hearth + Habitat bridge (experiment). "
                                            "Validated, local-only decisions for Hearth's Claude.")
    p.add_argument("--proposer", choices=["scripted", "ollama"], default="scripted")
    p.add_argument("--llm-model", help="model name for --proposer ollama")
    p.add_argument("--llm-url", default="http://127.0.0.1:11434")
    p.add_argument("--llm-timeout", type=float, default=8.0)
    p.add_argument("--scripted-noise", type=int, default=0,
                   help="scripted source sends a bad reply every Nth decision (shows refusals and fallback)")
    p.add_argument("--allow-births", action="store_true", help="let Claude propose children (off by default)")
    p.add_argument("--allow-portal", action="store_true", help="let Claude use the light bulb (off by default)")
    p.add_argument("--hearth-file", help="path to hearth_combined.py (default: next to this script)")
    p.add_argument("--allow-unpinned", action="store_true",
                   help="use a hearth_combined.py this bridge was not checked against")
    p.add_argument("--state", default="bridge_state.json")
    p.add_argument("--fresh", action="store_true")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--ticks", type=int, help="run this many ticks headlessly instead of starting the viewer")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--interval", type=float, default=1.0, help="seconds per tick in the viewer")
    p.add_argument("--pace", type=float, default=0.0,
                   help="seconds to wait between ticks in a headless --ticks run (give a slow model time to answer)")
    p.add_argument("--every", type=int, default=5, help="ticks between Claude's decisions")
    p.add_argument("--max-pop", type=int, default=14)
    p.add_argument("--log-json", help="write the decision log to this file when the run ends")
    args = p.parse_args(argv)
    if (args.ticks is not None and args.ticks < 0) or args.interval <= 0 or args.every <= 0 \
            or args.max_pop < 8 or args.scripted_noise < 0 or args.llm_timeout <= 0 or args.pace < 0:
        p.error("ticks, noise and pace must be nonnegative; interval, every and timeout positive; max-pop at least 8")
    if args.proposer == "ollama" and not args.llm_model:
        p.error("--proposer ollama requires --llm-model")
    if args.proposer == "scripted" and args.llm_model:
        p.error("--llm-model needs --proposer ollama")
    if args.proposer == "ollama":
        try:
            client = OllamaClient(args.llm_model, args.llm_url)
        except ValueError as exc:
            p.error(str(exc))
        proposer = OllamaProposer(client, args.llm_timeout)
    else:
        proposer = ScriptedProposer(args.seed, args.scripted_noise)
    allowed = list(DEFAULT_ALLOWED)
    if args.allow_births:
        allowed.append("propose_child")
    if args.allow_portal:
        allowed.append("use_bulb")
    check_state_dir(args.state)
    try:
        hc, digest = load_hearth(args.hearth_file, args.allow_unpinned)
    except BridgeError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    try:
        world, client = build_world(hc, proposer, args, allowed)
        print(f"Bridge on hearth_combined.py {digest[:12]}; actions allowed: {', '.join(allowed)}", flush=True)
        try:
            if args.ticks is not None:
                for _ in range(args.ticks):
                    if world.woke or world.asleep:
                        break
                    world.step()
                    if args.pace:
                        time.sleep(args.pace)
                world.save()
            else:
                hc.serve(world, args.port, args.interval)
        finally:
            if args.log_json:
                write_log(args.log_json, client)
        print(f"Tick {world.tick}: Claude is in {world.proxy.place}, "
              f"{'alive' if world.proxy.alive else 'faded'}. Bridge report: {json.dumps(client.report())}")
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
