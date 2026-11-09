"""Tests for the Hearth + Habitat bridge experiment. Standard library only.

Run from the folder that holds hearth_bridge.py:   python -m unittest discover -s tests -v
"""
import argparse
import hashlib
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import hearth_bridge as hb  # noqa: E402

HC, DIGEST = hb.load_hearth()
BRIDGE = os.path.join(ROOT, "hearth_bridge.py")


def make_world(tmp, seed=7, noise=0, allowed=hb.DEFAULT_ALLOWED, proposer=None):
    args = argparse.Namespace(seed=seed, state=os.path.join(tmp, "state.json"), fresh=True, every=5, max_pop=14)
    proposer = proposer or hb.ScriptedProposer(seed, noise)
    return hb.build_world(HC, proposer, args, list(allowed))


def put_other_here(world):
    other = next(o for o in world.alive() if o is not world.proxy)
    other.place = world.proxy.place
    return other


def j(action, target="", reason="because"):
    return json.dumps({"action": action, "target": target, "reason": reason})


class Fake:
    """A proposal source that replays a fixed list of (status, text)."""
    def __init__(self, replies):
        self.replies = list(replies)

    def poll(self, world, hc, system, prompt):
        return self.replies.pop(0) if self.replies else ("pending", None)


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bridge_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class ValidatorTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.world, _ = make_world(self.tmp)
        self.world.proxy.place = "The Hearth"
        self.other = put_other_here(self.world)

    def check(self, text, allowed=hb.DEFAULT_ALLOWED):
        return hb.validate_proposal(text, self.world, HC, allowed)

    def test_accepts_good_actions(self):
        self.assertEqual(self.check(j("go", "The Stage"))["action"], "go")
        self.assertEqual(self.check(j("talk", self.other.name))["target"], self.other.name)
        self.assertEqual(self.check(j("stay"))["action"], "stay")

    def test_rejects_bad_replies(self):
        too_long = json.dumps({"action": "stay", "target": "", "reason": "x" * 5000})
        bad = ["", "   ", "not json", "[]", "null", '"go"', '{"action":"go"}',
               '{"action":"stay","target":"","reason":"x","extra":1}',
               '{"action":"stay","action":"go","target":"","reason":"x"}',
               '{"action":"go","action":"stay","target":"","reason":"x"}',
               j("fly"), j("go", "Mars"), j("go", ""), j("talk", "Nobody"), j("stay", "The Stage"),
               j("stay", "", "line one\nline two"), j("stay", "", "x" * 201),
               '{"action":123,"target":"","reason":"x"}', '{"action":"stay","target":null,"reason":"x"}',
               '{"action":"stay","target":"","reason":5}', too_long, "{" * 5000]
        for text in bad:
            with self.subTest(text=text[:40]):
                with self.assertRaises(hb.Refusal):
                    self.check(text)

    def test_risky_actions_are_off_by_default(self):
        with self.assertRaises(hb.Refusal):
            self.check(j("propose_child", self.other.name))
        with self.assertRaises(hb.Refusal):
            self.check(j("use_bulb"))

    def test_switched_on_actions_still_checked(self):
        allowed = hb.DEFAULT_ALLOWED + ("propose_child", "use_bulb")
        self.assertEqual(self.check(j("propose_child", self.other.name), allowed)["action"], "propose_child")
        with self.assertRaises(hb.Refusal):
            self.check(j("propose_child", "Nobody"), allowed)
        with self.assertRaises(hb.Refusal):
            self.check(j("propose_child", f"{self.other.name},{self.other.name}"), allowed)
        with self.assertRaises(hb.Refusal):
            self.check(j("use_bulb"), allowed)          # not standing at the bulb
        self.world.proxy.place = self.world.at.bulb_place
        self.assertEqual(self.check(j("use_bulb"), allowed)["action"], "use_bulb")
        with self.assertRaises(hb.Refusal):
            self.check(j("use_bulb", "The Stage"), allowed)

    def test_dead_or_absent_beings_cannot_be_talked_to(self):
        self.other.alive = False
        with self.assertRaises(hb.Refusal):
            self.check(j("talk", self.other.name))

    def test_reason_is_cut_to_120(self):
        out = self.check(j("stay", "", "y" * 200))
        self.assertEqual(len(out["reason"]), 120)

    def test_summary(self):
        self.assertEqual(hb.validate_summary('{"summary":"ok"}'), "ok")
        for bad in ['{"summary":"ok","x":1}', '{"summary":"' + "a" * 351 + '"}', '{"summary":"a\\nb"}',
                    '{"summary":5}', "nope", "[]"]:
            with self.subTest(bad=bad[:30]):
                with self.assertRaises(hb.Refusal):
                    hb.validate_summary(bad)

    def test_lifeboat_goes_to_lowest_need_and_is_deterministic(self):
        p = self.world.proxy
        for n in HC.NEEDS:
            p.needs[n] = 0.9
        p.needs["rest"] = 0.1
        p.place = "The Hearth"
        a = hb.lifeboat_action(self.world, HC)
        self.assertEqual((a["action"], a["target"]), ("go", "The Shore"))
        self.assertEqual(a, hb.lifeboat_action(self.world, HC))
        p.place = "The Shore"
        self.assertEqual(hb.lifeboat_action(self.world, HC)["action"], "stay")


class ClientTests(TmpCase):
    def make(self, replies, **kw):
        world, _ = make_world(self.tmp)
        world.proxy.place = "The Hearth"
        client = hb.BridgeClient(world, HC, Fake(replies), **kw)
        return world, client

    def ask(self, client, prompt="state"):
        return json.loads(client._create(system="s", messages=[{"role": "user", "content": prompt}])
                          .content[0].text)

    def test_accepted_proposal_passes_through(self):
        _, c = self.make([("ready", j("go", "The Stage", "hi"))])
        self.assertEqual(self.ask(c), {"action": "go", "target": "The Stage", "reason": "hi"})
        self.assertEqual(c.counts["accepted"], 1)

    def test_refused_proposal_gets_the_fallback_action(self):
        _, c = self.make([("ready", "garbage")])
        out = self.ask(c)
        self.assertTrue(out["reason"].startswith("fallback"))
        self.assertEqual(c.counts["refused"], 1)

    def test_fallback_after_three_failures_and_handback_after_two_good(self):
        _, c = self.make([("ready", "bad")] * 3 + [("ready", j("go", "The Stage", "m1")),
                                                   ("ready", j("go", "The Grove", "m2"))])
        for _ in range(3):
            self.ask(c)
        self.assertEqual(c.mode, "fallback")
        first = self.ask(c)                     # good, but not yet trusted
        self.assertTrue(first["reason"].startswith("fallback"))
        self.assertEqual(c.mode, "fallback")
        second = self.ask(c)                    # second good one hands back and is used
        self.assertEqual(second["reason"], "m2")
        self.assertEqual((c.mode, c.handbacks), ("model", 1))

    def test_two_failures_do_not_trigger_fallback(self):
        _, c = self.make([("ready", "bad")] * 2 + [("ready", j("stay"))])
        for _ in range(3):
            self.ask(c)
        self.assertEqual(c.mode, "model")

    def test_a_bad_answer_resets_the_handback_count(self):
        _, c = self.make([("ready", "bad")] * 3 + [("ready", j("stay")), ("ready", "bad"),
                                                   ("ready", j("stay"))])
        for _ in range(6):
            self.ask(c)
        self.assertEqual(c.mode, "fallback")

    def test_pending_is_not_a_failure_but_uses_the_fallback(self):
        _, c = self.make([("pending", None)] * 5)
        for _ in range(5):
            self.assertTrue(self.ask(c)["reason"].startswith("fallback"))
        self.assertEqual((c.mode, c.failures), ("model", 0))

    def test_errors_count_as_failures(self):
        _, c = self.make([("error", "down")] * 3)
        for _ in range(3):
            self.ask(c)
        self.assertEqual(c.mode, "fallback")

    def test_notes(self):
        _, c = self.make([("ready", '{"summary":"learned things"}'), ("ready", "garbage"),
                          ("ready", '{"summary":"x","y":1}'), ("pending", None)])
        self.assertEqual(self.ask(c, "REFLECT now")["summary"], "learned things")
        for _ in range(3):
            self.assertEqual(self.ask(c, "REFLECT now")["summary"], "")
        self.assertEqual(c.counts["notes_refused"], 2)

    def test_reply_has_the_shape_hearth_reads(self):
        _, c = self.make([("ready", j("stay"))])
        reply = c.messages.create(model="m", max_tokens=1, system="s",
                                  messages=[{"role": "user", "content": "x"}])
        self.assertIsInstance(reply.content[0].text, str)

    def test_log_is_bounded(self):
        _, c = self.make([("ready", j("stay"))] * 30, log_limit=10)
        for _ in range(30):
            self.ask(c)
        self.assertEqual(len(c.log), 10)


# ---- a stand-in model server on this machine (no real model is involved) ----
class Stub(http.server.BaseHTTPRequestHandler):
    mode = "ok"
    reply = ""
    seen = []
    other_port = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).seen.append(json.loads(body))
        mode = type(self).mode
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{type(self).other_port}/api/generate")
            self.end_headers()
            return
        if mode == "error":
            self.send_response(500)
            self.end_headers()
            return
        if mode == "slow":
            time.sleep(1.0)
        if mode == "huge":
            payload = b'{"response":"' + b"a" * 300000 + b'"}'
        elif mode == "junk":
            payload = b"<html>not json</html>"
        elif mode == "notext":
            payload = b'{"done":true}'
        else:
            payload = json.dumps({"response": type(self).reply}).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass


class Target(http.server.BaseHTTPRequestHandler):
    hits = []

    def log_message(self, *a):
        pass

    def _hit(self):
        type(self).hits.append(self.command)
        payload = b'{"response":"reached the other server"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _hit


class StubCase(unittest.TestCase):
    def setUp(self):
        Stub.mode, Stub.reply, Stub.seen = "ok", "", []
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.port}"


class OllamaClientTests(StubCase):
    def test_address_rules(self):
        for ok in ["http://127.0.0.1:11434", "http://localhost:11434", "http://[::1]:11434", "http://127.0.0.1",
                   "http://127.0.0.1:11434/"]:
            with self.subTest(url=ok):
                hb.OllamaClient("m", ok)
        for bad in ["https://127.0.0.1:11434", "http://example.com:11434", "http://127.0.0.1.evil.com",
                    "http://user@127.0.0.1", "http://evil.com@127.0.0.1", "http://127.0.0.1:11434/x",
                    "http://127.0.0.1:11434/?a=1", "http://0.0.0.0:11434", "ftp://127.0.0.1", "", None,
                    "http://127.0.0.1:99999"]:
            with self.subTest(url=bad):
                with self.assertRaises(ValueError):
                    hb.OllamaClient("m", bad)
        for bad_model in ["", "   ", "a\nb", "x" * 101, None, 5]:
            with self.subTest(model=bad_model):
                with self.assertRaises(ValueError):
                    hb.OllamaClient(bad_model)

    def test_request_shape_and_reply(self):
        Stub.reply = '{"action":"stay"}'
        text = hb.OllamaClient("tiny", self.url).complete("sys", "prompt", 2)
        self.assertEqual(text, '{"action":"stay"}')
        body = Stub.seen[0]
        self.assertEqual((body["model"], body["stream"], body["format"]), ("tiny", False, "json"))
        self.assertEqual((body["system"], body["prompt"]), ("sys", "prompt"))

    def test_bad_servers(self):
        c = hb.OllamaClient("m", self.url)
        for mode, exc in [("error", OSError), ("junk", ValueError), ("notext", ValueError), ("huge", ValueError)]:
            Stub.mode = mode
            with self.subTest(mode=mode):
                with self.assertRaises(exc):
                    c.complete("s", "p", 2)

    def test_timeout(self):
        Stub.mode = "slow"
        with self.assertRaises(OSError):
            hb.OllamaClient("m", self.url).complete("s", "p", 0.2)

    def test_redirects_are_not_followed(self):
        Target.hits = []
        other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Target)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        Stub.mode, Stub.other_port = "redirect", other.server_address[1]
        with self.assertRaises(OSError):
            hb.OllamaClient("m", self.url).complete("s", "p", 2)
        self.assertEqual(len(Stub.seen), 1)
        self.assertEqual(Target.hits, [])         # the redirect target was never contacted


class OllamaProposerTests(StubCase):
    def wait(self, proposer, limit=3.0):
        end = time.time() + limit
        while time.time() < end:
            status, text = proposer.poll(None, None, "s", "p")
            if status != "pending":
                return status, text
            time.sleep(0.02)
        self.fail("no answer")

    def test_answer_arrives_on_a_later_poll(self):
        Stub.reply = j("stay")
        p = hb.OllamaProposer(hb.OllamaClient("m", self.url), 2)
        self.assertEqual(p.poll(None, None, "s", "p"), ("pending", None))
        self.assertEqual(self.wait(p), ("ready", j("stay")))

    def test_errors_are_reported_not_raised(self):
        Stub.mode = "error"
        p = hb.OllamaProposer(hb.OllamaClient("m", self.url), 2)
        status, _ = self.wait(p)
        self.assertEqual(status, "error")

    def test_a_slow_model_never_blocks_the_caller(self):
        Stub.mode = "slow"
        p = hb.OllamaProposer(hb.OllamaClient("m", self.url), 5)
        start = time.time()
        for _ in range(5):
            p.poll(None, None, "s", "p")
        self.assertLess(time.time() - start, 0.5)

    def test_only_one_request_is_in_flight_at_a_time(self):
        gate = threading.Event()

        class Slow:
            calls = 0

            def complete(self, system, prompt, timeout):
                type(self).calls += 1
                gate.wait(5)
                return "{}"

        p = hb.OllamaProposer(Slow(), 5)
        p.poll(None, None, "s", "p")
        end = time.time() + 2
        while Slow.calls < 1 and time.time() < end:
            time.sleep(0.01)
        for _ in range(5):
            self.assertEqual(p.poll(None, None, "s", "p"), ("pending", None))
        time.sleep(0.15)
        self.assertEqual(Slow.calls, 1)
        gate.set()


class IntegrationTests(TmpCase):
    def run_world(self, ticks, **kw):
        world, client = make_world(self.tmp, **kw)
        seen = []
        original = world.apply

        def spy(text):
            seen.append(text)
            return original(text)
        world.apply = spy
        for _ in range(ticks):
            if world.woke or world.asleep:
                break
            world.step()
        return world, client, seen

    def test_world_runs_and_only_clean_text_reaches_hearth(self):
        world, client, seen = self.run_world(200)
        self.assertGreater(client.counts["accepted"], 20)
        self.assertTrue(seen)
        for text in seen:
            d = json.loads(text)
            self.assertEqual(set(d), set(hb.KEYS))
            self.assertIn(d["action"], hb.DEFAULT_ALLOWED)

    def test_no_births_or_portal_by_default(self):
        _, _, seen = self.run_world(200, seed=11)
        actions = {json.loads(t)["action"] for t in seen}
        self.assertFalse(actions & {"propose_child", "use_bulb"})

    def test_constant_bad_answers_put_the_fallback_in_charge(self):
        world, client, seen = self.run_world(150, noise=1)
        self.assertEqual(client.mode, "fallback")
        self.assertEqual(client.counts["accepted"], 0)
        self.assertGreater(client.counts["fallback_ticks"], 5)
        for text in seen:
            self.assertEqual(json.loads(text)["action"] in hb.DEFAULT_ALLOWED, True)

    def test_scripted_mode_uses_no_network(self):
        def refuse(*a, **k):
            raise AssertionError("network used")
        with mock.patch.object(socket.socket, "connect", refuse), \
                mock.patch.object(socket, "create_connection", refuse):
            self.run_world(80)

    def test_hearth_still_saves_and_reloads(self):
        world, client, _ = self.run_world(40)
        world.save()
        args = argparse.Namespace(seed=7, state=world.state_path, fresh=False, every=5, max_pop=14)
        again, _ = hb.build_world(HC, hb.ScriptedProposer(7), args, list(hb.DEFAULT_ALLOWED))
        self.assertEqual(again.tick, world.tick)

    def test_package_holds_the_pinned_hearth_file_unchanged(self):
        with open(os.path.join(ROOT, "hearth_combined.py"), "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), DIGEST)
        self.assertIn(DIGEST, hb.PINNED)

    def test_unknown_hearth_file_is_refused_unless_allowed(self):
        copy = os.path.join(self.tmp, "hearth_combined.py")
        shutil.copy(os.path.join(ROOT, "hearth_combined.py"), copy)
        with open(copy, "a", encoding="utf-8") as fh:
            fh.write("\n# changed\n")
        try:
            with self.assertRaises(hb.BridgeError):
                hb.load_hearth(copy)
            hb.load_hearth(copy, allow_unpinned=True)
        finally:
            hb.load_hearth()          # put the real module back


class CliTests(TmpCase):
    def run_cli(self, *args, timeout=120):
        return subprocess.run([sys.executable, "-I", BRIDGE, *args], capture_output=True, text=True,
                              timeout=timeout, cwd=self.tmp)

    def state(self, name="s.json"):
        return os.path.join(self.tmp, name)

    def test_headless_run_and_log(self):
        log = os.path.join(self.tmp, "log.jsonl")
        r = self.run_cli("--ticks", "40", "--fresh", "--state", self.state(), "--log-json", log)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Bridge report", r.stdout)
        with open(log, encoding="utf-8") as fh:
            lines = [json.loads(x) for x in fh]
        self.assertTrue(lines)
        self.assertTrue(os.path.exists(self.state()))

    def test_option_mistakes(self):
        cases = [("--proposer", "ollama"),
                 ("--proposer", "ollama", "--llm-model", "m", "--llm-url", "http://example.com:11434"),
                 ("--llm-model", "m"), ("--ticks", "-1"), ("--every", "0"), ("--pace", "-1")]
        for case in cases:
            with self.subTest(case=case):
                r = self.run_cli(*case, "--state", self.state())
                self.assertEqual(r.returncode, 2, r.stderr)

    def test_unwritable_state_folder(self):
        blocker = os.path.join(self.tmp, "afile")
        open(blocker, "w").close()
        r = self.run_cli("--ticks", "1", "--state", os.path.join(blocker, "sub", "s.json"))
        self.assertEqual(r.returncode, 4)
        self.assertIn("Cannot save files", r.stderr)

    def test_wrong_hearth_file_is_refused(self):
        other = os.path.join(self.tmp, "other.py")
        with open(other, "w") as fh:
            fh.write("print('hi')\n")
        r = self.run_cli("--ticks", "1", "--state", self.state(), "--hearth-file", other)
        self.assertEqual(r.returncode, 3)
        self.assertIn("not checked against", r.stderr)


class OllamaCliTests(StubCase, TmpCase):
    """Whole path with a stand-in local server: command line -> model client -> checks -> Hearth."""

    def setUp(self):
        StubCase.setUp(self)
        TmpCase.setUp(self)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-I", BRIDGE, *args], capture_output=True, text=True,
                              timeout=120, cwd=self.tmp)

    def test_good_local_model(self):
        Stub.reply = j("go", "The Grove", "stub model")
        r = self.run_cli("--proposer", "ollama", "--llm-model", "stub", "--llm-url", self.url,
                         "--ticks", "60", "--pace", "0.05", "--fresh",
                         "--state", os.path.join(self.tmp, "s.json"), "--llm-timeout", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout.split("Bridge report: ")[1])
        self.assertGreaterEqual(report["accepted"], 3)
        self.assertTrue(Stub.seen)
        self.assertEqual(Stub.seen[0]["format"], "json")

    def test_bad_local_model_falls_back(self):
        Stub.reply = "I would like to wander off"
        r = self.run_cli("--proposer", "ollama", "--llm-model", "stub", "--llm-url", self.url,
                         "--ticks", "120", "--pace", "0.05", "--fresh",
                         "--state", os.path.join(self.tmp, "s.json"), "--llm-timeout", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout.split("Bridge report: ")[1])
        self.assertEqual(report["mode"], "fallback")
        self.assertNotIn("accepted", report)

    def test_unreachable_model_falls_back(self):
        r = self.run_cli("--proposer", "ollama", "--llm-model", "stub", "--llm-url", "http://127.0.0.1:9",
                         "--ticks", "120", "--pace", "0.05", "--fresh",
                         "--state", os.path.join(self.tmp, "s.json"), "--llm-timeout", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout.split("Bridge report: ")[1])
        self.assertEqual(report["mode"], "fallback")


if __name__ == "__main__":
    unittest.main()
