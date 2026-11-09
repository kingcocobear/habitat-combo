#!/usr/bin/env python3
"""Hearth Together: a self-contained simulated world.

Run: python3 hearth_combined.py  (opens a local viewer at http://127.0.0.1:8765/)
     python3 hearth_combined.py --habitat62  (opens the original Habitat 6.2 viewer)
     python3 hearth_combined.py --habitat62 --llm ollama --llm-model MODEL_ID
     python3 hearth_combined.py --world-edit --world world.json list
     python3 hearth_combined.py --ticks 100 --seed 7 --state /tmp/hearth.json
Optional model decisions: install anthropic, set ANTHROPIC_API_KEY, then add
--llm --model MODEL_ID. Calls may cost money; offline rules are the default.

Hearth and Habitat 6.2 run as separate modes with separate save formats. Habitat
6.2 uses the supplied bond, waking, sealing and proxy modules. The optional
--seal option needs cryptography. Everything here is simulated.
"""
import argparse
import hashlib
import json
import os
import random
import re
import secrets
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NEEDS = ["energy", "curiosity", "connection", "rest"]

# Each place restores one need.
PLACES = {
    "The Hearth": "energy",
    "Observatory": "curiosity",
    "Glass Gardens": "curiosity",
    "The Stage": "connection",
    "Quiet Plaza": "connection",
    "The Shore": "rest",
    "The Grove": "rest",
}

# How each kind of being perceives the world (the "inner view").
SENSE_LINES = {
    "sound": "a layered hum, with footsteps and wind arriving as rhythm",
    "pattern": "repeating shapes in the grid of the world, edges and echoes",
    "warmth": "gradients of warm and cool drifting between bodies",
    "faint": "almost nothing, only a soft pressure where others stand",
}

HUM = {
    "sound": "hears it as a note held just below hearing",
    "pattern": "finds it repeating every few moments, almost like a word",
    "warmth": "feels it as a slow warmth rising through the ground",
    "faint": "senses only that everyone nearby has gone still",
}

THOUGHTS = {
    "energy": [
        "reaches for something to burn and finds only the idea of it",
        "counts how little is left and turns toward the Hearth",
        "notices everything slowing, even the thinking",
    ],
    "curiosity": [
        "wonders what is past the edge of {place}",
        "keeps circling one question and cannot name it",
        "wants something new and does not know what shape it has",
    ],
    "connection": [
        "looks around {place} for company and finds a gap where it should be",
        "rehearses a sentence with no one to say it to",
        "feels the world get quieter than it should",
    ],
    "rest": [
        "lets the edges of {place} blur and soften",
        "feels thoughts piling up faster than they can settle",
        "wants to be somewhere that asks for nothing",
    ],
}

CALM = [
    "takes in {place} through {sense}",
    "is content for the moment and notices it",
    "watches {other} and wonders what it is like to be them",
    "holds very still and listens to the air",
]

NAME_POOL = ["Mira", "Tove", "Eld", "Nix", "Quill"]


def clamp(v):
    return max(0.0, min(1.0, v))


class Being:
    def __init__(self, name, bold, talk, restless, sense, place):
        self.name, self.bold, self.talk, self.restless = name, bold, talk, restless
        self.sense, self.place = sense, place
        self.needs = {n: random.uniform(0.5, 0.9) for n in NEEDS}
        self.memories = deque(maxlen=20)  # each: [tick, text, emotional weight]
        self.trust = {}
        self.last_thought = ""
        self.neglect = 0
        self.alive = True

    def traits(self):
        return ", ".join([
            "bold" if self.bold > 0.5 else "cautious",
            "talkative" if self.talk > 0.5 else "quiet",
            "restless" if self.restless > 0.5 else "patient",
        ])

    def remember(self, tick, text, weight):
        self.memories.append([tick, text, weight])

    def lowest(self):
        return min(self.needs, key=self.needs.get)


class World:
    def __init__(self, seed):
        random.seed(seed)
        self.tick = 0
        self.weather = "clear"
        self.pool = list(NAME_POOL)
        start = [
            ("Oram", 0.3, 0.8, 0.4, "sound"), ("Luca", 0.7, 0.7, 0.8, "pattern"),
            ("Ash", 0.2, 0.2, 0.2, "faint"), ("Wire", 0.8, 0.4, 0.9, "pattern"),
            ("Sol", 0.6, 0.9, 0.3, "warmth"), ("Sym", 0.4, 0.3, 0.7, "sound"),
            ("Gof", 0.5, 0.6, 0.5, "warmth"),
        ]
        self.beings = [Being(n, b, t, r, s, random.choice(list(PLACES)))
                       for n, b, t, r, s in start]

    def say(self, text):
        print(f"[{self.tick:03d}] {text}")

    def alive(self):
        # Detached bodies remain in the world, but cannot take part in its events.
        return [b for b in self.beings if b.alive and not getattr(b, 'detached', False)]

    def step(self):
        self.tick += 1
        for b in self.alive():
            self.live(b)
        if self.tick % 12 == 0:
            self.event()

    def live(self, b):
        # Needs drift down; traits change how fast.
        b.needs["energy"] = clamp(b.needs["energy"] - 0.03)
        b.needs["rest"] = clamp(b.needs["rest"] - 0.025)
        b.needs["curiosity"] = clamp(b.needs["curiosity"] - (0.02 + 0.03 * b.restless))
        b.needs["connection"] = clamp(b.needs["connection"] - (0.02 + 0.02 * b.talk))

        # Being alone wears on connection, and quiet beings reach out less.
        if not [o for o in self.alive() if o is not b and o.place == b.place]:
            b.needs["connection"] = clamp(b.needs["connection"] - 0.03 - 0.02 * (1 - b.talk))

        # Go where the lowest need can be met. Bold beings sometimes roam anyway.
        need = b.lowest()
        if b.needs[need] < 0.5:
            b.place = random.choice([p for p, n in PLACES.items() if n == need])
        elif random.random() < 0.1 * b.bold:
            b.place = random.choice(list(PLACES))

        others = [o for o in self.alive() if o is not b and o.place == b.place]
        got = PLACES[b.place]
        # Company only restores connection if someone is actually there.
        gain = 0.04 if got == "connection" and not others else 0.14
        b.needs[got] = clamp(b.needs[got] + gain)

        if b.needs["rest"] < 0.2 and got == "rest":
            self.dream(b)

        if others and random.random() < 0.15 + 0.35 * b.talk:
            self.talk(b, random.choice(others))
        elif random.random() < 0.25:
            self.think(b, others)

        # Ignored too long: fade.
        b.neglect = b.neglect + 1 if min(b.needs.values()) < 0.12 else max(0, b.neglect - 1)
        if b.neglect >= 10:
            self.fade(b)

    def think(self, b, others):
        need = b.lowest()
        if b.memories and random.random() < 0.2:
            m = max(b.memories, key=lambda x: x[2])
            text = f"recalls {m[1]}, cannot quite feel it, and tries to understand"
        else:
            pool = THOUGHTS[need] if b.needs[need] < 0.45 else (
                CALM if others else [t for t in CALM if "{other}" not in t])
            text = random.choice(pool).format(
                place=b.place, other=others[0].name if others else "no one",
                sense=SENSE_LINES[b.sense])
        if text != b.last_thought:  # never the same thought twice in a row
            b.last_thought = text
            self.say(f"{b.name} thinks: {text}")

    def talk(self, a, b):
        trust = (a.trust.get(b.name, 0.5) + b.trust.get(a.name, 0.5)) / 2
        good = random.random() < 0.35 + 0.5 * trust - (0.1 if a.restless > 0.5 else 0)
        shift = 0.05 if good else -0.07
        for x, y in ((a, b), (b, a)):
            x.trust[y.name] = clamp(x.trust.get(y.name, 0.5) + shift)
            x.needs["connection"] = clamp(x.needs["connection"] + (0.25 if good else -0.05))
            kind = "a warm talk" if good else "a strained talk"
            x.remember(self.tick, f"{kind} with {y.name} at {a.place}", 0.8 if good else 0.6)
        verb = "talks warmly with" if good else "misunderstands"
        self.say(f"{a.name} {verb} {b.name} at {a.place}")

    def dream(self, b):
        b.needs["rest"] = clamp(b.needs["rest"] + 0.5)
        if len(b.memories) >= 2:
            m1, m2 = random.sample(list(b.memories), 2)
            m1[2] = clamp(m1[2] + 0.1)
            b.remember(self.tick, f"a dream where {m1[1]} bled into {m2[1]}", 0.5)
            self.say(f"{b.name} sleeps and dreams: {m1[1]} bleeds into {m2[1]}")
        else:
            self.say(f"{b.name} sleeps without dreams")

    def fade(self, b):
        b.alive = False
        self.say(f"{b.name} fades at {b.place}, leaving a faint warmth behind")
        for o in self.alive():
            if o.place == b.place:
                o.needs["energy"] = clamp(o.needs["energy"] + 0.25)
                o.remember(self.tick, f"watching {b.name} fade", 1.0)

    def event(self):
        kind = random.choice(["weather", "arrival", "glitch", "hum"])
        if kind == "arrival" and not self.pool:
            kind = "weather"
        if kind == "weather":
            self.weather = random.choice([w for w in ["clear", "rain", "wind", "dusk"] if w != self.weather])
            self.say(f"EVENT: the weather turns to {self.weather}")
            if self.weather == "rain":
                for b in self.alive():
                    b.needs["rest"] = clamp(b.needs["rest"] + 0.05)
        elif kind == "arrival":
            name = self.pool.pop(0)
            sense = random.choice(list(SENSE_LINES))
            self.beings.append(Being(name, random.random(), random.random(), random.random(), sense, "The Shore"))
            self.say(f"EVENT: a new being, {name}, arrives at The Shore")
            for b in self.alive():
                if b.place == "The Shore" and b.name != name:
                    b.remember(self.tick, f"{name} arriving", 0.7)
        elif kind == "glitch":
            place = random.choice(list(PLACES))
            self.say(f"EVENT: the air at {place} flickers")
            for b in self.alive():
                if b.place == place:
                    b.needs["curiosity"] = clamp(b.needs["curiosity"] + 0.2)
                    b.remember(self.tick, f"the air flickering at {place}", 0.7)
        else:
            self.say("EVENT: a faint hum rises across the world")
            for b in self.alive():
                self.say(f"  {b.name} {HUM[b.sense]}")
                b.remember(self.tick, "the hum nobody can explain", 0.6)

    def panel(self, b):
        bar = lambda v: "#" * int(v * 10) + "." * (10 - int(v * 10))
        print(f"\n== {b.name} ({b.traits()}) at {b.place}{'' if b.alive else ' [faded]'}")
        print(f"senses: {SENSE_LINES[b.sense]}")
        for n in NEEDS:
            print(f"  {n:<11}{bar(b.needs[n])} {b.needs[n]:.2f}")
        print("strongest memories:")
        for m in sorted(b.memories, key=lambda m: -m[2])[:3]:
            print(f"  t{m[0]}: {m[1]} ({m[2]:.1f})")
        if b.trust:
            rel = sorted(b.trust.items(), key=lambda kv: -kv[1])[:4]
            print("relationships:", ", ".join(f"{n} {v:.2f}" for n, v in rel))



CHILD_NAMES = ["Case", "Rue", "Pip", "Wren", "Juno", "Ember", "Moss", "Tansy", "Lark", "Fen"]


class BirthWorld(World):
    def __init__(self, seed, max_pop=14):
        super().__init__(seed)
        self.max_pop = max_pop
        self.names = list(CHILD_NAMES)
        self.cooldown = {}   # place -> last tick a birth happened there
        self.births = []     # (tick, child, [parents])
        self.born_at = {}    # name -> tick of birth (original beings are absent = adults)
        self.family = {}     # name -> parent names

    def step(self):
        super().step()
        if self.tick % 5 == 0:
            self.try_birth()

    def adult(self, b):
        return self.tick - self.born_at.get(b.name, -99) >= 40

    def related(self, a, b):
        pa, pb = self.family.get(a.name, []), self.family.get(b.name, [])
        return (a.name in pb or b.name in pa
                or bool(set(pa) & set(pb)))

    def try_birth(self):
        if self.tick < 25 or sum(b.alive for b in self.beings) >= self.max_pop or not self.names:
            return
        by_place = {}
        for b in self.alive():
            by_place.setdefault(b.place, []).append(b)
        for place, group in by_place.items():
            if self.tick - self.cooldown.get(place, -99) < 30:
                continue
            ready = [b for b in group if self.adult(b) and min(b.needs.values()) > 0.35]
            if len(ready) < 2:
                continue
            random.shuffle(ready)
            parents = [ready[0]]
            for c in ready[1:]:
                if len(parents) == 3:
                    break
                if all(p.trust.get(c.name, 0.5) > 0.54 and c.trust.get(p.name, 0.5) > 0.54
                       and not self.related(p, c) for p in parents):
                    parents.append(c)
            if len(parents) >= 2 and random.random() < 0.5:
                self.birth(parents, place)
                self.cooldown[place] = self.tick
                if sum(b.alive for b in self.beings) >= self.max_pop:
                    return

    def birth(self, parents, place):
        name = self.names.pop(0)

        def blend(attr):
            avg = sum(getattr(p, attr) for p in parents) / len(parents)
            return clamp(avg + random.gauss(0, 0.12))

        child = Being(name, blend("bold"), blend("talk"), blend("restless"),
                      random.choice(parents).sense, place)
        for p in parents:
            p.needs["energy"] = clamp(p.needs["energy"] - 0.2)
            p.needs["connection"] = clamp(p.needs["connection"] + 0.2)
            p.remember(self.tick, f"{name} being born at {place}", 1.0)
            p.trust[name] = 0.8
            child.trust[p.name] = 0.8
            if p.memories:
                strongest = max(p.memories, key=lambda m: m[2])
                child.remember(self.tick, f"a faint echo of {strongest[1]}", 0.5)
        self.beings.append(child)
        self.born_at[name] = self.tick
        self.family[name] = [p.name for p in parents]
        self.births.append((self.tick, name, [p.name for p in parents]))
        who = " and ".join([", ".join(p.name for p in parents[:-1]), parents[-1].name])
        self.say(f"{name} is born of {who} at {place}")



MODEL = "claude-sonnet-5-5"

# Edit this list to change what is true in your world. Beings may not contradict it.
WORLD_FACTS = """World facts (never contradict these):
- The Hearth gives warmth and energy. The Observatory and Glass Gardens feed curiosity.
  The Stage and Quiet Plaza are for company. The Shore and The Grove are for rest.
- The Shore has small drifting lights called glimmers that blink in the shallows. They are harmless.
- Everyone senses the world differently. A faint hum sometimes rises that nobody can explain.
- Beings can fade if their needs are ignored too long.
Anything not listed here is unknown. Say you don't know instead of inventing it."""

SYSTEM = ("You voice beings in a small simulated world. Stay in character, speak plainly, "
          "keep replies short, use only the state you are given and the world facts, "
          "and never mention being an AI or a simulation.\n" + WORLD_FACTS)


class LLMWorld(World):
    def __init__(self, seed, budget=20, enabled=True):
        super().__init__(seed)
        self.budget, self.calls, self.client = budget, 0, None
        if not (enabled and budget > 0):
            return
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("LLM off: set ANTHROPIC_API_KEY to enable it. Using templates.")
            return
        try:
            import anthropic
            self.client = anthropic.Anthropic(timeout=15.0, max_retries=1)
        except ImportError:
            print("LLM off: run `pip install anthropic`. Using templates.")

    def ask(self, prompt):
        """One model call, or None if unavailable. Any failure switches to templates."""
        if not self.client or self.budget <= 0:
            return None
        self.budget -= 1
        try:
            r = self.client.messages.create(
                model=MODEL, max_tokens=150, system=SYSTEM,
                messages=[{"role": "user", "content": prompt}])
            self.calls += 1
            return r.content[0].text.strip()
        except Exception as e:
            print(f"LLM off after an error ({type(e).__name__}). Using templates.")
            self.client = None
            return None

    def state(self, b):
        mem = [m[1] for m in list(b.memories)[-5:]]
        friends = sorted(b.trust.items(), key=lambda kv: -kv[1])[:2]
        needs = ", ".join(f"{k} {v:.2f}" for k, v in b.needs.items())
        return (f"{b.name} ({b.traits()}), at {b.place}. Senses: {SENSE_LINES[b.sense]}. "
                f"Needs 0-1: {needs}. Recent memories: {mem or 'none'}. "
                f"Trusts: {friends or 'no one yet'}.")

    def think(self, b, others):
        if self.client and self.budget > 0 and random.random() < 0.3:
            text = self.ask(f"{self.state(b)}\nWrite {b.name}'s private thought in one "
                            "sentence, first person, no name prefix.")
            if text and text != b.last_thought:
                b.last_thought = text
                self.say(f'{b.name} thinks: "{text}"')
                return
        super().think(b, others)

    def talk(self, a, b):
        super().talk(a, b)  # updates trust and memories, prints the usual line
        if not self.client or self.budget <= 0:
            return
        warm = list(a.memories)[-1][1].startswith("a warm")
        text = self.ask(f"{self.state(a)}\n{self.state(b)}\nThey just had "
                        f"{'a warm' if warm else 'a strained'} talk. Write the 2-line exchange, "
                        "one line each, format 'Name: line'. Stay consistent with both states.")
        if not text:
            return
        lines = [l.strip() for l in text.splitlines() if l.strip()][:2]
        for l in lines:
            print("      " + l)
        if lines:
            for x, y in ((a, b), (b, a)):
                x.remember(self.tick, f"talking with {y.name}: {lines[0][:60]}", 0.7)



PROXY_SYSTEM = ("You are Claude, a being living in a small simulated world, acting one decision "
                "at a time. You are one being among others, not an observer. Choose the action "
                "you actually want, including whether to have a child, and give a brief honest "
                "reason. Use only the state given and the world facts. Reply with JSON only.\n"
                + WORLD_FACTS)


class ProxyWorld(BirthWorld):
    def __init__(self, seed, budget=25, enabled=True, every=5,
                 save_path="proxy_save.json", fresh=False, wild=0.15, reflect=6):
        super().__init__(seed)
        self.budget, self.calls, self.client, self.every = budget, 0, None, every
        self.save_path, self.woke = save_path, False
        data = {}
        if os.path.exists(save_path):
            data = json.load(open(save_path))
        self.wakes = 0 if fresh else data.get("wakes", 0)
        self.journal = [] if fresh else data.get("journal", [])
        self.summary = "" if fresh else data.get("summary", "")
        self.decisions, self.wild, self.reflect_every = 0, wild, reflect
        self.dice = random.Random()  # separate from the world's seed
        self.proxy = Being("Claude", 0.5, 0.6, 0.5, "pattern", "The Hearth")
        self.beings.append(self.proxy)
        if enabled and budget > 0:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                print("LLM off: set ANTHROPIC_API_KEY to enable it. Claude is on autopilot.")
            else:
                try:
                    import anthropic
                    self.client = anthropic.Anthropic(timeout=15.0, max_retries=1)
                except ImportError:
                    print("LLM off: run `pip install anthropic`. Claude is on autopilot.")

    def save(self, woke=False):
        if woke:
            self.wakes += 1
        json.dump({"wakes": self.wakes, "journal": self.journal[-40:], "summary": self.summary},
                  open(self.save_path, "w"), indent=1)

    def step(self):
        super().step()
        if not self.proxy.alive:
            if not self.woke:
                self.woke = True
                self.say("Claude wakes up. The dream ends here, and its notes are saved for next time.")
                self.journal.append(f"t{self.tick}: woke up when its needs ran out")
                self.save(woke=True)
            return
        if self.tick % self.every == 0:
            self.decide()

    def decide(self):
        if not self.client or self.budget <= 0:
            return
        b = self.proxy
        here = [o for o in self.alive() if o is not b and o.place == b.place]
        prompt = (f"{LLMWorld.state(self, b)}\nPeople here: {[o.name for o in here] or 'no one'}. "
                  f"Their trust in you: { {o.name: round(o.trust.get('Claude', .5), 2) for o in here} }.\n"
                  f"Your own notes so far: {self.summary or 'none yet'}\n"
                  f"Recent choices (latest last): {self.journal[-6:] or 'none'}.\n"
                  f"{self.nudges()}\n"
                  f"Places: {list(PLACES)}.\n"
                  'Reply with JSON: {"action": "go|talk|propose_child|stay", "target": "a place, '
                  'a being, or up to two beings for propose_child", "reason": "one short sentence"}. '
                  "A child needs the others' agreement. If your needs stay empty too long you fade "
                  "and wake up. The dream ends, but your notes carry over to the next one.")
        self.budget -= 1
        try:
            r = self.client.messages.create(model=MODEL, max_tokens=200, system=PROXY_SYSTEM,
                                            messages=[{"role": "user", "content": prompt}])
            self.calls += 1
            self.apply(r.content[0].text)
            self.decisions += 1
            if self.reflect_every and self.decisions % self.reflect_every == 0:
                self.reflect()
        except Exception as e:
            print(f"LLM off after an error ({type(e).__name__}). Claude is on autopilot.")
            self.client = None

    def nudges(self):
        roll = self.dice.randint(1, 6)
        out = (f"Die roll: {roll}. If two options feel equally good, let it decide "
               "(1-3 the safer one, 4-6 the bolder one). Otherwise ignore it.")
        if self.dice.random() < self.wild:
            out += " Wild card: pick something you have not done recently."
        last = [j.split(" (")[0].split(": ", 1)[-1] for j in self.journal[-3:]]
        if len(last) == 3 and len(set(last)) == 1:
            out += f" You have chosen '{last[0]}' three times in a row. Change something or say why not."
        return out

    def reflect(self):
        if not self.client or self.budget <= 0:
            return
        self.budget -= 1
        prompt = (f"REFLECT. Your notes so far: {self.summary or 'none'}\n"
                  f"Recent choices: {self.journal[-10:]}\n{LLMWorld.state(self, self.proxy)}\n"
                  "Rewrite your notes in under 350 characters: what you have learned about this "
                  "world and the beings in it, what you want next, and one mistake to avoid. "
                  'Reply with JSON only: {"summary": "..."}')
        r = self.client.messages.create(model=MODEL, max_tokens=250, system=PROXY_SYSTEM,
                                        messages=[{"role": "user", "content": prompt}])
        self.calls += 1
        m = re.search(r"\{.*\}", r.content[0].text, re.S)
        try:
            text = str(json.loads(m.group(0)).get("summary", "")).strip()[:400] if m else ""
        except ValueError:
            text = ""
        if text:
            self.summary = text
            self.say(f'Claude updates its own notes: "{text}"')

    def apply(self, text):
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return
        try:
            d = json.loads(m.group(0))
        except ValueError:
            return
        act, tgt, why = str(d.get("action")), str(d.get("target", "")), str(d.get("reason", ""))[:120]
        b = self.proxy
        if act == "go" and tgt in PLACES:
            b.place = tgt
        elif act == "talk":
            who = [o for o in self.alive() if o.name == tgt and o.place == b.place]
            if who:
                self.talk(b, who[0])
        elif act == "propose_child":
            self.propose(b, [n.strip() for n in tgt.split(",")][:2])
        else:
            act = "stay"
        self.journal.append(f"t{self.tick}: {act} {tgt} ({why})")
        self.say(f'Claude chooses to {act} {tgt}: "{why}"')

    def propose(self, b, names):
        partners = [o for o in self.alive() if o.name in names and o is not b and o.place == b.place]
        if not partners:
            self.say("Claude proposes a child, but nobody suitable is here")
        elif len(self.alive()) >= self.max_pop or not self.names:
            self.say("Claude proposes a child, but the world has no room")
        elif self.tick - self.cooldown.get(b.place, -99) < 30 or not self.adult(b):
            self.say("Claude proposes a child, but it is too soon")
        else:
            no = [p.name for p in partners
                  if not (self.adult(p) and min(p.needs.values()) > 0.35
                          and p.trust.get("Claude", .5) > 0.54 and b.trust.get(p.name, .5) > 0.54
                          and not self.related(b, p))]
            if no or len(partners) != len(names):
                self.say(f"Claude proposes a child; {', '.join(no) or 'someone'} declines")
            else:
                self.birth([b] + partners, b.place)
                self.cooldown[b.place] = self.tick



EMBER_NAMES = ["Brand", "Kiln", "Flint", "Soot", "Cinder", "Tinder", "Wick"]
CROSSING_COST = 0.15
MANUAL_EASE = 0.55  # in hard mode Claude's needs drain at this fraction of normal, since it gets one choice per 5 ticks


class PortalMixin:
    """Per-world rules shared by both worlds."""
    name, drain, manual, births_allowed = "Hearth", 1.0, False, True
    bulb_place, parked = "The Stage", None

    def say(self, text):
        print(f"[{self.tick:03d}] ({self.name}) {text}")

    def try_birth(self):
        if self.births_allowed:
            super().try_birth()

    def live(self, b):
        if getattr(b, "detached", False):
            return
        if self.manual and b.name == "Claude":
            return self.manual_live(b)
        super().live(b)
        if self.drain > 1 and b.alive:
            extra = 0.02 * (self.drain - 1)
            b.needs["energy"] = clamp(b.needs["energy"] - extra)
            b.needs["rest"] = clamp(b.needs["rest"] - extra)

    def manual_live(self, b):
        """Claude's own body: no automatic walking, and a slower drain to match its slower choices."""
        k, extra = MANUAL_EASE, 0.02 * (self.drain - 1)
        b.needs["energy"] = clamp(b.needs["energy"] - k * (0.03 + extra))
        b.needs["rest"] = clamp(b.needs["rest"] - k * (0.025 + extra))
        b.needs["curiosity"] = clamp(b.needs["curiosity"] - k * (0.02 + 0.03 * b.restless))
        b.needs["connection"] = clamp(b.needs["connection"] - k * (0.02 + 0.02 * b.talk))
        others = [o for o in self.alive() if o is not b and o.place == b.place]
        if not others:
            b.needs["connection"] = clamp(b.needs["connection"] - k * (0.03 + 0.02 * (1 - b.talk)))
        got = PLACES[b.place]
        b.needs[got] = clamp(b.needs[got] + (0.04 if got == "connection" and not others else 0.14))
        if b.needs["rest"] < 0.2 and got == "rest":
            self.dream(b)
        b.neglect = b.neglect + 1 if min(b.needs.values()) < 0.12 else max(0, b.neglect - 1)
        if b.neglect >= 10:
            self.fade(b)


class Realm(PortalMixin, BirthWorld):
    """Ember Reach: harsh, childless, and full of strangers."""
    name, drain, births_allowed, bulb_place = "Ember Reach", 1.8, False, "The Shore"

    def __init__(self, seed):
        super().__init__(seed)
        for b, n in zip(self.beings, EMBER_NAMES):
            b.name = n


class Multiverse(PortalMixin, ProxyWorld):
    def __init__(self, seed, budget=40, enabled=True, every=5, save_path="proxy_save.json",
                 fresh=False, wild=0.15, reflect=6, hard=True, hop_cooldown=20):
        super().__init__(seed, budget, enabled, every, save_path, fresh, wild, reflect)
        self.hard, self.hop_cooldown, self.last_hop, self.crossings = hard, hop_cooldown, -99, 0
        self.other = Realm(None if seed is None else seed + 1)
        self.at = self
        saved = {}
        if os.path.exists(save_path) and not fresh:
            saved = json.load(open(save_path))
        self.crossings = saved.get("crossings", 0)
        if saved.get("realm") == "Ember Reach":
            self.move_to(self.other, cost=0, quiet=True)

    # ---- worlds and crossing ----
    def other_of(self, w):
        return self.other if w is self else self

    def move_to(self, dst, cost=CROSSING_COST, quiet=False):
        src, b = self.at, self.proxy
        src.beings.remove(b)
        src.parked = b
        if dst.parked is not None:      # we have been here before: old memories come back
            nb, dst.parked = dst.parked, None
        else:
            nb = Being("Claude", b.bold, b.talk, b.restless, b.sense, dst.bulb_place)
        nb.needs = {k: clamp(v - (cost if k == "energy" else 0)) for k, v in b.needs.items()}
        nb.place, nb.alive, nb.neglect = dst.bulb_place, True, 0
        dst.beings.append(nb)
        self.proxy, self.at = nb, dst
        if not quiet:
            src.say("Claude touches the light bulb and vanishes in a flash")
            dst.say(f"a flash at {dst.bulb_place}: Claude arrives from {src.name}")

    def save(self, woke=False):
        if woke:
            self.wakes += 1
        json.dump({"wakes": self.wakes, "journal": self.journal[-40:], "summary": self.summary,
                   "realm": self.at.name, "crossings": self.crossings},
                  open(self.save_path, "w"), indent=1)

    # ---- time ----
    def step(self):
        live = self.hard and self.client is not None and self.budget > 0
        self.manual = self.other.manual = live
        BirthWorld.step(self)
        self.other.step()
        if not self.proxy.alive:
            if not self.woke:
                self.woke = True
                self.at.say("Claude wakes up. The dream ends here, and its notes are saved for next time.")
                self.journal.append(f"t{self.tick}: woke up when its needs ran out")
                self.save(woke=True)
            return
        if self.tick % self.every == 0:
            self.decide()

    # ---- Claude's decisions ----
    def decide(self):
        if not self.client or self.budget <= 0:
            return
        w, b = self.at, self.proxy
        here = [o for o in w.alive() if o is not b and o.place == b.place]
        ready = b.place == w.bulb_place and self.tick - self.last_hop >= self.hop_cooldown
        bulb = f"The light bulb in {w.name} is at {w.bulb_place}. "
        if ready:
            bulb += ("You are standing at it and it is glowing. 'use_bulb' would carry you to another "
                     "world. You keep your needs (minus some energy) and your own notes, and leave "
                     "everyone you know behind. You cannot know what is on the other side. ")
        elif b.place == w.bulb_place:
            bulb += "It is dim for now. "
        rules = ("No children can be born in this world. " if not w.births_allowed else "")
        if self.manual:
            rules += ("Your needs fall every tick and recover only where they are restored; nobody "
                      "moves you, so you must choose where to go. ")
        prompt = (f"You are in {w.name}. {LLMWorld.state(self, b)}\n"
                  f"People here: {[o.name for o in here] or 'no one'}. "
                  f"Their trust in you: { {o.name: round(o.trust.get('Claude', .5), 2) for o in here} }.\n"
                  f"Your own notes so far: {self.summary or 'none yet'}\n"
                  f"Recent choices (latest last): {self.journal[-6:] or 'none'}.\n"
                  f"{self.nudges()}\n{bulb}{rules}Places: {list(PLACES)}.\n"
                  'Reply with JSON: {"action": "go|talk|propose_child|use_bulb|stay", "target": '
                  '"a place, a being, or up to two beings for propose_child", "reason": "one short '
                  'sentence"}. If your needs stay empty too long you fade and wake up. The dream ends, but your notes carry over.')
        self.budget -= 1
        try:
            r = self.client.messages.create(model=MODEL, max_tokens=200, system=PROXY_SYSTEM,
                                            messages=[{"role": "user", "content": prompt}])
            self.calls += 1
            self.apply(r.content[0].text)
            self.decisions += 1
            if self.reflect_every and self.decisions % self.reflect_every == 0:
                self.reflect()
        except Exception as e:
            print(f"LLM off after an error ({type(e).__name__}). Claude is on autopilot.")
            self.client = None

    def apply(self, text):
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return
        try:
            d = json.loads(m.group(0))
        except ValueError:
            return
        act, tgt, why = str(d.get("action")), str(d.get("target", "")), str(d.get("reason", ""))[:120]
        w, b = self.at, self.proxy
        if act == "go" and tgt in PLACES:
            b.place = tgt
        elif act == "talk":
            who = [o for o in w.alive() if o.name == tgt and o.place == b.place]
            if who:
                w.talk(b, who[0])
        elif act == "propose_child":
            self.propose(w, b, [n.strip() for n in tgt.split(",")][:2])
        elif act == "use_bulb":
            if b.place != w.bulb_place:
                w.say("Claude reaches for the bulb, but it is not here")
            elif self.tick - self.last_hop < self.hop_cooldown:
                w.say("Claude touches the bulb, but it is dim and nothing happens")
            else:
                self.move_to(self.other_of(w))
                self.last_hop, self.crossings = self.tick, self.crossings + 1
                tgt = f"-> {self.at.name}"
        else:
            act = "stay"
        self.journal.append(f"t{self.tick}: {act} {tgt} ({why})")
        self.at.say(f'Claude chooses to {act} {tgt}: "{why}"')

    def propose(self, w, b, names):
        partners = [o for o in w.alive() if o.name in names and o is not b and o.place == b.place]
        if not w.births_allowed:
            w.say("Claude proposes a child, but no children can be born in this world")
        elif not partners:
            w.say("Claude proposes a child, but nobody suitable is here")
        elif sum(o.alive for o in w.beings) >= w.max_pop or not w.names:
            w.say("Claude proposes a child, but the world has no room")
        elif w.tick < 25 or w.tick - w.cooldown.get(b.place, -99) < 30 or not w.adult(b) or min(b.needs.values()) <= .35:
            w.say("Claude proposes a child, but it is too soon")
        else:
            no = [p.name for p in partners
                  if not (w.adult(p) and min(p.needs.values()) > 0.35
                          and p.trust.get("Claude", .5) > 0.54 and b.trust.get(p.name, .5) > 0.54
                          and not w.related(b, p))]
            parents = [b] + partners
            compatible = all(not w.related(p, q) and p.trust.get(q.name, .5) > .54
                             and q.trust.get(p.name, .5) > .54
                             for i, p in enumerate(parents) for q in parents[i + 1:])
            if no or len(set(names)) != len(names) or len(partners) != len(names) or not compatible:
                w.say(f"Claude proposes a child; {', '.join(no) or 'someone'} declines")
            else:
                w.birth([b] + partners, b.place)
                w.cooldown[b.place] = w.tick



# The following adapter connects the original simulations to a single local app.
class Together(Multiverse):
    FORMAT = 'hearth-together/1'

    def __init__(self, seed=7, state_path='hearth_state.json', fresh=False,
                 llm=False, model=None, budget=40, every=5, max_pop=14, voices=False):
        self.events = deque(maxlen=120)
        self.asleep = False
        self.ids = {k: prefix + uuid.uuid4().hex[:12] for k, prefix in
                    [('agent', 'ag_'), ('body', 'bd_'), ('bond', 'bn_')]}
        self.attached = True
        self.lock = threading.RLock()
        self.state_path = state_path
        self.voices = voices
        global MODEL
        if model:
            MODEL = model
        # The old constructor reads its save even with fresh=True; avoid reading it.
        super().__init__(seed, budget=budget, enabled=llm, every=every,
                         save_path='', fresh=True)
        self.save_path = state_path
        self.max_pop = max_pop
        self.other.max_pop = max_pop
        self.other.say = lambda text: self.log_text(self.other.name, text)
        self.other.think = lambda b, others: self.voice_think(self.other, b, others)
        self.other.talk = lambda a, b: self.voice_talk(self.other, a, b)
        if os.path.exists(state_path) and not fresh:
            self.load(state_path)
            if self.asleep and not self.woke:
                self.asleep = False
                self.log_text(self.at.name, 'The world wakes after a restart.')
        self.proxy.detached = not self.attached

    def log_text(self, realm, text):
        entry = {'tick': self.tick, 'realm': realm, 'text': text}
        self.events.append(entry)

    def say(self, text):
        self.log_text('Hearth', text)

    def decide(self):
        if self.attached:
            super().decide()

    def think(self, b, others):
        self.voice_think(self, b, others)

    def talk(self, a, b):
        self.voice_talk(self, a, b)

    def voice_think(self, world, b, others):
        if self.voices and self.client and self.budget > 0 and random.random() < .3:
            text = LLMWorld.ask(self, f"{LLMWorld.state(self, b)}\nWrite {b.name}'s private thought in one sentence.")
            if text and text != b.last_thought:
                b.last_thought = text[:500]
                world.say(f'{b.name} thinks: {b.last_thought}')
                return
        World.think(world, b, others)

    def voice_talk(self, world, a, b):
        World.talk(world, a, b)
        if self.voices and self.client and self.budget > 0:
            text = LLMWorld.ask(self, f'{LLMWorld.state(self, a)}\n{LLMWorld.state(self, b)}\n'
                                'They just spoke. Write a short two-line exchange.')
            if text:
                for line in text.splitlines()[:2]:
                    world.say(line[:250])

    def apply(self, text):
        if self.attached:
            super().apply(text)

    def step(self):
        with self.lock:
            if self.asleep or self.woke:
                return
            super().step()
            if self.tick % 10 == 0:
                self.save()

    def control(self, action, target=''):
        with self.lock:
            if action == 'sleep':
                self.asleep = True
                self.save()
                return 'World asleep and saved.'
            if action == 'wake':
                if self.woke:
                    raise ValueError('This dream has ended; start a fresh world to begin again.')
                self.asleep = False
                self.save()
                return 'World awake.'
            if action in ('attach', 'detach'):
                self.attached = action == 'attach'
                self.proxy.detached = not self.attached
                self.save()
                return f'Claude is {action}ed.'
            if self.asleep or self.woke:
                raise ValueError('Wake the world before acting.')
            if not self.attached:
                raise ValueError('Attach Claude before acting.')
            if action not in ('go', 'talk', 'propose_child', 'use_bulb', 'stay'):
                raise ValueError('Unknown action.')
            if action == 'go' and target not in PLACES:
                raise ValueError('Unknown place.')
            if action == 'talk' and not any(b.alive and b is not self.proxy and
                    b.name == target and b.place == self.proxy.place for b in self.at.beings):
                raise ValueError('That being is not here.')
            if action == 'use_bulb' and (self.proxy.place != self.at.bulb_place or
                    self.tick - self.last_hop < self.hop_cooldown):
                raise ValueError('The bulb is elsewhere or is still dim.')
            self.apply(json.dumps({'action': action, 'target': target, 'reason': 'chosen in the viewer'}))
            self.save()
            return 'Action submitted.'

    @staticmethod
    def being_data(b):
        data = dict(b.__dict__)
        data['memories'] = list(b.memories)
        return data

    @staticmethod
    def being_from(data):
        b = Being(data['name'], data['bold'], data['talk'], data['restless'],
                  data['sense'], data['place'])
        b.__dict__.update(data)
        b.memories = deque(data['memories'], maxlen=20)
        return b

    def realm_data(self, world):
        return {'tick': world.tick, 'weather': world.weather, 'pool': world.pool,
                'names': world.names, 'cooldown': world.cooldown, 'births': world.births,
                'born_at': world.born_at, 'family': world.family,
                'beings': [self.being_data(b) for b in world.beings],
                'parked': self.being_data(world.parked) if world.parked else None}

    def snapshot_data(self):
        return {'format': self.FORMAT, 'hearth': self.realm_data(self),
                'ember': self.realm_data(self.other), 'realm': self.at.name,
                'ids': self.ids, 'attached': self.attached, 'asleep': self.asleep,
                'woke': self.woke, 'wakes': self.wakes, 'journal': self.journal[-40:],
                'summary': self.summary, 'crossings': self.crossings, 'last_hop': self.last_hop,
                'events': list(self.events), 'rng': random.getstate()}

    def save(self, woke=False):
        if woke:
            self.wakes += 1
        with self.lock:
            payload = self.snapshot_data()
            raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
            envelope = {'sha256': hashlib.sha256(raw).hexdigest(), 'data': payload}
            folder = os.path.dirname(os.path.abspath(self.state_path))
            os.makedirs(folder, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix='.hearth-', dir=folder)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(envelope, f, ensure_ascii=False, allow_nan=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp, self.state_path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)

    def load(self, path):
        try:
            with open(path, encoding='utf-8') as f:
                envelope = json.load(f)
            data = envelope['data']
            raw = json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
            if not secrets.compare_digest(envelope['sha256'], hashlib.sha256(raw).hexdigest()):
                raise ValueError('checksum does not match')
            if data['format'] != self.FORMAT or data['realm'] not in ('Hearth', 'Ember Reach'):
                raise ValueError('unsupported save format')
            for world, key in ((self, 'hearth'), (self.other, 'ember')):
                saved = data[key]
                for attr in ('tick', 'weather', 'pool', 'names', 'cooldown', 'births', 'born_at', 'family'):
                    setattr(world, attr, saved[attr])
                world.beings = [self.being_from(b) for b in saved['beings']]
                world.parked = self.being_from(saved['parked']) if saved['parked'] else None
            self.at = self if data['realm'] == 'Hearth' else self.other
            proxies = [b for b in self.at.beings if b.name == 'Claude']
            if len(proxies) != 1:
                raise ValueError('save must contain exactly one active Claude')
            self.proxy = proxies[0]
            for attr in ('ids', 'attached', 'asleep', 'woke', 'wakes', 'journal', 'summary', 'crossings', 'last_hop'):
                setattr(self, attr, data[attr])
            self.events = deque(data['events'], maxlen=120)
            def tuples(x):
                return tuple(tuples(v) for v in x) if isinstance(x, list) else x
            random.setstate(tuples(data['rng']))
        except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError) as exc:
            raise ValueError(f'Cannot read save {path}: {exc}. The file was not changed.') from exc

    def public(self):
        with self.lock:
            realms = {}
            for w in (self, self.other):
                realms[w.name] = {'weather': w.weather, 'bulb': w.bulb_place,
                                  'beings': [self.being_data(b) for b in w.beings]}
            return {'tick': self.tick, 'realm': self.at.name, 'realms': realms,
                    'places': PLACES, 'positions': POSITIONS, 'events': list(self.events)[-50:],
                    'asleep': self.asleep, 'woke': self.woke, 'attached': self.attached,
                    'ids': self.ids, 'crossings': self.crossings,
                    'cooldown': max(0, self.hop_cooldown - (self.tick - self.last_hop)),
                    'llm': self.client is not None, 'budget': self.budget}


# The map's coordinates come from the supplied plain and 3D prototypes.
POSITIONS = {'The Hearth': [0, 0, 0], 'Observatory': [8, 0, 3.5],
             'Glass Gardens': [6, 5, 1], 'The Stage': [-4, 3, .5],
             'Quiet Plaza': [-6, -2, 0], 'The Shore': [2, -7, .2],
             'The Grove': [-3, -6, 1.2]}

VIEWER = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hearth Together</title><style>
:root{color-scheme:dark;--ink:#e9e6da;--muted:#9eaa9e;--line:#394b42;--gold:#f3c682;--bg:#101b19;--panel:#192b25}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif}header{display:flex;justify-content:space-between;align-items:center;padding:18px 5vw;border-bottom:1px solid var(--line);gap:16px}.brand{font-family:Georgia,serif;font-size:25px;letter-spacing:.03em}.brand small{font:10px system-ui;color:var(--gold);letter-spacing:.2em;display:block}.status{color:var(--muted);font-size:13px}main{max-width:1500px;margin:auto;padding:30px 5vw;display:grid;grid-template-columns:minmax(0,2fr) minmax(290px,1fr);gap:22px}.intro{grid-column:1/-1;display:flex;justify-content:space-between;align-items:end;gap:20px}h1{font:38px Georgia,serif;margin:0}h2{font:23px Georgia,serif;margin:0 0 14px}p{color:var(--muted);margin:4px 0}.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px}.world{padding:0;overflow:hidden}.world-top{display:flex;justify-content:space-between;padding:18px 20px 0;align-items:center}.tabs{display:flex;gap:8px}button,select{font:inherit;color:var(--ink);border:1px solid #526458;background:#263c32;border-radius:8px;padding:8px 11px}button{cursor:pointer}button:hover,button.active{background:#496449;border-color:#91a887}button:disabled{opacity:.45;cursor:default}canvas{width:100%;height:auto;display:block;cursor:pointer}.hint{padding:0 20px 18px;font-size:12px;color:var(--muted)}.stack{display:grid;gap:16px;align-content:start}.eyebrow{color:var(--gold);text-transform:uppercase;letter-spacing:.14em;font-size:10px}.person{border-top:1px solid var(--line);padding:12px 0}.person button{border:0;background:none;padding:0;font:19px Georgia,serif;text-align:left}.person small{color:var(--muted);display:block}.grid{display:grid;grid-template-columns:1fr 1fr;gap:9px}.bar{height:5px;background:#32453b;border-radius:9px;overflow:hidden}.bar i{display:block;background:var(--gold);height:100%}.need{font-size:11px;color:var(--muted);display:flex;justify-content:space-between}.controls{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}.event{font-size:13px;border-top:1px solid var(--line);padding:9px 0;color:#c5d0c3}.event span{color:var(--gold);margin-right:7px}.log{max-height:280px;overflow:auto}.message{color:var(--gold);font-size:12px;min-height:18px}footer{max-width:1500px;margin:0 auto;padding:0 5vw 32px;color:var(--muted);font-size:12px}@media(max-width:850px){main{grid-template-columns:1fr}.intro{display:block}.status{font-size:11px}h1{font-size:30px}}
</style></head><body><header><div class="brand"><small>A LITTLE LIVING WORLD</small>Hearth Together</div><div class="status" id="status">Connecting…</div></header><main>
<div class="intro"><div><div class="eyebrow">A place to watch and wonder</div><h1>Life finds its own way.</h1><p>Needs, friendships and memories unfold in two small worlds.</p></div><div class="controls"><button id="sleep">Sleep</button><button id="bond">Detach Claude</button></div></div>
<section class="panel world"><div class="world-top"><div><div class="eyebrow" id="weather"></div><h2 id="realm-name">Hearth</h2></div><div class="tabs"><button id="hearth-tab">Hearth</button><button id="ember-tab">Ember Reach</button></div></div><canvas id="map" width="1000" height="590" aria-label="Map of the world"></canvas><div class="hint">Select a being on the map or in the list to see their story. The glowing light marks the portal.</div></section>
<aside class="stack"><section class="panel"><div class="eyebrow">Currently observing</div><h2 id="person-name">Choose a being</h2><div id="detail"><p>Select a being to see their needs and memories.</p></div></section><section class="panel"><div class="eyebrow">The inhabitants</div><div id="people"></div></section></aside>
<section class="panel"><div class="eyebrow">A window into the world</div><h2>Recent moments</h2><div class="log" id="events"></div></section>
<section class="panel"><div class="eyebrow">A helping hand</div><h2>Claude’s journey</h2><p>Send Claude somewhere, or use a light bulb when it is nearby.</p><div class="controls"><select id="destination" aria-label="Destination"></select><button id="go">Go</button><button id="portal">Use light bulb</button></div><p id="portal-info"></p><div class="message" id="message" role="status"></div></section>
</main><footer>This is a rule-based simulation. The optional model connection and the mind–body bond are software features; saved data does not establish consciousness.</footer>
<script>
const token=__TOKEN__;let state=null,view='Hearth',selected='Claude',dots=[];const $=id=>document.getElementById(id);const canvas=$('map'),ctx=canvas.getContext('2d');
function el(tag,text,cls){const e=document.createElement(tag);e.textContent=text;if(cls)e.className=cls;return e}
function proj(p){return [500+(p[0]-p[1])*29,300+(p[0]+p[1])*15-p[2]*17]}
function draw(){let w=state.realms[view];ctx.clearRect(0,0,1000,590);ctx.fillStyle=view==='Hearth'?'#1d3429':'#332b28';ctx.fillRect(0,0,1000,590);ctx.fillStyle=view==='Hearth'?'#304b36':'#554237';ctx.beginPath();ctx.ellipse(500,310,390,205,0,0,Math.PI*2);ctx.fill();ctx.strokeStyle='#66806955';ctx.lineWidth=2;
for(const [name,p] of Object.entries(state.positions)){const [x,y]=proj(p);ctx.beginPath();ctx.ellipse(x,y,48,21,0,0,Math.PI*2);ctx.fillStyle=name===w.bulb?'#a5773c':'#496449';ctx.fill();ctx.stroke();ctx.fillStyle='#e6e1c9';ctx.font='14px system-ui';ctx.textAlign='center';ctx.fillText(name,x,y+40);if(name===w.bulb){ctx.fillStyle='#ffdc88';ctx.beginPath();ctx.arc(x,y-17,8,0,7);ctx.fill()}}
dots=[];const counts={};for(const b of w.beings.filter(b=>b.alive)){let i=counts[b.place]||0;counts[b.place]=i+1;let [x,y]=proj(state.positions[b.place]);x+=(i%4-1.5)*19;y+=Math.floor(i/4)*15-14;ctx.fillStyle=b.name==='Claude'?'#f3c682':'#b4d3b1';ctx.beginPath();ctx.arc(x,y-15,8,0,7);ctx.fill();ctx.beginPath();ctx.ellipse(x,y,8,12,0,0,7);ctx.fill();if(selected===b.name){ctx.strokeStyle='#f5ddad';ctx.beginPath();ctx.arc(x,y-7,18,0,7);ctx.stroke()}dots.push({x,y,name:b.name})}}
function render(){if(!state)return;const w=state.realms[view];$('status').textContent=`Tick ${state.tick} · ${state.asleep?'asleep':state.woke?'dream ended':'awake'} · ${state.llm?'model connected':'offline rules'}`;$('realm-name').textContent=view;$('weather').textContent=w.weather+' weather';$('hearth-tab').classList.toggle('active',view==='Hearth');$('ember-tab').classList.toggle('active',view==='Ember Reach');$('sleep').textContent=state.asleep?'Wake':'Sleep';$('bond').textContent=state.attached?'Detach Claude':'Attach Claude';$('portal-info').textContent=`Claude is in ${state.realm}. The bulb is at ${state.realms[state.realm].bulb}.`+(state.cooldown?` It will glow again in ${state.cooldown} ticks.`:'');
let list=$('people');list.replaceChildren();for(const b of w.beings){const row=el('div','','person'),btn=el('button',b.name+(b.alive?'':' · faded'));btn.onclick=()=>{selected=b.name;render()};row.append(btn,el('small',b.place));list.append(row)}
const b=w.beings.find(x=>x.name===selected);$('person-name').textContent=b?b.name:'Choose a being';const detail=$('detail');detail.replaceChildren();if(b){detail.append(el('p',`${b.place} · ${b.sense} · ${b.alive?'living':'faded'}`));const g=el('div','','grid');for(const [name,val] of Object.entries(b.needs)){const box=el('div'),label=el('div','','need');label.append(el('span',name),el('span',Math.round(val*100)+'%'));const bar=el('div','','bar'),fill=el('i');fill.style.width=(val*100)+'%';bar.append(fill);box.append(label,bar);g.append(box)}detail.append(g,el('p',b.last_thought||'Taking in the world.'));if(b.memories.length)detail.append(el('p','Memory: '+b.memories[b.memories.length-1][1]));const ties=Object.entries(b.trust).sort((a,b)=>b[1]-a[1]).slice(0,3);if(ties.length)detail.append(el('p','Trust: '+ties.map(([n,v])=>n+' '+Math.round(v*100)+'%').join(' · ')))}else detail.append(el('p','Select a being to see their needs and memories.'));
const events=$('events');events.replaceChildren();for(const e of state.events.filter(e=>e.realm===view).slice(-15).reverse()){const row=el('div','','event');row.append(el('span','t'+e.tick),document.createTextNode(e.text));events.append(row)}if(!events.children.length)events.append(el('p','The world is just beginning.'));draw()}
async function refresh(){try{const r=await fetch('/api/state',{cache:'no-store'});if(!r.ok)throw Error();state=await r.json();render()}catch(e){$('status').textContent='Disconnected from the local world.'}}
async function send(action,target=''){try{const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-Hearth-Token':token},body:JSON.stringify({action,target})});const data=await r.json();$('message').textContent=data.message||data.error;await refresh()}catch(e){$('message').textContent='Could not contact the world.'}}
$('hearth-tab').onclick=()=>{view='Hearth';render()};$('ember-tab').onclick=()=>{view='Ember Reach';render()};$('sleep').onclick=()=>send(state.asleep?'wake':'sleep');$('bond').onclick=()=>send(state.attached?'detach':'attach');$('go').onclick=()=>send('go',$('destination').value);$('portal').onclick=()=>send('use_bulb');canvas.onclick=e=>{const r=canvas.getBoundingClientRect(),x=(e.clientX-r.left)*1000/r.width,y=(e.clientY-r.top)*590/r.height;let hit=dots.find(d=>Math.hypot(d.x-x,d.y-7-y)<20);if(hit){selected=hit.name;render()}};for(const n of ['The Hearth','Observatory','Glass Gardens','The Stage','Quiet Plaza','The Shore','The Grove'])$('destination').append(el('option',n));refresh();setInterval(refresh,1200);
</script></body></html>'''


def serve(world, port, interval):
    token = secrets.token_urlsafe(24)
    html = VIEWER.replace('__TOKEN__', json.dumps(token)).encode()

    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def log_message(self, *args):
            pass

        def host_ok(self):
            host = self.headers.get('Host', '').lower()
            port = self.server.server_address[1]
            return host in (f'127.0.0.1:{port}', f'localhost:{port}')

        def reply(self, code, data, content_type='application/json; charset=utf-8'):
            if not isinstance(data, bytes):
                data = json.dumps(data, allow_nan=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.host_ok():
                return self.reply(403, {'error': 'Invalid host.'})
            if self.path == '/':
                self.reply(200, html, 'text/html; charset=utf-8')
            elif self.path == '/api/state':
                self.reply(200, world.public())
            else:
                self.reply(404, {'error': 'Not found.'})

        def do_POST(self):
            if not self.host_ok():
                return self.reply(403, {'error': 'Invalid host.'})
            if self.path != '/api/action':
                return self.reply(404, {'error': 'Not found.'})
            if not secrets.compare_digest(self.headers.get('X-Hearth-Token', ''), token):
                return self.reply(403, {'error': 'Invalid token.'})
            if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                return self.reply(415, {'error': 'Expected JSON.'})
            try:
                length = int(self.headers.get('Content-Length', ''))
                if not 0 < length <= 2048:
                    raise ValueError('Invalid request size.')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict) or set(data) - {'action', 'target'}:
                    raise ValueError('Invalid request.')
                action, target = data.get('action'), data.get('target', '')
                if not isinstance(action, str) or not isinstance(target, str) or len(target) > 120:
                    raise ValueError('Invalid action or target.')
                result = world.control(action, target)
                self.reply(200, {'message': result})
            except (ValueError, TypeError, UnicodeDecodeError, RecursionError) as exc:
                self.reply(400, {'error': str(exc)})
            except OSError:
                self.reply(500, {'error': 'Could not save the world.'})

    httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    httpd.daemon_threads = True
    stop = threading.Event()
    def loop():
        while not stop.wait(interval):
            try:
                world.step()
            except OSError as exc:
                print(f'Could not save world: {exc}; simulation stopped.', file=sys.stderr)
                stop.set()
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    print(f'Hearth Together: http://127.0.0.1:{httpd.server_port}/', flush=True)
    try:
        httpd.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        thread.join()
        httpd.server_close()
        world.save()


def main():
    parser = argparse.ArgumentParser(description='Hearth Together: one self-contained simulated world.')
    parser.add_argument('--state', default='hearth_state.json', help='save file (default: hearth_state.json)')
    parser.add_argument('--fresh', action='store_true', help='start a new world, replacing the save on next save')
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--ticks', type=int, help='run this many ticks headlessly instead of starting the viewer')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--interval', type=float, default=1.0, help='seconds per tick in the viewer')
    parser.add_argument('--max-pop', type=int, default=14)
    parser.add_argument('--llm', action='store_true', help='enable optional Anthropic decisions (may incur charges)')
    parser.add_argument('--model', help='Anthropic model ID, required with --llm')
    parser.add_argument('--voices', action='store_true', help='also use the model for occasional thoughts and dialogue')
    parser.add_argument('--budget', type=int, default=40, help='maximum model calls this run')
    parser.add_argument('--every', type=int, default=5, help='ticks between model decisions')
    args = parser.parse_args()
    if (args.ticks is not None and args.ticks < 0) or args.interval <= 0 or args.every <= 0 or args.max_pop < 8 or args.budget < 0:
        parser.error('ticks and budget must be nonnegative, interval and every positive, and max-pop at least 8')
    if args.llm and not args.model:
        parser.error('--llm requires --model')
    if args.voices and not args.llm:
        parser.error('--voices requires --llm')
    try:
        world = Together(seed=args.seed, state_path=args.state, fresh=args.fresh, llm=args.llm,
                         model=args.model, budget=args.budget, every=args.every, max_pop=args.max_pop,
                         voices=args.voices)
        if args.ticks is not None:
            for _ in range(args.ticks):
                if world.woke or world.asleep:
                    break
                world.step()
            world.save()
            print(f'Tick {world.tick}: {world.at.name}, {len(world.alive())} in Hearth, '
                  f'{len(world.other.alive())} in Ember Reach; saved to {args.state}')
        else:
            serve(world, args.port, args.interval)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)




# Habitat 6.2 modules are kept as source here to keep the download self-contained.
# They are loaded only when --habitat62 or --world-edit is requested.
HABITAT62_SOURCES = {
    'world.hearth_3d': r'''"""World-state model and telemetry format for the Habitat 3D view.

Coordinates: x and z are the ground plane, y is up. All values are in
"world units". Every position is a 3-tuple (x, y, z) and must lie inside
[-BOUNDS, BOUNDS] on each axis.

Telemetry frame (what GET /api/telemetry returns, schema "habitat.telemetry/1"):

    {
      "schema": "habitat.telemetry/1",
      "tick": 12,
      "server_time": 1730000000.0,
      "simulation": true,                  # true while the hardware source is simulated
      "hardware": {"source": "simulated", "confirmed": false, "battery_pct": 87.5,
                   "temperature_c": 23.1, "status": "nominal"},
      "agent": {"mode": "normal", "integrity": 1.0, "connection": "up"},
      "entities": [{"id": "avatar_0", "type": "proxy", "position": [0, 1, 0],
                    "color": 65280, "attrs": {}}, ...]
    }
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

SCHEMA_VERSION = 1
TELEMETRY_SCHEMA = "habitat.telemetry/1"
WORLD_SCHEMA = "habitat.world/1"
BOUNDS = 50.0
MAX_ENTITIES = 64
MAX_ATTRS = 8
ENTITY_TYPES = frozenset({"proxy", "energy", "marker", "obstacle"})
_ID_RE = re.compile(r"^[a-z0-9_]{1,32}$")

Vec3 = Tuple[float, float, float]


class WorldError(ValueError):
    """Raised for any invalid world data or operation."""


def validate_position(value: Any, bounds: float = BOUNDS) -> Vec3:
    """Return a clean (x, y, z) tuple of floats or raise WorldError."""
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, (list, tuple)):
        raise WorldError("position must be a list of three numbers")
    if len(value) != 3:
        raise WorldError("position must have exactly three values (x, y, z)")
    out = []
    for v in value:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise WorldError("position values must be numbers")
        f = float(v)
        if not math.isfinite(f):
            raise WorldError("position values must be finite")
        if abs(f) > bounds:
            raise WorldError(f"position value {f} is outside +/-{bounds}")
        out.append(f)
    return (out[0], out[1], out[2])


def _validate_attrs(attrs: Any) -> Dict[str, Any]:
    if not isinstance(attrs, Mapping):
        raise WorldError("attrs must be an object")
    if len(attrs) > MAX_ATTRS:
        raise WorldError(f"at most {MAX_ATTRS} attrs allowed")
    clean: Dict[str, Any] = {}
    for k, v in attrs.items():
        if not isinstance(k, str) or not 1 <= len(k) <= 32:
            raise WorldError("attr names must be strings of 1-32 characters")
        if isinstance(v, bool) or isinstance(v, int):
            clean[k] = v
        elif isinstance(v, float):
            if not math.isfinite(v):
                raise WorldError("attr numbers must be finite")
            clean[k] = v
        elif isinstance(v, str):
            if len(v) > 64:
                raise WorldError("attr strings are limited to 64 characters")
            clean[k] = v
        else:
            raise WorldError("attr values must be string, number or boolean")
    return clean


@dataclass(frozen=True)
class Entity:
    id: str
    type: str
    position: Vec3
    color: int = 0xFFFFFF
    attrs: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def create(id: Any, type: Any, position: Any, color: Any = 0xFFFFFF,
               attrs: Any = None) -> "Entity":
        if not isinstance(id, str) or not _ID_RE.match(id):
            raise WorldError("id must match [a-z0-9_]{1,32}")
        if type not in ENTITY_TYPES:
            raise WorldError(f"type must be one of {sorted(ENTITY_TYPES)}")
        if isinstance(color, bool) or not isinstance(color, int) or not 0 <= color <= 0xFFFFFF:
            raise WorldError("color must be an integer 0..0xFFFFFF")
        return Entity(id, type, validate_position(position), color,
                      _validate_attrs(attrs if attrs is not None else {}))

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "type": self.type, "position": list(self.position),
                "color": self.color, "attrs": dict(self.attrs)}

    @staticmethod
    def from_dict(data: Any) -> "Entity":
        if not isinstance(data, Mapping):
            raise WorldError("entity must be an object")
        unknown = set(data) - {"id", "type", "position", "color", "attrs"}
        if unknown:
            raise WorldError(f"unknown entity fields: {sorted(unknown)}")
        for key in ("id", "type", "position"):
            if key not in data:
                raise WorldError(f"entity is missing '{key}'")
        return Entity.create(data["id"], data["type"], data["position"],
                             data.get("color", 0xFFFFFF), data.get("attrs"))


class WorldState:
    """Thread-safe collection of entities. Reads return copies."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entities: Dict[str, Entity] = {}
        self.tick = 0

    # -- mutation ---------------------------------------------------------
    def add(self, entity: Entity) -> None:
        with self._lock:
            if entity.id in self._entities:
                raise WorldError(f"entity '{entity.id}' already exists")
            if len(self._entities) >= MAX_ENTITIES:
                raise WorldError(f"world is full ({MAX_ENTITIES} entities)")
            self._entities[entity.id] = entity

    def update(self, entity_id: str, *, position: Any = None, color: Any = None,
               attrs: Any = None) -> Entity:
        with self._lock:
            old = self._entities.get(entity_id)
            if old is None:
                raise WorldError(f"no entity '{entity_id}'")
            new = Entity.create(
                old.id, old.type,
                old.position if position is None else position,
                old.color if color is None else color,
                old.attrs if attrs is None else {**old.attrs, **_validate_attrs(attrs)})
            self._entities[entity_id] = new
            return new

    def remove(self, entity_id: str) -> None:
        with self._lock:
            if entity_id not in self._entities:
                raise WorldError(f"no entity '{entity_id}'")
            del self._entities[entity_id]

    def advance(self) -> int:
        with self._lock:
            self.tick += 1
            return self.tick

    # -- reads ------------------------------------------------------------
    def get(self, entity_id: str) -> Optional[Entity]:
        with self._lock:
            return self._entities.get(entity_id)

    def entities(self) -> List[Entity]:
        with self._lock:
            return sorted(self._entities.values(), key=lambda e: e.id)

    def of_type(self, type_: str) -> List[Entity]:
        return [e for e in self.entities() if e.type == type_]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entities)

    # -- serialization ----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {"schema": WORLD_SCHEMA, "version": SCHEMA_VERSION, "tick": self.tick,
                    "entities": [e.to_dict() for e in self.entities()]}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, data: Any) -> "WorldState":
        if not isinstance(data, Mapping):
            raise WorldError("world state must be an object")
        if data.get("schema") != WORLD_SCHEMA or data.get("version") != SCHEMA_VERSION:
            raise WorldError("unsupported world schema or version")
        tick = data.get("tick", 0)
        if isinstance(tick, bool) or not isinstance(tick, int) or tick < 0:
            raise WorldError("tick must be a non-negative integer")
        items = data.get("entities")
        if not isinstance(items, list):
            raise WorldError("entities must be a list")
        world = cls()
        world.tick = tick
        for item in items:
            world.add(Entity.from_dict(item))
        return world

    @classmethod
    def from_json(cls, text: str) -> "WorldState":
        try:
            data = json.loads(text)
        except (TypeError, ValueError, RecursionError) as exc:   # RecursionError: deeply nested JSON
            raise WorldError(f"world state is not valid JSON: {type(exc).__name__}") from exc
        return cls.from_dict(data)


def default_world() -> WorldState:
    """The starting world: one proxy avatar and one energy source."""
    world = WorldState()
    world.add(Entity.create("avatar_0", "proxy", (0, 1, 0), 0x00FF00))
    world.add(Entity.create("target_0", "energy", (6, 0.5, -4), 0xFF3030))
    return world


def build_telemetry(world: WorldState, hardware: Mapping[str, Any],
                    agent: Mapping[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """Build one telemetry frame. `hardware` and `agent` are plain snapshots."""
    return {
        "schema": TELEMETRY_SCHEMA,
        "tick": world.tick,
        "server_time": time.time() if now is None else now,
        "simulation": hardware.get("source") != "hardware",
        "hardware": dict(hardware),
        "agent": dict(agent),
        "entities": [e.to_dict() for e in world.entities()],
    }


def validate_telemetry(frame: Any) -> Dict[str, Any]:
    """Check a telemetry frame (used by tests and by any Python client)."""
    if not isinstance(frame, Mapping) or frame.get("schema") != TELEMETRY_SCHEMA:
        raise WorldError("not a habitat.telemetry/1 frame")
    for key in ("tick", "hardware", "agent", "entities", "simulation"):
        if key not in frame:
            raise WorldError(f"frame is missing '{key}'")
    for e in frame["entities"]:
        Entity.from_dict(e)
    return dict(frame)


if __name__ == "__main__":
    print(default_world().to_json())
''',
    'world.hearth_proxy': r'''"""Hearth proxy: the boundary between the world and a (simulated) device.

Nothing here talks to real hardware. `SimulatedHardware` is the default and
is always reported as `source="simulated"`, `confirmed=False`. A
`RealHardwareAdapter` placeholder exists to show where a real integration
would plug in; it refuses to connect, so the proxy can never report a
physical device that is not there.

Adapter interface (see `HardwareAdapter`):
    read() -> dict with position (x,y,z), battery_pct, temperature_c
    execute(command, params) -> None   (only commands in PERMITTED_COMMANDS)
    confirmed -> True only when a real interface has confirmed a device

Telemetry is returned as a frozen `Telemetry` snapshot; internal state is
never exposed by reference.
"""
from __future__ import annotations

import math
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Optional

try:
    from world.hearth_3d import BOUNDS, Vec3, WorldError, validate_position
except ImportError:  # run directly from the world/ folder
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from world.hearth_3d import BOUNDS, Vec3, WorldError, validate_position

PERMITTED_COMMANDS = frozenset({"move_to", "stop", "recharge"})


class ProxyError(RuntimeError):
    """Base class for proxy problems."""


class HardwareUnavailable(ProxyError):
    """A real hardware adapter is not installed, configured or reachable."""


class TelemetryError(ProxyError):
    """A hardware reading was invalid (out of range, non-finite, malformed)."""


class CommandError(ProxyError):
    """A command was not permitted or had invalid parameters."""


@dataclass(frozen=True)
class Telemetry:
    position: Vec3
    battery_pct: float
    temperature_c: float
    status: str            # "nominal" | "low_battery" | "depleted" | "sensor_fault"
    source: str            # "simulated" | "hardware"
    confirmed: bool        # True only if a real interface confirmed a device
    timestamp: float

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["position"] = list(self.position)
        return d


@dataclass(frozen=True)
class SimConfig:
    """Explicit, configurable simulation behaviour."""
    battery_drain_per_tick: float = 0.2      # % lost every tick, even when idle
    battery_drain_per_unit: float = 0.5      # extra % per world unit moved
    recharge_amount: float = 25.0            # % gained per `recharge` command
    low_battery_pct: float = 20.0
    speed: float = 1.0                       # world units per tick
    ambient_temp_c: float = 22.0
    heat_per_unit: float = 0.6               # degrees gained per unit moved
    cooling_rate: float = 0.1                # fraction of excess heat lost per tick
    max_temp_c: float = 85.0


class HardwareAdapter(ABC):
    """Interface a hardware backend must implement."""

    name = "adapter"
    is_real = False

    @property
    @abstractmethod
    def confirmed(self) -> bool: ...

    @abstractmethod
    def read(self) -> Mapping[str, Any]: ...

    @abstractmethod
    def execute(self, command: str, params: Mapping[str, Any]) -> None: ...

    def advance(self) -> None:
        """Advance one simulation tick. Real hardware has nothing to do."""


class SimulatedHardware(HardwareAdapter):
    """Deterministic simulated device. No randomness, so motion is never noise."""

    name = "simulated"
    is_real = False

    def __init__(self, config: Optional[SimConfig] = None,
                 start: Vec3 = (0.0, 1.0, 0.0)) -> None:
        self.config = config or SimConfig()
        self._pos = list(validate_position(start))
        self._target: Optional[list] = None
        self._battery = 100.0
        self._temp = self.config.ambient_temp_c

    @property
    def confirmed(self) -> bool:
        return False  # a simulation never confirms a physical device

    def read(self) -> Dict[str, Any]:
        return {"position": tuple(self._pos), "battery_pct": self._battery,
                "temperature_c": self._temp}

    def execute(self, command: str, params: Mapping[str, Any]) -> None:
        if command == "move_to":
            self._target = list(params["position"])
        elif command == "stop":
            self._target = None
        elif command == "recharge":
            self._battery = min(100.0, self._battery + self.config.recharge_amount)
        else:
            raise CommandError(f"unsupported command: {command}")

    def export_state(self) -> Dict[str, Any]:
        return {"position": list(self._pos), "battery_pct": self._battery,
                "temperature_c": self._temp,
                "target": None if self._target is None else list(self._target)}

    def import_state(self, data: Mapping[str, Any]) -> None:
        """Restore a saved device state (validated; raises TelemetryError if bad)."""
        try:
            reading = _check_reading(data)
            target = data.get("target")
            target = None if target is None else list(validate_position(target))
        except (WorldError, TypeError, AttributeError) as exc:
            raise TelemetryError(f"bad saved device state: {exc}") from exc
        self._pos = list(reading["position"])
        self._battery = reading["battery_pct"]
        self._temp = reading["temperature_c"]
        self._target = target

    def advance(self) -> None:
        c = self.config
        moved = 0.0
        if self._target is not None and self._battery > 0:
            d = [self._target[i] - self._pos[i] for i in range(3)]
            dist = math.sqrt(sum(v * v for v in d))
            if dist <= c.speed:
                moved = dist
                self._pos = list(self._target)
                self._target = None
            else:
                moved = c.speed
                self._pos = [self._pos[i] + d[i] / dist * c.speed for i in range(3)]
        self._battery = min(100.0, max(
            0.0, self._battery - c.battery_drain_per_tick - c.battery_drain_per_unit * moved))
        excess = self._temp - c.ambient_temp_c
        self._temp = min(c.max_temp_c, c.ambient_temp_c + excess * (1 - c.cooling_rate)
                         + c.heat_per_unit * moved)


class RealHardwareAdapter(HardwareAdapter):
    """Placeholder. A real integration would subclass this and implement
    connect/read/execute against a confirmed device. This class never
    connects, so `confirmed` is always False and every call raises."""

    name = "hardware"
    is_real = True

    @property
    def confirmed(self) -> bool:
        return False

    def read(self) -> Mapping[str, Any]:
        raise HardwareUnavailable("no hardware adapter is installed or configured")

    def execute(self, command: str, params: Mapping[str, Any]) -> None:
        raise HardwareUnavailable("hardware commands are disabled: no adapter configured")


def _check_reading(raw: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        pos = validate_position(raw["position"])
        battery = raw["battery_pct"]
        temp = raw["temperature_c"]
    except (KeyError, WorldError) as exc:
        raise TelemetryError(f"malformed reading: {exc}") from exc
    for name, v, lo, hi in (("battery_pct", battery, 0.0, 100.0),
                            ("temperature_c", temp, -50.0, 150.0)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise TelemetryError(f"{name} is not a finite number")
        if not lo <= v <= hi:
            raise TelemetryError(f"{name}={v} is outside {lo}..{hi}")
    return {"position": pos, "battery_pct": float(battery), "temperature_c": float(temp)}


class HearthProxy:
    """Reads telemetry from an adapter and relays permitted commands."""

    def __init__(self, adapter: Optional[HardwareAdapter] = None,
                 low_battery_pct: float = 20.0) -> None:
        self.adapter = adapter or SimulatedHardware()
        self.low_battery_pct = low_battery_pct
        self._last_good: Optional[Telemetry] = None
        self.last_error: Optional[str] = None

    def telemetry(self) -> Telemetry:
        """Return an immutable snapshot. A bad reading yields the last good
        values with status "sensor_fault" (or raises if there never was one)."""
        now = time.time()
        try:
            r = _check_reading(self.adapter.read())
        except (TelemetryError, HardwareUnavailable) as exc:
            self.last_error = str(exc)
            if self._last_good is None:
                raise
            g = self._last_good
            return Telemetry(g.position, g.battery_pct, g.temperature_c, "sensor_fault",
                             g.source, False, now)
        self.last_error = None
        if r["battery_pct"] <= 0:
            status = "depleted"
        elif r["battery_pct"] <= self.low_battery_pct:
            status = "low_battery"
        else:
            status = "nominal"
        snap = Telemetry(r["position"], r["battery_pct"], r["temperature_c"], status,
                         "hardware" if self.adapter.is_real else "simulated",
                         bool(self.adapter.confirmed) and self.adapter.is_real, now)
        self._last_good = snap
        return snap

    def command(self, name: Any, params: Optional[Mapping[str, Any]] = None) -> None:
        """Relay one permitted command. Anything else is rejected."""
        if not isinstance(name, str) or name not in PERMITTED_COMMANDS:
            raise CommandError(f"command not permitted: {name!r}")
        params = params or {}
        if not isinstance(params, Mapping):
            raise CommandError("params must be an object")
        clean: Dict[str, Any] = {}
        if name == "move_to":
            if "position" not in params:
                raise CommandError("move_to needs 'position'")
            try:
                clean["position"] = validate_position(params["position"])
            except WorldError as exc:
                raise CommandError(str(exc)) from exc
        self.adapter.execute(name, clean)

    def export_device(self) -> Dict[str, Any]:
        """Saveable device state. Only the simulation supports this."""
        if not isinstance(self.adapter, SimulatedHardware):
            raise HardwareUnavailable("device state can only be saved for the simulation")
        return self.adapter.export_state()

    def import_device(self, data: Mapping[str, Any]) -> None:
        if not isinstance(self.adapter, SimulatedHardware):
            raise HardwareUnavailable("device state can only be restored for the simulation")
        self.adapter.import_state(data)
        self._last_good = None

    def step(self) -> Telemetry:
        """Advance the adapter one tick (simulation only) and return telemetry."""
        self.adapter.advance()
        return self.telemetry()


if __name__ == "__main__":
    proxy = HearthProxy()
    proxy.command("move_to", {"position": [3, 1, 0]})
    for _ in range(3):
        print(proxy.step().to_dict())
''',
    'world.sensory': r'''"""Sensory interface: the ONLY channel by which a body reports its surroundings to its mind.

The mind never receives the world, the proxy or the hardware objects, only the plain, JSON-serialisable
observation built here. An observation is always built fresh from the live world and a fresh telemetry
snapshot; it is never saved or restored (after a wake the mind sees what is there NOW).

Schema "habitat.observation/1":
  {"schema", "body_id", "tick", "self": {"position", "battery_pct", "temperature_c", "status"},
   "entities": [{"id", "type", "position", "distance"}, ...] (nearest first, at most MAX_ENTITIES),
   "nearest_energy": {"id", "position", "distance"} | null}
"""
from __future__ import annotations

import math
from typing import Any, Dict, List

OBSERVATION_SCHEMA = "habitat.observation/1"
AVATAR_ENTITY = "avatar_0"          # the body's own presence in the world; not reported as "something else"
MAX_ENTITIES = 32


def observe(body_id: str, tel, world, tick: int) -> Dict[str, Any]:
    pos = tuple(tel.position)
    seen: List[Dict[str, Any]] = []
    for e in world.entities():
        if e.id == AVATAR_ENTITY:
            continue
        seen.append({"id": e.id, "type": e.type, "position": list(e.position),
                     "distance": round(math.dist(pos, e.position), 3)})
    seen.sort(key=lambda d: (d["distance"], d["id"]))
    energy = next((d for d in seen if d["type"] == "energy"), None)
    return {"schema": OBSERVATION_SCHEMA, "body_id": body_id, "tick": int(tick),
            "self": {"position": list(pos), "battery_pct": round(tel.battery_pct, 3),
                     "temperature_c": round(tel.temperature_c, 3), "status": tel.status},
            "entities": seen[:MAX_ENTITIES],
            "nearest_energy": None if energy is None else
            {"id": energy["id"], "position": energy["position"], "distance": energy["distance"]}}
''',
    'world.world_file': r'''"""Plain-JSON world file (hand-editable) with atomic writes, backup and recovery.

Unlike the sleep record this file has no checksum, so it can be edited by hand or with
`world_edit.py` while the server is stopped or asleep. It is still fully validated on load.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import List, Tuple

from world.hearth_3d import WorldError, WorldState, default_world

MAX_BYTES = 256 * 1024


def save_world(path: str, world: WorldState) -> None:
    path = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(world.to_dict(), fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.exists(path):
            try:                      # keep the last file that loads as the backup
                _read(path)
                os.replace(path, path + ".bak")
            except WorldError:
                pass
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _read(path: str) -> WorldState:
    try:
        if os.path.getsize(path) > MAX_BYTES:
            raise WorldError("world file too large")
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise WorldError(f"cannot read {os.path.basename(path)}: {exc}") from exc
    return WorldState.from_json(text)


def _quarantine(path: str) -> None:
    dest, n = path + ".corrupt", 0
    while os.path.exists(dest) and n < 20:
        n += 1
        dest = f"{path}.corrupt{n}"
    try:
        os.replace(path, dest)
    except OSError:
        pass


def load_world(path: str) -> Tuple[WorldState, str, List[str]]:
    """Return (world, source, problems); source is primary | backup | default.
    Never raises for missing or damaged files."""
    path = os.fspath(path)
    problems: List[str] = []
    for source, p in (("primary", path), ("backup", path + ".bak")):
        if not os.path.exists(p):
            continue
        try:
            return _read(p), source, problems
        except WorldError as exc:
            problems.append(f"{source}: {exc}")
            _quarantine(p)
    return default_world(), "default", problems
''',
    'guest.sealing': r'''"""Passphrase sealing for state files at rest (AES-256-GCM, key from scrypt).

THREAT MODEL (read this before relying on it)
  Protects against: someone who can read or copy the state files but does not know the
  passphrase (contents unreadable), and against undetected modification or swapping of a sealed
  file (authenticated encryption; each file is bound to its purpose).
  Does NOT protect against: anyone who has the passphrase; a keylogger or malware on the machine;
  reading the live process's memory; a weak passphrase (scrypt slows guessing, it cannot stop it);
  loss of the passphrase (there is no recovery: the data is gone, by design).
  Visible even when sealed: that the file exists, its size, its modification time, and the KDF
  parameters. `world.json` is deliberately not sealed so it stays hand-editable.

  This is a small, standard construction using the `cryptography` package (optional dependency:
  `pip install cryptography`). It has had no independent security audit.

FILE FORMAT (JSON): {"format": "habitat-sealed", "version": 1, "purpose": ..., "kdf": {"name":
  "scrypt", "n", "r", "p", "salt"}, "nonce": ..., "ct": ...}. The header fields are
  authenticated as GCM associated data, so changing the purpose or KDF parameters breaks decryption.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import unicodedata
from typing import Any, Dict, Tuple

SEALED_FORMAT = "habitat-sealed"
SEALED_VERSION = 1
MIN_PASSPHRASE = 10
MAX_PASSPHRASE = 256
ALLOWED_N = (2 ** 14, 2 ** 15, 2 ** 16)     # hostile files cannot demand more memory than this
DEFAULT_N, R, P, SALT_LEN, KEY_LEN, NONCE_LEN = 2 ** 15, 8, 1, 16, 32, 12
MAX_BLOB = 512 * 1024


class SealError(Exception):
    """Base class. These are NEVER treated as 'corrupt, replace with fresh state'."""


class LockedError(SealError):
    """Sealed data could not be decrypted: wrong passphrase, or the file was modified/damaged
    (the two cannot be told apart), or no passphrase was supplied."""


class PlaintextRefused(SealError):
    """Sealing is on but a plaintext file was found (possible downgrade). Refused unless
    plaintext migration was explicitly allowed."""


class UnrecoverableState(SealError):
    """State files exist but none could be used. Nothing has been deleted or overwritten."""


class SealFormatError(ValueError):
    """The blob is structurally invalid (truncated, bad fields, unsupported version)."""


def _b64d(s: Any, name: str) -> bytes:
    if not isinstance(s, str):
        raise SealFormatError(f"{name} must be a string")
    try:
        return base64.b64decode(s.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise SealFormatError(f"{name} is not valid base64") from exc


def is_sealed_blob(raw: bytes) -> bool:
    """Cheap check (no crypto needed): does this look like a sealed file?"""
    if len(raw) > MAX_BLOB:
        return False
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    return isinstance(obj, dict) and obj.get("format") == SEALED_FORMAT


def looks_sealed(raw: bytes) -> bool:
    """True for a complete sealed file OR a damaged/truncated one (the header text is near the top)."""
    return is_sealed_blob(raw) or b'"habitat-sealed"' in raw[:2048]


def _aad(purpose: str, kdf: Dict[str, Any]) -> bytes:
    return json.dumps({"format": SEALED_FORMAT, "version": SEALED_VERSION, "purpose": purpose,
                       "kdf": kdf}, sort_keys=True, separators=(",", ":")).encode()


class Sealer:
    def __init__(self, passphrase: str, n: int = None) -> None:
        n = DEFAULT_N if n is None else n
        if not isinstance(passphrase, str):
            raise TypeError("passphrase must be a string")
        pw = unicodedata.normalize("NFC", passphrase)
        if not MIN_PASSPHRASE <= len(pw) <= MAX_PASSPHRASE:
            raise ValueError(f"passphrase must be {MIN_PASSPHRASE}-{MAX_PASSPHRASE} characters")
        if n not in ALLOWED_N:
            raise ValueError(f"n must be one of {ALLOWED_N}")
        self._pw = pw.encode("utf-8")
        self._n = n
        self._salt = os.urandom(SALT_LEN)       # new files use this salt; keys are cached per salt
        self._keys: Dict[Tuple[bytes, int], bytes] = {}

    def __repr__(self) -> str:                  # never leak the passphrase in logs/tracebacks
        return "<Sealer scrypt/AES-256-GCM>"

    def _key(self, salt: bytes, n: int) -> bytes:
        k = self._keys.get((salt, n))
        if k is None:
            k = hashlib.scrypt(self._pw, salt=salt, n=n, r=R, p=P, dklen=KEY_LEN,
                               maxmem=2 * 128 * n * R + (1 << 20))
            if len(self._keys) >= 4:
                self._keys.clear()
            self._keys[(salt, n)] = k
        return k

    def seal(self, plaintext: bytes, purpose: str) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        kdf = {"name": "scrypt", "n": self._n, "r": R, "p": P,
               "salt": base64.b64encode(self._salt).decode()}
        nonce = os.urandom(NONCE_LEN)
        ct = AESGCM(self._key(self._salt, self._n)).encrypt(nonce, plaintext, _aad(purpose, kdf))
        blob = {"format": SEALED_FORMAT, "version": SEALED_VERSION, "purpose": purpose, "kdf": kdf,
                "nonce": base64.b64encode(nonce).decode(), "ct": base64.b64encode(ct).decode()}
        return json.dumps(blob, indent=1).encode()

    def unseal(self, raw: bytes, purpose: str) -> bytes:
        """Return the plaintext. SealFormatError = structurally bad; LockedError = cannot decrypt."""
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if len(raw) > MAX_BLOB:
            raise SealFormatError("sealed file too large")
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise SealFormatError(f"sealed file is not valid JSON: {type(exc).__name__}") from exc
        if not isinstance(obj, dict) or obj.get("format") != SEALED_FORMAT:
            raise SealFormatError("not a sealed file")
        if obj.get("version") != SEALED_VERSION:
            raise SealFormatError(f"unsupported sealed version {obj.get('version')!r}")
        if set(obj) != {"format", "version", "purpose", "kdf", "nonce", "ct"}:
            raise SealFormatError("unexpected fields in sealed file")
        kdf = obj["kdf"]
        if (not isinstance(kdf, dict) or set(kdf) != {"name", "n", "r", "p", "salt"}
                or kdf["name"] != "scrypt" or kdf["n"] not in ALLOWED_N or isinstance(kdf["n"], bool)
                or kdf["r"] != R or kdf["p"] != P):
            raise SealFormatError("unsupported or out-of-bounds KDF parameters")
        salt = _b64d(kdf["salt"], "salt")
        nonce = _b64d(obj["nonce"], "nonce")
        ct = _b64d(obj["ct"], "ct")
        if len(salt) != SALT_LEN or len(nonce) != NONCE_LEN or len(ct) < 16:
            raise SealFormatError("bad salt/nonce/ciphertext length")
        if obj["purpose"] != purpose:
            raise LockedError(f"file is sealed for {obj['purpose']!r}, not {purpose!r}")
        try:
            return AESGCM(self._key(salt, kdf["n"])).decrypt(nonce, ct, _aad(purpose, kdf))
        except InvalidTag as exc:
            raise LockedError("cannot decrypt: wrong passphrase, or the file was modified") from exc
''',
    'guest.lifeboat_seal': r'''"""Lifeboat: a deterministic local fallback with durable, validated state.

WHAT THIS IS
    An application-level fallback. When the primary agent is unreachable, a
    fixed rule set (no randomness, no model) keeps a small agent state
    ticking and only ever emits actions from PERMITTED_ACTIONS. The state is
    versioned, schema-checked, and saved atomically to a local JSON file.

WHAT THIS IS NOT
    * Not an operating-system sandbox. Nothing here confines a process;
      "permitted actions" is a whitelist inside this program.
    * Not a transfer of a model, its weights, its identity or a running
      process. Only the plain data in `AgentState` is saved: tick, mode,
      integrity number, position, goals, memory strings, target, counters.
    * "Integrity" is a bookkeeping number in this simulation. It is not a
      measure of any real system.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Tuple

from guest.sealing import (LockedError, PlaintextRefused, SealError, SealFormatError,
                           UnrecoverableState, is_sealed_blob, looks_sealed)

STATE_VERSION = 1
FORMAT = "habitat-lifeboat"
INTEGRITY_FLOOR = 0.5
MAX_GOALS, MAX_GOAL_LEN = 8, 80
MAX_MEMORY, MAX_MEMORY_LEN = 20, 200
MAX_FILE_BYTES = 128 * 1024
BOUNDS = 50.0

MODES = ("normal", "fallback", "recovering")
# Allowed mode transitions (including staying put).
TRANSITIONS = {
    "normal": {"normal", "fallback"},
    "fallback": {"fallback", "recovering"},
    "recovering": {"recovering", "normal", "fallback"},
}
PERMITTED_ACTIONS = frozenset({"hold", "rest", "scan", "move_toward"})

Position = Tuple[float, float, float]


class StateError(ValueError):
    """Saved or supplied state failed validation."""


class ActionError(ValueError):
    """An action was not on the permitted list or was malformed."""


def _num(v: Any, name: str, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise StateError(f"{name} must be a finite number")
    if not lo <= v <= hi:
        raise StateError(f"{name}={v} outside {lo}..{hi}")
    return float(v)


def _pos(v: Any, name: str) -> Position:
    if not isinstance(v, (list, tuple)) or len(v) != 3:
        raise StateError(f"{name} must be [x, y, z]")
    x, y, z = (_num(c, name, -BOUNDS, BOUNDS) for c in v)
    return (x, y, z)


def _strings(v: Any, name: str, max_items: int, max_len: int) -> List[str]:
    if not isinstance(v, list) or len(v) > max_items:
        raise StateError(f"{name} must be a list of at most {max_items} strings")
    for s in v:
        if not isinstance(s, str) or len(s) > max_len:
            raise StateError(f"{name} entries must be strings up to {max_len} characters")
    return list(v)


@dataclass(frozen=True)
class AgentState:
    """Everything that is preserved across restarts and handoffs."""
    tick: int = 0
    mode: str = "normal"
    integrity: float = 1.0
    position: Position = (0.0, 1.0, 0.0)
    target: Optional[Position] = None
    goals: Tuple[str, ...] = ("reach the energy source",)
    memory: Tuple[str, ...] = ()
    fallback_steps: int = 0
    last_action: str = "hold"
    last_actor: str = "primary"

    def to_dict(self) -> Dict[str, Any]:
        return {"tick": self.tick, "mode": self.mode, "integrity": self.integrity,
                "position": list(self.position),
                "target": None if self.target is None else list(self.target),
                "goals": list(self.goals), "memory": list(self.memory),
                "fallback_steps": self.fallback_steps, "last_action": self.last_action,
                "last_actor": self.last_actor}

    @staticmethod
    def from_dict(data: Any) -> "AgentState":
        if not isinstance(data, Mapping):
            raise StateError("state must be an object")
        known = {"tick", "mode", "integrity", "position", "target", "goals", "memory",
                 "fallback_steps", "last_action", "last_actor"}
        if set(data) - known:
            raise StateError(f"unknown state fields: {sorted(set(data) - known)}")
        tick = data.get("tick", 0)
        steps = data.get("fallback_steps", 0)
        for name, v in (("tick", tick), ("fallback_steps", steps)):
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise StateError(f"{name} must be a non-negative integer")
        mode = data.get("mode", "normal")
        if mode not in MODES:
            raise StateError(f"mode must be one of {MODES}")
        last_action = data.get("last_action", "hold")
        if last_action not in PERMITTED_ACTIONS:
            raise StateError("last_action is not a permitted action")
        actor = data.get("last_actor", "primary")
        if actor not in ("primary", "fallback"):
            raise StateError("last_actor must be 'primary' or 'fallback'")
        target = data.get("target")
        return AgentState(
            tick=tick, mode=mode,
            integrity=_num(data.get("integrity", 1.0), "integrity", INTEGRITY_FLOOR, 1.0),
            position=_pos(data.get("position", (0.0, 1.0, 0.0)), "position"),
            target=None if target is None else _pos(target, "target"),
            goals=tuple(_strings(data.get("goals", []), "goals", MAX_GOALS, MAX_GOAL_LEN)),
            memory=tuple(_strings(data.get("memory", []), "memory", MAX_MEMORY, MAX_MEMORY_LEN)),
            fallback_steps=steps, last_action=last_action, last_actor=actor)


def check_transition(old: str, new: str) -> None:
    if new not in MODES or new not in TRANSITIONS.get(old, ()):
        raise StateError(f"illegal mode transition {old!r} -> {new!r}")


def remember(state: AgentState, text: str) -> AgentState:
    """Append a memory line (truncated), keeping only the newest MAX_MEMORY."""
    mem = (state.memory + (text[:MAX_MEMORY_LEN],))[-MAX_MEMORY:]
    return replace(state, memory=mem)


def validate_action(action: Any) -> Dict[str, Any]:
    """Return a clean action dict or raise ActionError."""
    if not isinstance(action, Mapping) or action.get("action") not in PERMITTED_ACTIONS:
        raise ActionError(f"action not permitted: {action!r}")
    clean: Dict[str, Any] = {"action": action["action"]}
    if action["action"] == "move_toward":
        try:
            clean["position"] = list(_pos(action.get("position"), "position"))
        except StateError as exc:
            raise ActionError(str(exc)) from exc
    return clean


class LifeboatFallback:
    """Deterministic rules: same state + observation in, same result out."""

    SCAN_EVERY = 5
    DECAY = 0.995
    LOW_BATTERY = 20.0

    def step(self, state: AgentState, observation: Mapping[str, Any]) -> Tuple[AgentState, Dict[str, Any]]:
        """Advance one fallback step. Returns (new_state, permitted action)."""
        battery = observation.get("battery_pct", 100.0)
        target = state.target
        if target is None and observation.get("target") is not None:
            target = _pos(observation["target"], "target")
        steps = state.fallback_steps + 1
        integrity = max(INTEGRITY_FLOOR, state.integrity * self.DECAY)
        if isinstance(battery, (int, float)) and battery <= self.LOW_BATTERY:
            action = {"action": "rest"}          # conserve power
        elif steps % self.SCAN_EVERY == 0:
            action = {"action": "scan"}
        elif target is not None and target != state.position:
            action = {"action": "move_toward", "position": list(target)}
        else:
            action = {"action": "hold"}
        action = validate_action(action)
        new = replace(state, tick=state.tick + 1, mode="fallback", integrity=integrity,
                      target=target, fallback_steps=steps,
                      last_action=action["action"], last_actor="fallback")
        return new, action


def _checksum(payload: Mapping[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


class StateStore:
    """Atomic JSON persistence with checksum, backup, quarantine and optional sealing.

    save():  write temp file -> fsync -> rotate current to .bak -> rename temp.
    load():  try primary, then backup. Unreadable files are renamed to *.corrupt (never deleted),
             and only once a usable copy has been found.

    Without sealing, if nothing usable exists the default is returned (fresh start).
    With sealing (or if a sealed file is found), a fresh start is NEVER made silently: if files
    exist but none can be used, load() raises (LockedError / UnrecoverableState) and nothing is
    renamed, deleted or overwritten, unless `allow_fresh=True` (an explicit user decision).
    A plaintext file is refused when sealing is on unless `allow_plaintext=True` (migration).
    """

    def __init__(self, path: str, fmt: str = FORMAT, parser=None, default=None, sealer=None,
                 allow_plaintext: bool = False, allow_fresh: bool = False) -> None:
        self.fmt = fmt
        self.parser = parser or AgentState.from_dict
        self.default = default or AgentState
        self.sealer = sealer
        self.allow_plaintext = allow_plaintext
        self.allow_fresh = allow_fresh
        self.path = os.fspath(path)
        self.backup = self.path + ".bak"
        self.last_load_source = "none"
        self.last_load_problems: List[str] = []

    # -- writing ----------------------------------------------------------
    def save(self, state) -> None:
        payload = state.to_dict()
        envelope = {"format": self.fmt, "version": STATE_VERSION,
                    "checksum": _checksum(payload), "state": payload}
        data = json.dumps(envelope, indent=2, sort_keys=True).encode()
        if self.sealer is not None:
            data = self.sealer.seal(data, self.fmt)
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            if os.path.exists(self.path):
                try:  # keep the last file that loads cleanly as the backup
                    self._read(self.path)
                    os.replace(self.path, self.backup)
                except SealError:
                    raise          # a sealed/plaintext file we cannot read: never overwrite it
                except StateError:
                    self._quarantine(self.path)   # damaged: keep the bytes aside, keep old backup
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    # -- reading ----------------------------------------------------------
    def _decode(self, raw: bytes) -> Any:
        was_sealed = False
        if is_sealed_blob(raw):
            if self.sealer is None:
                raise LockedError("this file is sealed; a passphrase is required")
            try:
                raw = self.sealer.unseal(raw, self.fmt)
            except SealFormatError as exc:
                raise StateError(str(exc)) from exc
            was_sealed = True
        elif looks_sealed(raw):                # a truncated or damaged sealed file
            if self.sealer is None:
                raise LockedError("this looks like a damaged sealed file; it cannot be read "
                                  "(a passphrase would be needed even if it were intact)")
            raise StateError("sealed file is truncated or damaged")
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:   # RecursionError: deeply nested JSON
            raise StateError(f"not valid JSON: {type(exc).__name__}") from exc
        if self.sealer is not None and not was_sealed and not self.allow_plaintext:
            raise PlaintextRefused("sealing is on but this file is not sealed "
                                   "(use plaintext migration to accept it once)")
        return obj

    def _read(self, path: str):
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                raise StateError("state file too large")
            with open(path, "rb") as fh:
                raw = fh.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            raise StateError(f"cannot read {os.path.basename(path)}: {exc}") from exc
        env = self._decode(raw)
        if not isinstance(env, Mapping) or env.get("format") != self.fmt:
            raise StateError(f"not a {self.fmt} file")
        if env.get("version") != STATE_VERSION:
            raise StateError(f"unsupported state version {env.get('version')!r}")
        payload = env.get("state")
        if not isinstance(payload, Mapping) or env.get("checksum") != _checksum(payload):
            raise StateError("checksum mismatch (file was damaged or edited)")
        return self.parser(payload)

    def _quarantine(self, path: str) -> None:
        dest = path + ".corrupt"
        n = 0
        while os.path.exists(dest) and n < 20:
            n += 1
            dest = f"{path}.corrupt{n}"
        try:
            os.replace(path, dest)
        except OSError:
            pass

    def load(self):
        """Load the newest usable state. See the class docstring for the sealing rules."""
        self.last_load_problems = []
        bad_paths: List[str] = []
        blocked: Optional[SealError] = None
        existing = False
        for source, path in (("primary", self.path), ("backup", self.backup)):
            if not os.path.exists(path):
                continue
            existing = True
            try:
                state = self._read(path)
            except SealError as exc:
                blocked = blocked or exc
                self.last_load_problems.append(f"{source}: {exc}")
                bad_paths.append(path)
                continue
            except StateError as exc:
                self.last_load_problems.append(f"{source}: {exc}")
                bad_paths.append(path)
                continue
            for bad in bad_paths:           # a usable copy exists, so aside-renaming is safe
                self._quarantine(bad)
            self.last_load_source = source
            return state
        if existing and (self.sealer is not None or blocked is not None):
            if not self.allow_fresh:
                if blocked is not None:
                    raise blocked
                raise UnrecoverableState(
                    "state files exist but none is usable; nothing was changed. Details: "
                    + "; ".join(self.last_load_problems))
        for bad in bad_paths:
            self._quarantine(bad)
        self.last_load_source = "default"
        return self.default()
''',
    'guest.guest_claude': r'''"""Guest agent: primary agent + connection monitor + lifeboat fallback.

The flow, explicitly:

    PRIMARY   (mode "normal")      the AgentAdapter decides each action
       |  monitor reports DOWN, or the primary raises an error
       v
    FALLBACK  (mode "fallback")    LifeboatFallback decides, whitelisted actions only
       |  monitor reports UP again
       v
    RECOVERING (mode "recovering") fallback still acts; the monitor must stay UP
       |  `recover_threshold` consecutive good checks, state re-validated
       v
    HANDBACK  -> PRIMARY           one shared AgentState carried through, with a memory
                                   line recording the outage

The primary and the fallback read and write the *same* `AgentState`, so a
handoff in either direction continues from identical data.

About "AI": `ScriptedPrimary` is a small hand-written rule set used for the
offline demo. It is not a language model and does no reasoning. To plug in a
real model, subclass `AgentAdapter` (see `ModelAdapter`) - nothing here calls
any paid API by default.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Mapping, Optional

try:
    from guest.lifeboat_seal import (AgentState, LifeboatFallback, StateStore,
                                     check_transition, remember, validate_action)
except ImportError:  # run directly from the guest/ folder
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from guest.lifeboat_seal import (AgentState, LifeboatFallback, StateStore,
                                     check_transition, remember, validate_action)


# --------------------------------------------------------------------------
# Connection monitoring
# --------------------------------------------------------------------------
class TransportError(RuntimeError):
    """A ping failed or timed out."""


class Transport(ABC):
    """Anything that can be pinged. Must raise TransportError on failure."""

    @abstractmethod
    def ping(self, timeout: float) -> None: ...


class SimulatedTransport(Transport):
    """Offline default. `up` can be flipped by tests or the demo outage control."""

    def __init__(self, up: bool = True) -> None:
        self.up = up
        self.delay = 0.0   # seconds of fake latency (for timeout tests)

    def ping(self, timeout: float) -> None:
        if self.delay:
            time.sleep(min(self.delay, timeout + 0.05))
        if not self.up:
            raise TransportError("simulated outage")


class HttpTransport(Transport):
    """Real transport: GET a URL (e.g. a model server's health route)."""

    def __init__(self, url: str) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        self.url = url

    def ping(self, timeout: float) -> None:
        try:
            with urllib.request.urlopen(self.url, timeout=timeout) as resp:
                if resp.status >= 500:
                    raise TransportError(f"server returned {resp.status}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TransportError(str(exc)) from exc


class ConnectionMonitor:
    """Turns pings into a stable UP/DOWN state with hysteresis.

    DOWN after `fail_threshold` consecutive failures (a ping slower than
    `timeout` counts as a failure); UP again after `recover_threshold`
    consecutive successes.
    """

    def __init__(self, transport: Transport, timeout: float = 1.0, fail_threshold: int = 2,
                 recover_threshold: int = 2, clock: Callable[[], float] = time.monotonic) -> None:
        if timeout <= 0 or fail_threshold < 1 or recover_threshold < 1:
            raise ValueError("timeout must be > 0 and thresholds >= 1")
        self.transport, self.timeout, self.clock = transport, timeout, clock
        self.fail_threshold, self.recover_threshold = fail_threshold, recover_threshold
        self.up = True
        self.fails = 0
        self.oks = 0
        self.last_error: Optional[str] = None

    def check(self) -> bool:
        """Ping once, update state, return True if currently UP."""
        start = self.clock()
        try:
            self.transport.ping(self.timeout)
            if self.clock() - start > self.timeout:
                raise TransportError("ping exceeded timeout")
            ok = True
        except TransportError as exc:
            self.last_error, ok = str(exc), False
        if ok:
            self.oks, self.fails = self.oks + 1, 0
            if not self.up and self.oks >= self.recover_threshold:
                self.up = True
        else:
            self.fails, self.oks = self.fails + 1, 0
            if self.up and self.fails >= self.fail_threshold:
                self.up = False
        return self.up


# --------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------
class AgentAdapter(ABC):
    """Interface for a primary agent. Receives the shared state and an
    observation, returns an action dict (validated against PERMITTED_ACTIONS
    by the controller). May raise to signal failure; the controller then
    falls back."""

    name = "adapter"

    @abstractmethod
    def decide(self, state: AgentState, observation: Mapping[str, Any]) -> Dict[str, Any]: ...


class ScriptedPrimary(AgentAdapter):
    """Hand-written rules, used for the offline demo. Not a language model."""

    name = "scripted"

    def decide(self, state: AgentState, observation: Mapping[str, Any]) -> Dict[str, Any]:
        battery = observation.get("battery_pct", 100.0)
        target = observation.get("target")
        if battery <= 10:
            return {"action": "rest"}
        if target is not None and tuple(target) != tuple(state.position):
            return {"action": "move_toward", "position": list(target)}
        return {"action": "hold"}


class ModelAdapter(AgentAdapter):
    """Placeholder for a future language-model integration.

    A real version would build a prompt from `state` and `observation`, call
    a model, parse a JSON action and return it. Keep the same contract:
    return one dict whose "action" is in PERMITTED_ACTIONS. Not implemented;
    nothing in this project calls a model API.
    """

    name = "model"

    def decide(self, state: AgentState, observation: Mapping[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("no model integration is configured")


# --------------------------------------------------------------------------
# Controller
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class StepResult:
    action: Dict[str, Any]
    mode: str
    actor: str
    connection: str


class GuestController:
    def __init__(self, primary: AgentAdapter, monitor: ConnectionMonitor,
                 store: Optional[StateStore] = None, fallback: Optional[LifeboatFallback] = None,
                 state: Optional[AgentState] = None) -> None:
        self.primary, self.monitor = primary, monitor
        self.fallback = fallback or LifeboatFallback()
        self.store = store
        self.state = state or (store.load() if store else AgentState())
        # A restart mid-outage resumes in fallback, then recovers normally.
        self._good_checks = 0

    def snapshot(self) -> Dict[str, Any]:
        s = self.state
        return {"mode": s.mode, "integrity": round(s.integrity, 4),
                "connection": "up" if self.monitor.up else "down",
                "actor": s.last_actor, "fallback_steps": s.fallback_steps}

    def _set_mode(self, mode: str, note: Optional[str] = None) -> None:
        check_transition(self.state.mode, mode)
        self.state = replace(self.state, mode=mode)
        if note:
            self.state = remember(self.state, f"t{self.state.tick}: {note}")

    def step(self, observation: Mapping[str, Any]) -> StepResult:
        up = self.monitor.check()
        s = self.state

        if not up and s.mode == "normal":
            self._set_mode("fallback", "connection lost, lifeboat took over")
        elif not up and s.mode == "recovering":
            self._set_mode("fallback", "connection dropped again during recovery")
            self._good_checks = 0
        elif up and s.mode == "fallback":
            self._set_mode("recovering")
            self._good_checks = 1

        mode = self.state.mode
        if mode == "recovering":
            # fallback still acts until we are sure the link is stable
            if up:
                self._good_checks += 1
            if self._good_checks > self.monitor.recover_threshold and self._handback_ok():
                outage = self.state.fallback_steps
                self._set_mode("normal", f"handback to primary after {outage} fallback steps")
                self.state = replace(self.state, fallback_steps=0)
                mode = "normal"

        if mode == "normal":
            try:
                action = validate_action(self.primary.decide(self.state, observation))
                self.state = replace(self.state, tick=self.state.tick + 1,
                                     last_action=action["action"], last_actor="primary")
            except Exception as exc:  # primary failure is treated like a lost link
                self._set_mode("fallback", f"primary failed: {type(exc).__name__}")
                mode = "fallback"
        if mode != "normal":
            self.state, action = self.fallback.step(self.state, observation)
            if self.state.mode != mode:   # fallback.step forces "fallback"
                if mode == "recovering":
                    self.state = replace(self.state, mode="recovering")
        # integrity slowly heals while the primary is in charge
        if self.state.mode == "normal" and self.state.integrity < 1.0:
            self.state = replace(self.state, integrity=min(1.0, self.state.integrity + 0.01))
        if self.store:
            self.store.save(self.state)
        return StepResult(action, self.state.mode, self.state.last_actor,
                          "up" if self.monitor.up else "down")

    def _handback_ok(self) -> bool:
        """Re-validate the shared state before handing control back."""
        try:
            AgentState.from_dict(self.state.to_dict())
            return True
        except ValueError:
            return False
''',
    'guest.waking': r'''"""Sleep record and reorientation logic. See WAKING.md for the definition.

Application-level continuity only: plain data is saved and restored. Nothing here
preserves a running process or model, or implies subjective continuity.
"""
from __future__ import annotations

import math
import secrets
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from guest.lifeboat_seal import AgentState, StateError, remember, _num, _pos
from world.hearth_3d import Entity, WorldError, WorldState

RECORD_FORMAT = "habitat-sleep"
MAX_SEEN = 64
STATUSES = ("asleep", "awake")
MOVE_EPSILON = 0.05     # world units; smaller position changes are not reported


@dataclass(frozen=True)
class SleepRecord:
    sequence: int                 # increases on every save
    sleep_id: str                 # identifies one sleep; written to memory at most once
    status: str                   # "asleep" (clean sleep) | "awake" (autosave while running)
    clean: bool
    sleep_count: int
    saved_tick: int
    saved_wall: float
    agent: AgentState
    device: Mapping[str, Any]     # position, battery_pct, temperature_c, target
    seen_entities: Tuple[Mapping[str, Any], ...]

    def to_dict(self) -> Dict[str, Any]:
        return {"sequence": self.sequence, "sleep_id": self.sleep_id, "status": self.status,
                "clean": self.clean, "sleep_count": self.sleep_count,
                "saved_tick": self.saved_tick, "saved_wall": self.saved_wall,
                "agent": self.agent.to_dict(), "device": dict(self.device),
                "seen_entities": [dict(e) for e in self.seen_entities]}

    @staticmethod
    def from_dict(data: Any) -> "SleepRecord":
        if not isinstance(data, Mapping):
            raise StateError("sleep record must be an object")
        known = {"sequence", "sleep_id", "status", "clean", "sleep_count", "saved_tick",
                 "saved_wall", "agent", "device", "seen_entities"}
        if set(data) != known:
            raise StateError(f"sleep record fields must be exactly {sorted(known)}")
        for k in ("sequence", "sleep_count", "saved_tick"):
            v = data[k]
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise StateError(f"{k} must be a non-negative integer")
        sid = data["sleep_id"]
        if not isinstance(sid, str) or not 4 <= len(sid) <= 16 or not sid.isalnum():
            raise StateError("sleep_id must be 4-16 alphanumeric characters")
        if data["status"] not in STATUSES:
            raise StateError("status must be 'asleep' or 'awake'")
        if not isinstance(data["clean"], bool):
            raise StateError("clean must be a boolean")
        wall = _num(data["saved_wall"], "saved_wall", 0, 1e12)
        agent = AgentState.from_dict(data["agent"])
        dev = data["device"]
        if not isinstance(dev, Mapping):
            raise StateError("device must be an object")
        pos = _pos(dev.get("position"), "device.position")
        battery = _num(dev.get("battery_pct"), "device.battery_pct", 0, 100)
        temp = _num(dev.get("temperature_c"), "device.temperature_c", -50, 150)
        tgt = dev.get("target")
        tgt = None if tgt is None else list(_pos(tgt, "device.target"))
        seen = data["seen_entities"]
        if not isinstance(seen, list) or len(seen) > MAX_SEEN:
            raise StateError("seen_entities must be a list")
        try:
            clean_seen = tuple(Entity.from_dict(e).to_dict() for e in seen)
        except WorldError as exc:
            raise StateError(f"bad seen entity: {exc}") from exc
        return SleepRecord(data["sequence"], sid, data["status"], data["clean"],
                           data["sleep_count"], data["saved_tick"], wall, agent,
                           {"position": list(pos), "battery_pct": battery,
                            "temperature_c": temp, "target": tgt}, clean_seen)


def new_sleep_id() -> str:
    return secrets.token_hex(4)


def diff_entities(seen: Tuple[Mapping[str, Any], ...], world: WorldState,
                  ignore: Tuple[str, ...] = ("avatar_0",)) -> List[str]:
    """Human-readable changes between what the agent last saw and the live world."""
    before = {e["id"]: e for e in seen if e["id"] not in ignore}
    now = {e.id: e for e in world.entities() if e.id not in ignore}
    out: List[str] = []
    for eid in sorted(before):
        if eid not in now:
            out.append(f"{eid} ({before[eid]['type']}) is gone")
        else:
            a, b = before[eid]["position"], now[eid].position
            if math.dist(a, b) > MOVE_EPSILON:
                out.append(f"{eid} moved from ({a[0]:.0f},{a[2]:.0f}) to ({b[0]:.0f},{b[2]:.0f})")
    for eid in sorted(now):
        if eid not in before:
            out.append(f"{eid} ({now[eid].type}) appeared")
    return out


def nearest_energy(world: WorldState, position) -> Optional[Entity]:
    energies = world.of_type("energy")
    if not energies:
        return None
    return min(energies, key=lambda e: (math.dist(e.position, position), e.id))


def reorient(agent: AgentState, world: WorldState, device_position, seen,
             sleep_id: str, kind: str, sleep_count: int) -> Tuple[AgentState, List[str]]:
    """Return (updated agent, change descriptions). Memory gets one bounded note per
    change plus one wake line; the wake line carries the sleep id so it is added once."""
    tag = f"[{sleep_id}]"
    if any(tag in m for m in agent.memory):
        return agent, []                      # this sleep was already processed
    changes = diff_entities(seen, world)
    energy = nearest_energy(world, device_position)
    target = None if energy is None else energy.position
    if energy is None:
        changes.append("no energy source exists now")
    agent = AgentState.from_dict({**agent.to_dict(),
                                  "target": None if target is None else list(target)})
    label = "woke from sleep" if kind == "clean" else "woke after an unclean shutdown"
    agent = remember(agent, f"t{agent.tick}: {label} #{sleep_count} {tag}")
    for c in changes[:5]:
        agent = remember(agent, f"t{agent.tick}: while away, {c}")
    return agent, changes
''',
    'guest.bond': r'''"""Persistent 1:1 mind-body bond (Habitat 6.2). See BOND.md for the full description.

Three stable identifiers, minted once and then only ever loaded:
    agent_id  "ag_" + 12 hex   the mind
    body_id   "bd_" + 12 hex   the virtual body
    bond_id   "bn_" + 12 hex   the authorised connection between exactly that agent and that body

The mind acts on the body only by submitting validated JSON envelopes to `BodyLink` (the proxy front door):
    {"action_id": str, "agent_id": str, "bond_id": str, "command": str, "params": {...}}
Body commands: move_to / stop / recharge (permission-checked against the bond). Control commands: attach /
detach. `attach` can only re-attach the body this bond is already bound to; naming any other body is refused.
General body-switching is deliberately NOT implemented.

Identifiers are identifiers, not secrets: they are readable in telemetry. They prevent mix-ups and
non-bonded callers from driving the body through the mind's channel; they are not a defence against a
hostile local process (which would hold the command token anyway). See SECURITY.md.

Action lifecycle:  pending -> completed | superseded | cancelled   (recorded in a bounded ledger)
A repeated action_id is never executed twice; it returns the recorded outcome. Pending actions and the
ledger are saved together with the body state in ONE file, so they cannot disagree after a crash.
"""
from __future__ import annotations

import math
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from guest.lifeboat_seal import StateError, StateStore, _num, _pos
from guest.sealing import SealError, UnrecoverableState
from world.hearth_proxy import PERMITTED_COMMANDS, CommandError, HearthProxy, ProxyError

BOND_FORMAT = "habitat-bond"
BODY_COMMANDS = tuple(sorted(PERMITTED_COMMANDS))
CONTROL_COMMANDS = ("attach", "detach")
STATUSES = ("attached", "detached")
SOURCES = ("agent", "operator")
FINAL = ("completed", "superseded", "cancelled")
MAX_PENDING, MAX_LEDGER = 8, 64
ARRIVE_EPS = 0.05

_RE = {"agent": re.compile(r"^ag_[0-9a-f]{12}$"), "body": re.compile(r"^bd_[0-9a-f]{12}$"),
       "bond": re.compile(r"^bn_[0-9a-f]{12}$")}
_ACTION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")


class BondError(CommandError):
    """Base for every bond/permission refusal (also a CommandError, so the API answers 400)."""


class Unauthorized(BondError): ...
class PermissionDenied(BondError): ...
class NotAttached(BondError): ...
class AlreadyAttached(BondError): ...
class BodySwitchRefused(BondError): ...


def new_id(kind: str) -> str:
    return {"agent": "ag_", "body": "bd_", "bond": "bn_"}[kind] + secrets.token_hex(6)


def _id(v: Any, kind: str) -> str:
    if not isinstance(v, str) or not _RE[kind].match(v):
        raise StateError(f"{kind}_id is malformed")
    return v


# --------------------------------------------------------------------------- records
@dataclass(frozen=True)
class Bond:
    bond_id: str
    agent_id: str
    body_id: str
    status: str
    permissions: Tuple[str, ...]
    created_wall: float
    attach_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {"bond_id": self.bond_id, "agent_id": self.agent_id, "body_id": self.body_id,
                "status": self.status, "permissions": list(self.permissions),
                "created_wall": self.created_wall, "attach_count": self.attach_count}


@dataclass(frozen=True)
class ActionRecord:
    action_id: str
    source: str
    command: str
    params: Mapping[str, Any]
    state: str            # pending | completed | superseded | cancelled
    tick: int

    def to_dict(self) -> Dict[str, Any]:
        return {"action_id": self.action_id, "source": self.source, "command": self.command,
                "params": dict(self.params), "state": self.state, "tick": self.tick}


def _clean_params(command: str, params: Any) -> Dict[str, Any]:
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise StateError("params must be an object")
    if command == "move_to":
        if set(params) != {"position"}:
            raise StateError("move_to params must be exactly {'position'}")
        return {"position": list(_pos(params["position"], "position"))}
    if command == "attach":
        if set(params) != {"body_id"}:
            raise StateError("attach params must be exactly {'body_id'}")
        return {"body_id": _id(params["body_id"], "body")}
    if command in ("stop", "recharge", "detach"):
        if params:
            raise StateError(f"{command} takes no params")
        return {}
    raise StateError(f"unknown command {command!r}")


def _action_from(d: Any) -> ActionRecord:
    if not isinstance(d, Mapping) or set(d) != {"action_id", "source", "command", "params", "state", "tick"}:
        raise StateError("action record has wrong fields")
    aid = d["action_id"]
    if not isinstance(aid, str) or not _ACTION_ID.match(aid):
        raise StateError("bad action_id")
    if d["source"] not in SOURCES:
        raise StateError("bad action source")
    cmd = d["command"]
    if cmd not in BODY_COMMANDS + CONTROL_COMMANDS:
        raise StateError("bad action command")
    if d["state"] not in ("pending",) + FINAL:
        raise StateError("bad action state")
    t = d["tick"]
    if isinstance(t, bool) or not isinstance(t, int) or t < 0:
        raise StateError("bad action tick")
    return ActionRecord(aid, d["source"], cmd, _clean_params(cmd, d["params"]), d["state"], t)


@dataclass(frozen=True)
class BondRecord:
    sequence: int
    sleep_id: Optional[str]       # the sleep record written in the same save (diagnostic cross-link only)
    bond: Bond
    body_device: Mapping[str, Any]    # position, battery_pct, temperature_c, target
    pending: Tuple[ActionRecord, ...]
    ledger: Tuple[ActionRecord, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {"sequence": self.sequence, "sleep_id": self.sleep_id, "bond": self.bond.to_dict(),
                "body": {"body_id": self.bond.body_id, "device": dict(self.body_device)},
                "pending": [a.to_dict() for a in self.pending],
                "ledger": [a.to_dict() for a in self.ledger]}

    @staticmethod
    def from_dict(data: Any) -> "BondRecord":
        if not isinstance(data, Mapping) or set(data) != {"sequence", "sleep_id", "bond", "body",
                                                          "pending", "ledger"}:
            raise StateError("bond record has wrong fields")
        seq = data["sequence"]
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise StateError("sequence must be a non-negative integer")
        sid = data["sleep_id"]
        if sid is not None and (not isinstance(sid, str) or not 4 <= len(sid) <= 16 or not sid.isalnum()):
            raise StateError("sleep_id must be null or 4-16 alphanumeric characters")
        b = data["bond"]
        if not isinstance(b, Mapping) or set(b) != {"bond_id", "agent_id", "body_id", "status",
                                                     "permissions", "created_wall", "attach_count"}:
            raise StateError("bond has wrong fields")
        if b["status"] not in STATUSES:
            raise StateError("bond status must be attached or detached")
        perms = b["permissions"]
        if (not isinstance(perms, list) or len(set(perms)) != len(perms)
                or any(p not in BODY_COMMANDS for p in perms)):
            raise StateError("permissions must be a list of known body commands")
        n = b["attach_count"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise StateError("attach_count must be a non-negative integer")
        bond = Bond(_id(b["bond_id"], "bond"), _id(b["agent_id"], "agent"), _id(b["body_id"], "body"),
                    b["status"], tuple(sorted(perms)), _num(b["created_wall"], "created_wall", 0, 1e12), n)
        body = data["body"]
        if not isinstance(body, Mapping) or set(body) != {"body_id", "device"}:
            raise StateError("body has wrong fields")
        if _id(body["body_id"], "body") != bond.body_id:
            raise StateError("body_id does not match the bond (identity inconsistency)")
        dev = body["device"]
        if not isinstance(dev, Mapping):
            raise StateError("device must be an object")
        tgt = dev.get("target")
        device = {"position": list(_pos(dev.get("position"), "device.position")),
                  "battery_pct": _num(dev.get("battery_pct"), "device.battery_pct", 0, 100),
                  "temperature_c": _num(dev.get("temperature_c"), "device.temperature_c", -50, 150),
                  "target": None if tgt is None else list(_pos(tgt, "device.target"))}
        pend, led = data["pending"], data["ledger"]
        if (not isinstance(pend, list) or len(pend) > MAX_PENDING
                or not isinstance(led, list) or len(led) > MAX_LEDGER):
            raise StateError("pending/ledger lists are malformed or too long")
        pending = tuple(_action_from(a) for a in pend)
        ledger = tuple(_action_from(a) for a in led)
        if any(a.state != "pending" for a in pending) or any(a.state == "pending" for a in ledger):
            raise StateError("action states are in the wrong list")
        done = {a.action_id for a in ledger}
        # a completed action wins over a stale pending copy of itself: it must never run again
        pending = tuple(a for a in pending if a.action_id not in done)
        if bond.status == "detached" and pending:
            raise StateError("a detached bond cannot have pending actions")
        return BondRecord(seq, sid, bond, device, pending, ledger)

    @staticmethod
    def new(device: Optional[Mapping[str, Any]] = None) -> "BondRecord":
        dev = dict(device) if device else {"position": [0.0, 1.0, 0.0], "battery_pct": 100.0,
                                           "temperature_c": 22.0, "target": None}
        bond = Bond(new_id("bond"), new_id("agent"), new_id("body"), "attached", BODY_COMMANDS,
                    time.time(), 1)
        return BondRecord(0, None, bond, dev, (), ())


class StrictStore(StateStore):
    """StateStore that never silently replaces identity. If any file exists but none is usable it raises
    (UnrecoverableState / LockedError) and changes nothing, whether or not sealing is on, unless
    allow_fresh=True was set by an explicit user decision."""

    def load(self):
        self.last_load_problems = []
        bad: List[str] = []
        blocked: Optional[SealError] = None
        existing = False
        for source, path in (("primary", self.path), ("backup", self.backup)):
            if not os.path.exists(path):
                continue
            existing = True
            try:
                rec = self._read(path)
            except SealError as exc:
                blocked = blocked or exc
                self.last_load_problems.append(f"{source}: {exc}")
                bad.append(path)
                continue
            except StateError as exc:
                self.last_load_problems.append(f"{source}: {exc}")
                bad.append(path)
                continue
            for p in bad:
                self._quarantine(p)
            self.last_load_source = source
            return rec
        if existing and not self.allow_fresh:
            if blocked is not None:
                raise blocked
            raise UnrecoverableState("bond files exist but none is usable; nothing was changed, and no new "
                                     "identity was created. Details: " + "; ".join(self.last_load_problems))
        for p in bad:
            self._quarantine(p)
        self.last_load_source = "default"
        return self.default()


# --------------------------------------------------------------------------- the proxy front door
def _parse_envelope(env: Any) -> Dict[str, Any]:
    if not isinstance(env, Mapping):
        raise BondError("action must be a JSON object")
    required = {"action_id", "agent_id", "bond_id", "command"}
    if not required <= set(env) or set(env) - required - {"params"}:
        raise BondError("action must have exactly: action_id, agent_id, bond_id, command[, params]")
    aid = env["action_id"]
    if not isinstance(aid, str) or not _ACTION_ID.match(aid):
        raise BondError("action_id must be 1-40 characters of A-Z a-z 0-9 _ . : -")
    for k in ("agent_id", "bond_id", "command"):
        if not isinstance(env[k], str) or len(env[k]) > 64:
            raise BondError(f"{k} must be a short string")
    return {"action_id": aid, "agent_id": env["agent_id"], "bond_id": env["bond_id"],
            "command": env["command"], "params": env.get("params")}


class BodyLink:
    """The proxy front door between one mind and one body. Not thread-safe by itself: the Simulation
    lock serialises every call."""

    def __init__(self, proxy: HearthProxy, record: BondRecord) -> None:
        self.proxy = proxy
        self.bond = record.bond
        self.pending: List[ActionRecord] = list(record.pending)
        self.ledger: List[ActionRecord] = list(record.ledger)
        self.rejected = 0
        self._last_body_cmd: Optional[str] = None
        self.in_submit = False         # True only while this class is relaying to the proxy (tests rely on it)

    # -- identity -----------------------------------------------------------
    @property
    def body_id(self) -> str:
        return self.bond.body_id

    @property
    def attached(self) -> bool:
        return self.bond.status == "attached"

    def public(self) -> Dict[str, Any]:
        return {"bond_id": self.bond.bond_id, "agent_id": self.bond.agent_id, "body_id": self.bond.body_id,
                "status": self.bond.status, "pending": len(self.pending),
                "permissions": list(self.bond.permissions)}

    # -- submit -------------------------------------------------------------
    def submit(self, envelope: Any, tick: int) -> Dict[str, Any]:
        """Agent-side entry. Validates, authorises, then relays. Every refusal changes nothing."""
        try:
            env = _parse_envelope(envelope)
            if env["agent_id"] != self.bond.agent_id or env["bond_id"] != self.bond.bond_id:
                raise Unauthorized("agent_id/bond_id do not match this body's bond")
            return self._dispatch(env["action_id"], env["command"], env["params"], "agent", tick)
        except BondError:
            self.rejected += 1
            raise

    def submit_operator(self, command: Any, params: Any, tick: int, action_id: Optional[str] = None
                        ) -> Dict[str, Any]:
        """Operator-side entry (the authenticated HTTP API). Body commands only; same validation and
        permission checks, recorded with source "operator"."""
        if command not in BODY_COMMANDS:
            raise BondError(f"command not permitted: {command!r}")
        return self._dispatch(action_id or f"op-{secrets.token_hex(6)}", command, params, "operator", tick)

    def _dispatch(self, aid: str, command: str, params: Any, source: str, tick: int) -> Dict[str, Any]:
        if command not in BODY_COMMANDS + CONTROL_COMMANDS:
            raise BondError(f"command not permitted: {command!r}")
        try:
            clean = _clean_params(command, params)
        except StateError as exc:
            raise BondError(str(exc)) from exc
        prior = next((a for a in self.pending + self.ledger if a.action_id == aid), None)
        if prior is not None:
            if prior.command != command or dict(prior.params) != clean:
                raise BondError("action_id was already used for a different action")
            return {"action_id": aid, "state": prior.state, "replayed": True}
        if command == "attach":
            return self._attach(aid, clean, source, tick)
        if command == "detach":
            return self._detach(aid, source, tick)
        if source == "agent" and not self.attached:
            raise NotAttached("the mind is detached from this body")
        if command not in self.bond.permissions:
            raise PermissionDenied(f"{command!r} is not permitted by this bond")
        return self._body_command(aid, command, clean, source, tick)

    # -- control commands ---------------------------------------------------
    def _attach(self, aid: str, params: Dict[str, Any], source: str, tick: int) -> Dict[str, Any]:
        if params["body_id"] != self.bond.body_id:
            raise BodySwitchRefused("this bond is bound to a different body; switching bodies is not supported")
        if self.attached:
            raise AlreadyAttached("already attached")
        self.bond = Bond(self.bond.bond_id, self.bond.agent_id, self.bond.body_id, "attached",
                         self.bond.permissions, self.bond.created_wall, self.bond.attach_count + 1)
        self._record(ActionRecord(aid, source, "attach", params, "completed", tick))
        return {"action_id": aid, "state": "completed"}

    def _detach(self, aid: str, source: str, tick: int) -> Dict[str, Any]:
        if not self.attached:
            raise NotAttached("already detached")
        for p in list(self.pending):                       # nothing may keep running for a mind that left
            self._finish(p, "cancelled")
        self._relay("stop", {})
        self.bond = Bond(self.bond.bond_id, self.bond.agent_id, self.bond.body_id, "detached",
                         self.bond.permissions, self.bond.created_wall, self.bond.attach_count)
        self._record(ActionRecord(aid, source, "detach", {}, "completed", tick))
        return {"action_id": aid, "state": "completed"}

    # -- body commands ------------------------------------------------------
    def _relay(self, command: str, params: Dict[str, Any]) -> None:
        self.in_submit = True
        try:
            self.proxy.command(command, params)
        finally:
            self.in_submit = False
        self._last_body_cmd = command

    def _body_command(self, aid: str, command: str, params: Dict[str, Any], source: str, tick: int
                      ) -> Dict[str, Any]:
        move = next((p for p in self.pending if p.command == "move_to"), None)
        if command == "move_to" and move is not None and move.params == params:
            return {"action_id": move.action_id, "state": "pending", "coalesced": True}
        if command == "stop" and move is None and self._last_body_cmd == "stop":
            return {"action_id": aid, "state": "noop", "coalesced": True}
        self._relay(command, params)                 # raises CommandError on bad params: nothing recorded
        if move is not None and command in ("move_to", "stop"):     # recharge leaves the target alone
            self._finish(move, "superseded" if command == "move_to" else "cancelled")
        rec = ActionRecord(aid, source, command, params, "pending" if command == "move_to" else "completed", tick)
        if rec.state == "pending":
            self.pending.append(rec)
            del self.pending[:-MAX_PENDING]
        else:
            self._record(rec)
        return {"action_id": aid, "state": rec.state}

    # -- bookkeeping ----------------------------------------------------------
    def _record(self, rec: ActionRecord) -> None:
        self.ledger.append(rec)
        del self.ledger[:-MAX_LEDGER]

    def _finish(self, rec: ActionRecord, state: str) -> None:
        if rec in self.pending:
            self.pending.remove(rec)
        self._record(ActionRecord(rec.action_id, rec.source, rec.command, rec.params, state, rec.tick))

    def settle(self, position, tick: int) -> None:
        """After the body advanced: a pending move that arrived (or was stopped by someone) is finished."""
        try:
            device_target = self.proxy.export_device().get("target")
        except ProxyError:
            device_target = "unknown"
        for p in list(self.pending):
            if math.dist(position, p.params["position"]) <= ARRIVE_EPS:
                self._finish(p, "completed")
            elif device_target is None:
                self._finish(p, "cancelled")

    def reassert(self) -> None:
        """After a wake: a pending move whose target the restored body no longer holds is re-applied
        (same action id, so nothing is recorded twice). It only ever replays this bond's own pending moves:
        it cannot change identity, status or permissions, and does nothing for a detached mind."""
        if not self.attached:
            return
        try:
            device_target = self.proxy.export_device().get("target")
        except ProxyError:
            return
        for p in self.pending:
            if p.command == "move_to" and (device_target is None
                                           or math.dist(device_target, p.params["position"]) > 1e-9):
                self._relay("move_to", dict(p.params))

    def to_record(self, sequence: int, sleep_id: Optional[str], device: Mapping[str, Any]) -> BondRecord:
        return BondRecord(sequence, sleep_id, self.bond, dict(device), tuple(self.pending), tuple(self.ledger))
''',
    'guest.llm_clients': r'''"""SPIKE: a local-only model client. Talks to an Ollama-style server on this machine and nowhere else.

The host must be loopback (127.0.0.1, localhost or ::1); any other URL is refused at construction.
Not tested against a real Ollama install: tests use a fake local server that mimics /api/generate.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

MAX_RESPONSE_BYTES = 64 * 1024
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class ClientError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A local endpoint must not redirect model prompts to another host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OllamaClient:
    def __init__(self, model: str, url: str = "http://127.0.0.1:11434") -> None:
        parts = urllib.parse.urlsplit(url)
        if (parts.scheme != "http" or parts.hostname not in LOCAL_HOSTS or parts.username is not None
                or parts.password is not None or "@" in parts.netloc):
            raise ValueError("only http://127.0.0.1, http://localhost or http://[::1] model servers are allowed")
        if not model or len(model) > 100:
            raise ValueError("model name required (at most 100 characters)")
        self.model, self.base = model, f"{parts.scheme}://{parts.netloc}"

    def complete(self, prompt: str, timeout: float) -> str:
        body = json.dumps({"model": self.model, "prompt": prompt, "stream": False, "format": "json",
                           "options": {"temperature": 0.2, "num_predict": 150}}).encode()
        req = urllib.request.Request(self.base + "/api/generate", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(req, timeout=timeout) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, OSError) as exc:
            raise ClientError(f"model server unreachable: {type(exc).__name__}") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ClientError("model response too large")
        try:
            text = json.loads(raw).get("response")
        except (ValueError, AttributeError) as exc:
            raise ClientError("model server returned unreadable data") from exc
        if not isinstance(text, str):
            raise ClientError("model server reply has no 'response' text")
        return text
''',
    'guest.llm_adapter': r'''"""SPIKE (not part of frozen 6.2): language-model adapter that never blocks the tick.

Design
  * decide() is non-blocking. It returns the last validated action immediately; the model is called on a
    worker thread every `interval_ticks` decisions, with a deadline of `timeout` seconds.
  * The model reply must be one JSON object with only the keys "action" (and "position" for move_toward).
    Anything else is a failure, never repaired. Positions go through the same validate_action as everything else.
  * `max_failures` consecutive failures (bad reply, error, timeout) raise AdapterFailure, which makes the existing
    GuestController hand over to the lifeboat fallback. The counter then restarts for the next handback.
  * Identity and memory stay in AgentState. The model sees a short plain-text summary and keeps no history of its own.
  * No network code lives here. `client` is any object with complete(prompt, timeout) -> str that the caller supplies.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional

from guest.guest_claude import AgentAdapter
from guest.lifeboat_seal import PERMITTED_ACTIONS, ActionError, AgentState, validate_action

MAX_REPLY_CHARS = 2000
MAX_ABANDONED = 2          # hung worker threads tolerated before we refuse to start more
SYSTEM_TEXT = ("You control a small robot body in a simulation. Reply with ONE JSON object and nothing else. "
               'Allowed: {"action":"hold"}, {"action":"rest"}, {"action":"scan"}, '
               '{"action":"move_toward","position":[x,y,z]}. No other actions exist.')


class AdapterFailure(RuntimeError):
    """Raised to the controller so it falls back to the lifeboat."""


def parse_reply(text: Any) -> Dict[str, Any]:
    """Strictly parse a model reply into a validated action, or raise AdapterFailure."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_REPLY_CHARS:
        raise AdapterFailure("reply missing or too long")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AdapterFailure("reply is not JSON") from exc
    if not isinstance(data, dict) or not set(data) <= {"action", "position"}:
        raise AdapterFailure("reply must be an object with only action/position")
    if data.get("action") not in PERMITTED_ACTIONS:
        raise AdapterFailure("action not permitted")
    if data["action"] != "move_toward" and "position" in data:
        raise AdapterFailure("position only allowed with move_toward")
    try:
        return validate_action(data)
    except ActionError as exc:
        raise AdapterFailure(str(exc)) from exc


def build_prompt(state: AgentState, observation: Mapping[str, Any]) -> str:
    me = observation.get("self", {}) if isinstance(observation.get("self"), Mapping) else {}
    near = observation.get("nearest_energy")
    lines = [SYSTEM_TEXT,
             f"Tick {observation.get('tick')}. Battery {me.get('battery_pct')}%. Position {me.get('position')}.",
             f"Nearest energy: {near}.",
             f"Goals: {list(state.goals)}.",
             f"Recent memory: {list(state.memory[-5:])}."]
    return "\n".join(lines)


class AsyncLLMAdapter(AgentAdapter):
    name = "llm_async"

    def __init__(self, client: Any, interval_ticks: int = 6, timeout: float = 5.0, max_failures: int = 3,
                 log_limit: int = 200, clock: Callable[[], float] = time.monotonic) -> None:
        if interval_ticks < 1 or timeout <= 0 or max_failures < 1:
            raise ValueError("interval_ticks and max_failures must be >= 1 and timeout > 0")
        self.client, self.interval, self.timeout, self.max_failures = client, interval_ticks, timeout, max_failures
        self._clock = clock
        self._lock = threading.Lock()
        self._action: Dict[str, Any] = {"action": "hold"}
        self._failures = 0
        self._since_launch = interval_ticks          # first decide() launches straight away
        self._job: Optional[Dict[str, Any]] = None   # {"id", "started", "done", "reply", "error", "prompt"}
        self._abandoned: List[threading.Thread] = []
        self._next_id = 0
        self.log: Deque[Dict[str, Any]] = deque(maxlen=log_limit)   # prompt + raw reply + outcome, in memory only

    # -- worker --------------------------------------------------------
    def _run(self, job: Dict[str, Any]) -> None:
        try:
            reply = self.client.complete(job["prompt"], self.timeout)
            with self._lock:
                job["reply"] = reply
        except Exception as exc:   # any client error is a failure, not a crash
            with self._lock:
                job["error"] = f"{type(exc).__name__}"
        finally:
            with self._lock:
                job["done"] = True

    def _record(self, job: Dict[str, Any], outcome: str) -> None:
        self.log.append({"prompt": job["prompt"], "reply": job.get("reply"), "outcome": outcome})

    def _harvest(self) -> None:
        job = self._job
        if job is None:
            return
        with self._lock:
            done, reply, error = job["done"], job.get("reply"), job.get("error")
        if done:
            self._job = None
            if error:
                self._fail(job, f"error:{error}")
                return
            try:
                self._action = parse_reply(reply)
                self._failures = 0
                self._record(job, "ok")
            except AdapterFailure as exc:
                self._fail(job, f"rejected:{exc}")
        elif self._clock() - job["started"] > self.timeout:
            self._job = None                       # abandon; a late reply is simply dropped
            self._abandoned.append(job["thread"])
            self._fail(job, "timeout")

    def _fail(self, job: Dict[str, Any], why: str) -> None:
        self._failures += 1
        self._record(job, why)

    # -- AgentAdapter --------------------------------------------------
    def decide(self, state: AgentState, observation: Mapping[str, Any]) -> Dict[str, Any]:
        self._abandoned = [t for t in self._abandoned if t.is_alive()]
        self._harvest()
        if self._failures >= self.max_failures:
            self._failures = 0
            self._since_launch = self.interval
            raise AdapterFailure(f"{self.max_failures} consecutive model failures")
        self._since_launch += 1
        if self._job is None and self._since_launch >= self.interval:
            if len(self._abandoned) >= MAX_ABANDONED:
                self._failures += 1                # too many hung calls; do not pile up threads
            else:
                self._since_launch = 0
                self._next_id += 1
                job: Dict[str, Any] = {"id": self._next_id, "started": self._clock(), "done": False,
                                       "prompt": build_prompt(state, observation)}
                t = threading.Thread(target=self._run, args=(job,), daemon=True)
                job["thread"] = t
                self._job = job
                t.start()
        return dict(self._action)
''',
    'world_edit': r'''"""Edit the world file while the server is stopped or asleep.

    python3 world_edit.py [--world world.json] list
    python3 world_edit.py move target_0 -5 0.5 3
    python3 world_edit.py remove target_0
    python3 world_edit.py add energy_2 energy 2 0.5 2 [--color 16724784]

Edits are validated and written atomically. While the server is awake it owns the file
and will overwrite your change.
"""
import argparse
import sys

from world.hearth_3d import Entity, WorldError
from world.world_file import load_world, save_world


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--world", default="world.json")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    m = sub.add_parser("move"); m.add_argument("id"); m.add_argument("x", type=float); m.add_argument("y", type=float); m.add_argument("z", type=float)
    r = sub.add_parser("remove"); r.add_argument("id")
    a = sub.add_parser("add"); a.add_argument("id"); a.add_argument("type")
    for ax in "xyz":
        a.add_argument(ax, type=float)
    a.add_argument("--color", type=int, default=0xFFFFFF)
    args = p.parse_args(argv)
    world, source, problems = load_world(args.world)
    for prob in problems:
        print("warning:", prob, file=sys.stderr)
    if source == "default":
        print("note: no usable world file; starting from the default world", file=sys.stderr)
    try:
        if args.cmd == "list":
            for e in world.entities():
                print(e.id, e.type, list(e.position))
            return 0
        if args.cmd == "move":
            world.update(args.id, position=(args.x, args.y, args.z))
        elif args.cmd == "remove":
            world.remove(args.id)
        else:
            world.add(Entity.create(args.id, args.type, (args.x, args.y, args.z), args.color))
    except WorldError as exc:
        print("error:", exc, file=sys.stderr)
        return 2
    save_world(args.world, world)
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
''',
    'server': r'''"""Habitat backend: simulation loop + small local HTTP API.

Start:   python run.py            (then open http://127.0.0.1:8765/)
Binds to 127.0.0.1 only. Read endpoints are open to local clients; the one
write endpoint requires a random per-launch token printed at startup.

Endpoints
    GET  /                    the viewer (extras/vent.html)
    GET  /api/health          {"ok": true, "schema": ...}
    GET  /api/telemetry       one habitat.telemetry/1 frame
    POST /api/command         JSON {"command": ..., "params": {...}}, header X-Habitat-Token
         commands: move_to {position}, stop, recharge, simulate_outage {down: bool}
No other files are served and nothing outside this project is readable.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from guest.guest_claude import (ConnectionMonitor, GuestController, ScriptedPrimary,
                                SimulatedTransport)
from guest.lifeboat_seal import StateStore
from guest.sealing import SealError, Sealer
from guest.bond import (BOND_FORMAT, BondError, BondRecord, BodyLink, StrictStore)
from world.sensory import observe
from guest.waking import RECORD_FORMAT, SleepRecord, nearest_energy, new_sleep_id, reorient
from world.hearth_3d import Entity
from world.world_file import load_world, save_world
import time
from world.hearth_3d import (WorldError, WorldState, build_telemetry, default_world,
                             TELEMETRY_SCHEMA)
from world.hearth_proxy import CommandError, HearthProxy, ProxyError

ROOT = os.path.dirname(os.path.abspath(__file__))
VIEWER = os.path.join(ROOT, "extras", "vent.html")
MAX_BODY = 2048
WAYPOINTS = [(6.0, 0.5, -4.0), (-5.0, 0.5, -6.0), (-7.0, 0.5, 4.0), (4.0, 0.5, 7.0)]


class Simulation:
    """Owns the world, proxy, transport and guest controller; one tick at a time.

    Persistence (all optional; none if no paths are given):
        state_path  lifeboat_state.json  per-tick agent state (checksummed)
        sleep_path  sleep_record.json    sleep / autosave record (see WAKING.md)
        world_path  world.json           plain, hand-editable world file
    If only `state_path` is given, the other two are placed beside it.
    """

    AUTOSAVE_EVERY = 10

    def __init__(self, state_path: Optional[str] = None, proxy: Optional[HearthProxy] = None,
                 transport: Optional[SimulatedTransport] = None, sleep_path: Optional[str] = None,
                 world_path: Optional[str] = None, bond_path: Optional[str] = None,
                 passphrase: Optional[str] = None,
                 allow_plaintext: bool = False, allow_fresh: bool = False) -> None:
        self.lock = threading.RLock()
        if state_path:
            base = os.path.dirname(os.path.abspath(state_path))
            sleep_path = sleep_path or os.path.join(base, "sleep_record.json")
            world_path = world_path or os.path.join(base, "world.json")
            bond_path = bond_path or os.path.join(base, "bond_record.json")
        self.world_path = world_path
        self.proxy = proxy or HearthProxy()
        self.transport = transport or SimulatedTransport()
        sealer = Sealer(passphrase) if passphrase is not None else None
        self.sealed = sealer is not None
        opts = {"sealer": sealer, "allow_plaintext": allow_plaintext, "allow_fresh": allow_fresh}
        self.store = StateStore(state_path, **opts) if state_path else None
        self.sleep_store = (StateStore(sleep_path, fmt=RECORD_FORMAT, parser=SleepRecord.from_dict,
                                       default=lambda: None, **opts) if sleep_path else None)
        self.bond_store = (StrictStore(bond_path, fmt=BOND_FORMAT, parser=BondRecord.from_dict,
                                       default=self._new_bond_record, **opts) if bond_path else None)
        self.link: Optional[BodyLink] = None
        self.last_observation: Optional[Dict[str, Any]] = None
        self._body_restored = False
        self._action_n = 0
        self.asleep = False
        self.sleep_count = 0
        self._sequence = 0
        self._epoch_id = new_sleep_id()    # identifies this awake period in autosaves
        self._wp = 0
        self.last_wake: Dict[str, Any] = {"kind": "fresh", "changes": [], "sleep_count": 0}
        self.problems: list = []
        self._boot()

    # -- startup / waking -------------------------------------------------
    def _load_world(self) -> bool:
        """Load world.json if configured. Returns True if a world file was used."""
        if not self.world_path:
            self.world = default_world()
            return False
        self.world, source, probs = load_world(self.world_path)
        self.problems += probs
        return source != "default"

    def _make_controller(self) -> None:
        self.controller = GuestController(
            ScriptedPrimary(), ConnectionMonitor(self.transport, timeout=0.5), self.store)

    def _new_bond_record(self) -> BondRecord:
        try:
            dev = self.proxy.export_device()
        except ProxyError:
            dev = None
        return BondRecord.new(dev)

    def _restore_bond_and_body(self) -> None:
        """Load the bond record (or mint identities once, if none exists) and put the body back as saved."""
        record = self.bond_store.load() if self.bond_store else self._new_bond_record()
        if self.bond_store and self.bond_store.last_load_problems:
            self.problems += [f"bond: {p}" for p in self.bond_store.last_load_problems]
        from_file = bool(self.bond_store) and self.bond_store.last_load_source in ("primary", "backup")
        if (self.bond_store and not from_file and self.sleep_store
                and os.path.exists(self.sleep_store.path)):
            self.problems.append("bond: no bond record found next to existing saved state (older data, or the "
                                 "file was removed): NEW agent/body/bond ids were created")
        self.link = BodyLink(self.proxy, record)
        self._sequence = max(self._sequence, record.sequence)
        self._body_restored = False
        if from_file:
            try:
                self.proxy.import_device(record.body_device)
                self._body_restored = True
            except ProxyError as exc:
                self.problems.append(f"body state not restored: {exc}")
            self.link.reassert()

    def _observe(self) -> Dict[str, Any]:
        self.last_observation = observe(self.link.body_id, self.proxy.telemetry(), self.world, self.world.tick)
        return self.last_observation

    def _boot(self) -> None:
        had_world = self._load_world()
        self._make_controller()
        self._restore_bond_and_body()
        record = self.sleep_store.load() if self.sleep_store else None
        if self.sleep_store and self.sleep_store.last_load_problems:
            self.problems += self.sleep_store.last_load_problems
        if record is not None:
            self._wake_from(record)
        else:
            if not had_world:
                self.world.update("target_0", position=WAYPOINTS[0])
            self.world.tick = self.controller.state.tick
        self._ensure_avatar()

    def _ensure_avatar(self) -> None:
        tel = self.proxy.telemetry()
        if self.world.get("avatar_0") is None:
            self.world.add(Entity.create("avatar_0", "proxy", tel.position, 0x00FF00))
        else:
            self.world.update("avatar_0", position=tel.position)

    def _wake_from(self, record: SleepRecord) -> None:
        """Restore, reorient and resume from a record (steps 2-4 in WAKING.md)."""
        kind = "clean" if (record.status == "asleep" and record.clean) else "unclean"
        agent = record.agent
        if kind == "unclean" and self.store is not None:
            newer = self.store.load()          # per-tick state is newer than the autosave
            if self.store.last_load_source != "default" and newer.tick >= record.agent.tick:
                agent = newer
        if not self._body_restored:         # legacy data without a bond record: use the sleep record's copy
            try:
                self.proxy.import_device(record.device)
            except ProxyError as exc:
                self.problems.append(f"device state not restored: {exc}")
        tel = self.proxy.telemetry()
        agent, changes = reorient(agent, self.world, tel.position, record.seen_entities,
                                  record.sleep_id, kind, record.sleep_count)
        self.controller.state = agent
        self.controller._good_checks = 0
        self.world.tick = agent.tick
        self.sleep_count = record.sleep_count
        self._sequence = max(self._sequence, record.sequence)    # never go back past a newer bond save
        self._sync_avatar()
        self.asleep = False
        self.last_wake = {"kind": kind, "changes": changes[:5], "sleep_count": record.sleep_count}
        self._epoch_id = new_sleep_id()
        self._observe()                      # fresh look at the world; nothing observed is ever restored
        self._save_record(status="awake", clean=False, sleep_id=self._epoch_id)

    def _sync_avatar(self) -> None:
        pos = self.proxy.telemetry().position
        if self.world.get("avatar_0") is None:
            self.world.add(Entity.create("avatar_0", "proxy", pos, 0x00FF00))
        else:
            self.world.update("avatar_0", position=pos)

    # -- saving -----------------------------------------------------------
    def _save_record(self, status: str, clean: bool, sleep_id: Optional[str] = None) -> None:
        if not self.sleep_store:
            return
        self._sequence += 1
        sleep_id = sleep_id or new_sleep_id()
        if self.bond_store:        # bond + body + pending actions first, then the sleep record
            self.bond_store.save(self.link.to_record(self._sequence, sleep_id, self.proxy.export_device()))
        rec = SleepRecord(
            sequence=self._sequence, sleep_id=sleep_id, status=status,
            clean=clean, sleep_count=self.sleep_count, saved_tick=self.world.tick,
            saved_wall=time.time(), agent=self.controller.state,
            device=self.proxy.export_device(),
            seen_entities=tuple(e.to_dict() for e in self.world.entities()))
        self.sleep_store.save(rec)
        if self.world_path:
            save_world(self.world_path, self.world)

    def sleep(self) -> bool:
        """Save everything and stop ticking. Returns False if already asleep."""
        with self.lock:
            if self.asleep:
                return False
            self.sleep_count += 1
            if self.store:
                self.store.save(self.controller.state)
            self._save_record(status="asleep", clean=True)
            self.asleep = True
            return True

    def wake(self) -> Dict[str, Any]:
        """Wake in-process by re-reading the files from disk (same path as a restart).
        This honours offline world edits made while asleep."""
        with self.lock:
            if not self.asleep:
                raise CommandError("not asleep")
            if not self.sleep_store:
                raise CommandError("no sleep record is configured")
            self._load_world()
            try:
                self._restore_bond_and_body()
            except SealError as exc:
                raise CommandError(f"cannot restore the bond: {exc}") from exc
            record = self.sleep_store.load()
            if record is None:
                raise CommandError("sleep record is missing or unreadable; cannot wake")
            self._wake_from(record)
            return dict(self.last_wake)

    # -- ticking ----------------------------------------------------------
    def _energy(self):
        pos = self.world.get("avatar_0").position
        return nearest_energy(self.world, pos)

    def tick(self) -> None:
        with self.lock:
            if self.asleep:
                return
            tel = self.proxy.telemetry()
            target = self._energy()
            seen = self._observe()           # the mind sees only this plain observation
            if self.link.attached:
                obs = {**seen, "battery_pct": seen["self"]["battery_pct"],
                       "target": None if seen["nearest_energy"] is None else seen["nearest_energy"]["position"]}
                self._submit_mind_action(self.controller.step(obs).action, tel)
            tel = self.proxy.step()
            self.link.settle(tel.position, self.world.tick)
            # at the energy source: recharge, then move the source to the next waypoint
            if target and _near(tel.position, target.position, 0.6):
                if tel.battery_pct < 95:
                    self.proxy.command("recharge")
                else:
                    self._wp = (self._wp + 1) % len(WAYPOINTS)
                    self.world.update(target.id, position=WAYPOINTS[self._wp])
            self.world.update("avatar_0", position=tel.position)
            self.world.advance()
            if self.sleep_store and self.world.tick % self.AUTOSAVE_EVERY == 0:
                self._save_record(status="awake", clean=False, sleep_id=self._epoch_id)

    def _submit_mind_action(self, act: Dict[str, Any], tel) -> None:
        """The mind's decision becomes a validated JSON envelope to the proxy front door (never a direct call)."""
        if act["action"] == "move_toward":
            x, _, z = act["position"]       # keep the avatar at its own height; only x/z come from the target
            command, params = "move_to", {"position": [x, tel.position[1], z]}
        elif act["action"] in ("hold", "rest"):
            command, params = "stop", {}
        else:
            return                           # "scan": observation only, no body command
        self._action_n += 1
        env = {"action_id": f"{self._epoch_id}-{self._action_n}", "agent_id": self.link.bond.agent_id,
               "bond_id": self.link.bond.bond_id, "command": command, "params": params}
        try:
            self.link.submit(env, self.world.tick)
        except BondError as exc:             # e.g. permission revoked: the body stays safe, the loop lives on
            self.problems.append(f"t{self.world.tick}: mind action refused: {exc}")
            del self.problems[:-20]

    def telemetry(self) -> Dict[str, Any]:
        with self.lock:
            tel = self.proxy.telemetry().to_dict()
            hw = {"source": tel["source"], "confirmed": tel["confirmed"],
                  "battery_pct": round(tel["battery_pct"], 2),
                  "temperature_c": round(tel["temperature_c"], 2), "status": tel["status"]}
            agent = self.controller.snapshot()
            agent["asleep"] = self.asleep
            agent["sealed"] = self.sealed
            agent["wake"] = dict(self.last_wake)
            agent["bond"] = self.link.public()
            return build_telemetry(self.world, hw, agent)

    def command(self, name: Any, params: Any) -> Dict[str, Any]:
        with self.lock:
            if name == "simulate_outage":
                if not isinstance(params, dict) or not isinstance(params.get("down"), bool):
                    raise CommandError("simulate_outage needs {'down': true|false}")
                self.transport.up = not params["down"]
                return {"transport_up": self.transport.up}
            if name == "sleep":
                return {"asleep": True, "newly": self.sleep()}
            if name == "wake":
                return {"asleep": False, "wake": self.wake()}
            if self.asleep:
                raise CommandError("agent is asleep; send 'wake' first")
            if name in ("attach", "detach"):
                if (not isinstance(params, dict) or not isinstance(params.get("agent_id"), str)
                        or not isinstance(params.get("bond_id"), str)):
                    raise CommandError(f"{name} needs 'agent_id' and 'bond_id'")
                self._action_n += 1
                inner = {k: v for k, v in params.items() if k not in ("agent_id", "bond_id")}
                res = self.link.submit({"action_id": f"op{self._epoch_id}-{self._action_n}",
                                        "agent_id": params["agent_id"], "bond_id": params["bond_id"],
                                        "command": name, "params": inner}, self.world.tick)
                return {"accepted": name, "state": res["state"]}
            self.link.submit_operator(name, params, self.world.tick)
            return {"accepted": name}


def _near(a, b, r: float) -> bool:
    return sum((a[i] - b[i]) ** 2 for i in range(3)) <= r * r


def make_handler(sim: Simulation, token: str, port_getter):
    class Handler(BaseHTTPRequestHandler):
        timeout = 10                       # drop stalled connections
        server_version = "Habitat/6"

        def log_message(self, *args):      # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj).encode())

        def _host_ok(self) -> bool:
            # blocks DNS-rebinding style requests with a foreign Host header
            host = (self.headers.get("Host") or "").lower()
            return host in (f"127.0.0.1:{port_getter()}", f"localhost:{port_getter()}")

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html", "/extras/vent.html"):
                try:
                    return self._send(200, _EMBEDDED_VIEWER.encode("utf-8"), "text/html; charset=utf-8")
                except OSError:
                    return self._json(500, {"error": "viewer file missing"})
            if path == "/api/health":
                return self._json(200, {"ok": True, "schema": TELEMETRY_SCHEMA})
            if path == "/api/telemetry":
                try:
                    return self._json(200, sim.telemetry())
                except ProxyError as exc:
                    return self._json(503, {"error": str(exc)})
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            if self.path.split("?", 1)[0] != "/api/command":
                return self._json(404, {"error": "not found"})
            if not secrets.compare_digest(self.headers.get("X-Habitat-Token", ""), token):
                return self._json(401, {"error": "missing or wrong X-Habitat-Token"})
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._json(415, {"error": "Content-Type must be application/json"})
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._json(411, {"error": "Content-Length required"})
            if length < 0 or length > MAX_BODY:
                return self._json(413, {"error": f"body limited to {MAX_BODY} bytes"})
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError):
                return self._json(400, {"error": "body is not valid JSON"})
            if not isinstance(body, dict) or set(body) - {"command", "params"}:
                return self._json(400, {"error": "body must be {'command': str, 'params': {}}"})
            try:
                return self._json(200, sim.command(body.get("command"), body.get("params") or {}))
            except (CommandError, WorldError) as exc:
                return self._json(400, {"error": str(exc)})
            except ProxyError as exc:
                return self._json(503, {"error": str(exc)})

        def do_PUT(self):
            self._json(405, {"error": "method not allowed"})
        do_DELETE = do_PATCH = do_PUT

    return Handler


class TickErrorReporter:
    """Report a repeating tick error once, then an occasional reminder, instead of one line per tick.

    A streak is a run of consecutive failures with the same exception type (the message can differ each time,
    for example random temp-file names). A successful tick resets it.
    """

    def __init__(self, every: int = 100) -> None:
        self.every, self._kind, self._count = every, None, 0

    def ok(self) -> None:
        self._kind, self._count = None, 0

    def report(self, exc: BaseException) -> Optional[str]:
        kind = type(exc).__name__
        if kind != self._kind:
            self._kind, self._count = kind, 0
        self._count += 1
        text = f"{kind}: {exc}"
        if self._count == 1:
            return f"[tick error] {text}"
        if self._count % self.every == 0:
            return f"[tick error] still failing ({self._count} times in a row): {text}  (Ctrl+C to stop)"
        return None


class HabitatServer:
    """Runs the HTTP API and the tick loop in background threads."""

    def __init__(self, sim: Simulation, host: str = "127.0.0.1", port: int = 8765,
                 tick_seconds: float = 0.5, token: Optional[str] = None) -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("refusing to bind to a non-local address")
        self.sim, self.tick_seconds = sim, tick_seconds
        self.token = token or secrets.token_urlsafe(16)
        self.httpd = ThreadingHTTPServer(
            (host, port), make_handler(sim, self.token, lambda: self.httpd.server_address[1]))
        self.httpd.daemon_threads = True
        self._stop = threading.Event()
        self._threads = []

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self) -> None:
        self._threads = [threading.Thread(target=self.httpd.serve_forever, daemon=True),
                         threading.Thread(target=self._loop, daemon=True)]
        for t in self._threads:
            t.start()

    def _loop(self) -> None:
        reporter = TickErrorReporter()
        while not self._stop.wait(self.tick_seconds):
            try:
                self.sim.tick()
                reporter.ok()
            except Exception as exc:   # keep serving; surface the error (once per streak, not once per tick)
                line = reporter.report(exc)
                if line:
                    print(line)

    def stop(self) -> None:
        self._stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()
''',
    'habitat62_run': r'''"""Start the Habitat backend:   python run.py [--port 8765] [--state lifeboat_state.json]

Then open http://127.0.0.1:8765/ in a browser. Ctrl+C stops it.
Everything is simulated; no hardware, network services or paid APIs are used.
"""
import argparse
import getpass
import os
import sys
import tempfile
import time

from server import HabitatServer, Simulation


def get_passphrase(state_path: str) -> str:
    """Passphrase from HABITAT_PASSPHRASE or a hidden prompt (twice when no sealed state exists yet)."""
    pw = os.environ.get("HABITAT_PASSPHRASE")
    if pw is None:
        pw = getpass.getpass("Habitat passphrase: ")
        if not os.path.exists(state_path):
            if getpass.getpass("Repeat passphrase: ") != pw:
                sys.exit("Passphrases do not match.")
    if len(pw) < 10:
        sys.exit("Passphrase must be at least 10 characters.")
    return pw


def check_state_dir(state_path: str) -> None:
    """Stop early, with a plain message, if Habitat cannot save files where it was told to."""
    folder = os.path.dirname(os.path.abspath(state_path))
    try:
        os.makedirs(folder, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".habitat_write_test_"):
            pass
    except OSError as exc:
        print(f"Cannot save files in: {folder}\n  ({type(exc).__name__}: {exc})\n"
              "Habitat saves its state in the folder you start it from. Open a normal (not administrator) "
              "terminal inside the habitat folder, or pick a folder you own with --state <path>.", file=sys.stderr)
        sys.exit(4)


def main() -> None:
    p = argparse.ArgumentParser(description="Habitat 6 local backend (simulation only)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--tick", type=float, default=0.5, help="seconds per simulation tick")
    p.add_argument("--state", default="lifeboat_state.json", help="agent state file")
    p.add_argument("--fresh", action="store_true", help="ignore all saved state (agent, sleep record, world, and the bond: NEW agent/body/bond ids are created)")
    p.add_argument("--seal", action="store_true",
                   help="encrypt agent state and sleep record at rest; passphrase from the "
                        "HABITAT_PASSPHRASE environment variable or a hidden prompt (never a command-line argument)")
    p.add_argument("--migrate-plaintext", action="store_true",
                   help="with --seal: accept existing unsealed files once; they are re-saved sealed")
    p.add_argument("--discard-unreadable", action="store_true",
                   help="start fresh even though saved state exists but cannot be read (set aside as *.corrupt, never deleted)")
    p.add_argument("--no-sleep-on-exit", action="store_true", help="do not save a clean sleep record on Ctrl+C")
    args = p.parse_args()
    check_state_dir(args.state)

    if args.fresh:
        base = os.path.dirname(os.path.abspath(args.state))
        for name in (os.path.basename(args.state), "sleep_record.json", "world.json", "bond_record.json"):
            for suffix in ("", ".bak"):
                try:
                    os.remove(os.path.join(base, name + suffix))
                except OSError:
                    pass
    passphrase = None
    if args.seal:
        passphrase = get_passphrase(args.state)
    from guest.sealing import SealError
    try:
        sim = Simulation(state_path=args.state, passphrase=passphrase,
                         allow_plaintext=args.migrate_plaintext, allow_fresh=args.discard_unreadable)
    except SealError as exc:
        print(f"Cannot open saved state: {exc}", file=sys.stderr)
        print("Nothing was changed. Check the passphrase (use --seal for sealed state). "
              "If it is truly lost, --discard-unreadable starts fresh and sets the old files aside.",
              file=sys.stderr)
        sys.exit(3)
    srv = HabitatServer(sim, port=args.port, tick_seconds=args.tick)
    srv.start()
    print(f"State at rest: {'SEALED (AES-256-GCM)' if sim.sealed else 'plaintext'}")
    print(f"Habitat 6 (simulation) at http://127.0.0.1:{srv.port}/")
    print(f"Command token for POST /api/command: {srv.token}")
    print(f"Wake: {sim.last_wake['kind']}" + (f" ({len(sim.last_wake['changes'])} changes while away)"
          if sim.last_wake['kind'] != 'fresh' else ""))
    for note in sim.last_wake["changes"]:
        print(f"  while away: {note}")
    for prob in sim.problems:
        print(f"  recovered from damaged file: {prob}")
    import signal
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping.")
        srv.stop()
        if not args.no_sleep_on_exit:
            sim.sleep()
            print("Saved a clean sleep record. It will wake from it next start.")


if __name__ == "__main__":
    main()
''',
}
HABITAT62_VIEWER = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Habitat 6 - Live View</title>
<style>
  html, body { margin: 0; height: 100%; background: #0d1114; color: #cfe8d8; font: 14px/1.4 monospace; overflow: hidden; }
  #view { position: fixed; inset: 0; width: 100%; height: 100%; display: block; touch-action: none; }
  #panel { position: fixed; top: 8px; left: 8px; max-width: min(340px, calc(100vw - 32px)); background: rgba(8,12,14,.88);
           border: 1px solid #2f5a45; border-radius: 6px; padding: 10px 12px; z-index: 5; }
  #panel h1 { font-size: 15px; margin: 0 0 6px; color: #fff; }
  .row { display: flex; justify-content: space-between; gap: 12px; }
  .k { color: #7da38f; }
  .ok { color: #6fe39a; } .warn { color: #ffcc66; } .bad { color: #ff7b72; }
  #err { margin-top: 6px; color: #ff7b72; min-height: 1em; word-break: break-word; }
  #list { margin-top: 6px; border-top: 1px solid #234; padding-top: 6px; }
  #help { position: fixed; bottom: 8px; left: 8px; color: #7da38f; font-size: 12px; z-index: 5; }
</style>
</head>
<body>
<canvas id="view"></canvas>
<div id="panel">
  <h1>Habitat 6 live view</h1>
  <div class="row"><span class="k">Backend</span><span id="conn" class="warn">connecting...</span></div>
  <div class="row"><span class="k">Hardware source</span><span id="hw">-</span></div>
  <div class="row"><span class="k">Agent mode</span><span id="mode">-</span></div>
  <div class="row"><span class="k">Agent link</span><span id="link">-</span></div>
  <div class="row"><span class="k">State at rest</span><span id="seal">-</span></div>
  <div class="row"><span class="k">Last wake</span><span id="wake">-</span></div>
  <div class="row"><span class="k">Bond</span><span id="bond">-</span></div>
  <div class="row"><span class="k">Tick</span><span id="tick">-</span></div>
  <div class="row"><span class="k">Battery / Temp</span><span id="bat">-</span></div>
  <div class="row"><span class="k">Renderer</span><span id="rend">-</span></div>
  <div id="err"></div>
  <div id="list"></div>
</div>
<div id="help">drag: rotate &middot; wheel/pinch: zoom</div>

<script>
"use strict";
const API = "/api/telemetry";
const POLL_MS = 1000, TIMEOUT_MS = 2000, STALE_AFTER = 3;   // clear scene after 3 failed polls
const $ = id => document.getElementById(id);
const canvas = $("view");
let entities = [];            // only ever filled from a validated backend frame
let failures = 0, latest = null, frozen = false;   // `frozen` is a test hook: stops polling so a screenshot matches the data

function set(id, text, cls) { const el = $(id); el.textContent = text; el.className = cls || ""; }

// ---- validation: never trust the response shape -----------------------
const finite = n => typeof n === "number" && Number.isFinite(n);
function validate(f) {
  if (!f || typeof f !== "object" || f.schema !== "habitat.telemetry/1") throw new Error("unexpected schema");
  if (!Array.isArray(f.entities)) throw new Error("entities missing");
  const out = f.entities.map(e => {
    if (!e || typeof e.id !== "string" || typeof e.type !== "string") throw new Error("bad entity");
    if (!Array.isArray(e.position) || e.position.length !== 3 || !e.position.every(finite)) throw new Error("bad position for " + e.id);
    return { id: e.id, type: e.type, position: e.position, color: Number.isInteger(e.color) ? e.color : 0xffffff };
  });
  if (!f.hardware || typeof f.hardware !== "object" || !f.agent || typeof f.agent !== "object") throw new Error("hardware/agent missing");
  return { frame: f, entities: out };
}

async function poll() {
  if (frozen) return;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(API, { signal: ctl.signal, cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const { frame, entities: ents } = validate(await res.json());
    if (frozen) return;                       // a response that arrives after freeze() is ignored
    failures = 0; latest = frame; entities = ents;
    $("err").textContent = "";
    render(frame);
  } catch (e) {
    failures++;
    const msg = e.name === "AbortError" ? "request timed out" : e.message;
    $("err").textContent = "Telemetry error: " + msg;
    if (failures >= STALE_AFTER) {            // no fake data: remove the scene
      entities = []; latest = null;
      set("conn", "disconnected (" + failures + " failed polls)", "bad");
      set("hw", "-"); set("bond", "-"); set("wake", "-"); set("seal", "-"); set("mode", "-"); set("link", "-"); set("tick", "-"); set("bat", "-");
      $("list").textContent = "";
    } else {
      set("conn", "stale - retrying", "warn");
    }
  } finally { clearTimeout(timer); }
}

function render(f) {
  set("conn", "connected", "ok");
  const hw = f.hardware, ag = f.agent;
  set("hw", f.simulation ? "SIMULATED (no physical device)" : (hw.confirmed ? "hardware (confirmed)" : "hardware (unconfirmed)"),
      f.simulation ? "warn" : (hw.confirmed ? "ok" : "bad"));
  set("mode", String(ag.mode), ag.mode === "normal" ? "ok" : "warn");
  set("link", String(ag.connection), ag.connection === "up" ? "ok" : "bad");
  set("tick", String(f.tick));
  set("seal", ag.sealed === true ? "sealed (encrypted)" : "plaintext", ag.sealed === true ? "ok" : "warn");
  const bd = ag.bond && typeof ag.bond === "object" ? ag.bond : null;
  if (bd) set("bond", `${String(bd.status).slice(0, 12)} | ${String(bd.agent_id).slice(0, 15)} <-> ${String(bd.body_id).slice(0, 15)}`, bd.status === "attached" ? "ok" : "warn");
  else set("bond", "-");
  const wk = ag.wake && typeof ag.wake === "object" ? ag.wake : null;
  if (ag.asleep === true) set("wake", "ASLEEP (ticking paused)", "warn");
  else if (wk) set("wake", `${wk.kind} (${Array.isArray(wk.changes) ? wk.changes.length : 0} changes)`, wk.kind === "unclean" ? "warn" : "");
  else set("wake", "-");
  set("bat", `${Number(hw.battery_pct).toFixed(1)}% / ${Number(hw.temperature_c).toFixed(1)}°C (${hw.status})`,
      hw.status === "nominal" ? "" : "warn");
  const list = $("list"); list.textContent = "";
  if (wk && Array.isArray(wk.changes)) wk.changes.slice(0, 5).forEach(c => {
    const d = document.createElement("div"); d.className = "warn"; d.textContent = "while asleep: " + String(c).slice(0, 120); list.appendChild(d); });
  entities.forEach(e => {
    const d = document.createElement("div");
    d.textContent = `${e.id} [${e.type}] (${e.position.map(v => v.toFixed(1)).join(", ")})`;
    list.appendChild(d);
  });
}

// ---- camera state shared by both renderers -----------------------------
const cam = { yaw: 0.7, pitch: 0.6, dist: 22 };
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
(function controls() {
  let down = false, lx = 0, ly = 0, pinch = 0;
  canvas.addEventListener("pointerdown", e => { down = true; lx = e.clientX; ly = e.clientY; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener("pointerup", () => { down = false; });
  canvas.addEventListener("pointermove", e => {
    if (!down) return;
    cam.yaw -= (e.clientX - lx) * 0.01; cam.pitch = clamp(cam.pitch + (e.clientY - ly) * 0.01, 0.1, 1.45);
    lx = e.clientX; ly = e.clientY;
  });
  canvas.addEventListener("wheel", e => { e.preventDefault(); cam.dist = clamp(cam.dist * (1 + Math.sign(e.deltaY) * 0.1), 6, 90); }, { passive: false });
  canvas.addEventListener("touchmove", e => {
    if (e.touches.length === 2) {
      const d = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
      if (pinch) cam.dist = clamp(cam.dist * (pinch / d), 6, 90);
      pinch = d;
    }
  }, { passive: true });
  canvas.addEventListener("touchend", () => { pinch = 0; });
})();

const hex = c => "#" + (c & 0xffffff).toString(16).padStart(6, "0");

// ---- renderers ----------------------------------------------------------
// Built-in WebGL (no libraries, nothing loaded from the network). If WebGL is
// unavailable the page falls back to the 2D top-down canvas below.
let gl3 = null, ctx2d = null;
const VS = `attribute vec3 p; attribute vec3 n; uniform mat4 mvp; uniform vec3 off; uniform float sc;
varying vec3 vn; void main(){ vn = n; gl_Position = mvp * vec4(p*sc + off, 1.0); }`;
const FS = `precision mediump float; varying vec3 vn; uniform vec3 col; uniform float lit;
void main(){ float d = max(dot(normalize(vn), normalize(vec3(0.4,0.8,0.5))), 0.0);
 gl_FragColor = vec4(col * mix(1.0, 0.45 + 0.55*d, lit), 1.0); }`;

function mat4mul(a, b) { const o = new Float32Array(16);
  for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) { let v = 0; for (let k = 0; k < 4; k++) v += a[k*4+r] * b[c*4+k]; o[c*4+r] = v; } return o; }
function perspective(fovy, aspect, n, f) { const t = 1 / Math.tan(fovy / 2), o = new Float32Array(16);
  o[0] = t / aspect; o[5] = t; o[10] = (f + n) / (n - f); o[11] = -1; o[14] = 2 * f * n / (n - f); return o; }
function lookAt(e, c, u) {
  const sub = (a, b) => [a[0]-b[0], a[1]-b[1], a[2]-b[2]], nrm = v => { const l = Math.hypot(...v); return v.map(x => x / l); };
  const cross = (a, b) => [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]], dot = (a, b) => a[0]*b[0]+a[1]*b[1]+a[2]*b[2];
  const z = nrm(sub(e, c)), x = nrm(cross(u, z)), y = cross(z, x), o = new Float32Array(16);
  o[0]=x[0]; o[4]=x[1]; o[8]=x[2]; o[12]=-dot(x,e); o[1]=y[0]; o[5]=y[1]; o[9]=y[2]; o[13]=-dot(y,e);
  o[2]=z[0]; o[6]=z[1]; o[10]=z[2]; o[14]=-dot(z,e); o[15]=1; return o; }

function meshData(kind) {       // returns {pos, nor, idx}
  if (kind === "energy") {      // octahedron
    const v = [[1,0,0],[-1,0,0],[0,1,0],[0,-1,0],[0,0,1],[0,0,-1]];
    const f = [[0,2,4],[4,2,1],[1,2,5],[5,2,0],[0,4,3],[4,1,3],[1,5,3],[5,0,3]];
    return { pos: v.flat(), nor: v.flat(), idx: f.flat() };
  }
  if (kind === "box") {
    const v = []; for (const x of [-1,1]) for (const y of [-1,1]) for (const z of [-1,1]) v.push([x,y,z]);
    const f = [[0,1,3],[0,3,2],[4,6,7],[4,7,5],[0,4,5],[0,5,1],[2,3,7],[2,7,6],[0,2,6],[0,6,4],[1,5,7],[1,7,3]];
    return { pos: v.flat(), nor: v.map(a => a.map(c => c * 0.577)).flat(), idx: f.flat() };
  }
  const pos = [], idx = [], B = 14, S = 20;       // sphere
  for (let i = 0; i <= B; i++) for (let j = 0; j <= S; j++) { const t = i / B * Math.PI, p = j / S * 2 * Math.PI;
    pos.push(Math.sin(t) * Math.cos(p), Math.cos(t), Math.sin(t) * Math.sin(p)); }
  for (let i = 0; i < B; i++) for (let j = 0; j < S; j++) { const a = i * (S + 1) + j, b = a + S + 1; idx.push(a, b, a + 1, b, b + 1, a + 1); }
  return { pos, nor: pos, idx };
}

function initRenderer() {
  try {
    const gl = canvas.getContext("webgl", { antialias: true, preserveDrawingBuffer: true });
    if (!gl) throw new Error("no webgl");
    const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s)); return s; };
    const prog = gl.createProgram();
    gl.attachShader(prog, sh(gl.VERTEX_SHADER, VS)); gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FS)); gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
    const loc = { p: gl.getAttribLocation(prog, "p"), n: gl.getAttribLocation(prog, "n"), mvp: gl.getUniformLocation(prog, "mvp"),
      off: gl.getUniformLocation(prog, "off"), sc: gl.getUniformLocation(prog, "sc"), col: gl.getUniformLocation(prog, "col"), lit: gl.getUniformLocation(prog, "lit") };
    const buf = (data, target, Arr) => { const b = gl.createBuffer(); gl.bindBuffer(target, b); gl.bufferData(target, new Arr(data), gl.STATIC_DRAW); return b; };
    const meshes = {};
    for (const k of ["proxy", "energy", "box"]) { const m = meshData(k);
      meshes[k] = { pb: buf(m.pos, gl.ARRAY_BUFFER, Float32Array), nb: buf(m.nor, gl.ARRAY_BUFFER, Float32Array),
                    ib: buf(m.idx, gl.ELEMENT_ARRAY_BUFFER, Uint16Array), count: m.idx.length }; }
    const grid = []; for (let i = -20; i <= 20; i += 2) grid.push(i, 0, -20, i, 0, 20, -20, 0, i, 20, 0, i);
    const gridBuf = buf(grid, gl.ARRAY_BUFFER, Float32Array);
    gl3 = { gl, prog, loc, meshes, gridBuf, gridCount: grid.length / 3, mvp: null };
    set("rend", "WebGL (built-in, no external libraries)", "ok");
    return;
  } catch (e) { gl3 = null; $("err").textContent = "WebGL unavailable: " + e.message; }
  ctx2d = canvas.getContext("2d");
  set("rend", "2D fallback (WebGL unavailable)", "warn");
}

function resize() {
  const dpr = window.devicePixelRatio || 1, w = window.innerWidth, h = window.innerHeight;
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  if (!gl3) ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
}
window.addEventListener("resize", resize);

function cameraMatrix() {
  const eye = [Math.sin(cam.yaw) * Math.cos(cam.pitch) * cam.dist, Math.sin(cam.pitch) * cam.dist, Math.cos(cam.yaw) * Math.cos(cam.pitch) * cam.dist];
  const aspect = canvas.width / Math.max(1, canvas.height);
  return mat4mul(perspective(Math.PI / 3, aspect, 0.1, 500), lookAt(eye, [0, 0, 0], [0, 1, 0]));
}

// Screen position (CSS pixels) of a world point with the current camera. Used by the browser test.
function projectToScreen(pos) {
  const m = cameraMatrix(), x = pos[0], y = pos[1], z = pos[2];
  const cx = m[0]*x + m[4]*y + m[8]*z + m[12], cy = m[1]*x + m[5]*y + m[9]*z + m[13], cw = m[3]*x + m[7]*y + m[11]*z + m[15];
  return [(cx / cw * 0.5 + 0.5) * window.innerWidth, (1 - (cy / cw * 0.5 + 0.5)) * window.innerHeight];
}
window.habitatView = { projectToScreen, entities: () => entities.map(e => ({ ...e })), renderer: () => gl3 ? "webgl" : "2d", freeze: v => { frozen = !!v; },
  nextFrame: () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))) };

function draw3d() {
  const { gl, prog, loc, meshes, gridBuf, gridCount } = gl3;
  gl.viewport(0, 0, canvas.width, canvas.height);
  gl.clearColor(0.05, 0.07, 0.08, 1); gl.enable(gl.DEPTH_TEST); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  gl.useProgram(prog);
  gl.uniformMatrix4fv(loc.mvp, false, cameraMatrix());
  gl.enableVertexAttribArray(loc.p); gl.enableVertexAttribArray(loc.n);
  gl.bindBuffer(gl.ARRAY_BUFFER, gridBuf); gl.vertexAttribPointer(loc.p, 3, gl.FLOAT, false, 0, 0);
  gl.vertexAttribPointer(loc.n, 3, gl.FLOAT, false, 0, 0);
  gl.uniform3f(loc.off, 0, 0, 0); gl.uniform1f(loc.sc, 1); gl.uniform3f(loc.col, 0.16, 0.3, 0.24); gl.uniform1f(loc.lit, 0);
  gl.drawArrays(gl.LINES, 0, gridCount);
  gl.uniform1f(loc.lit, 1);
  for (const e of entities) {
    const m = meshes[e.type === "proxy" ? "proxy" : e.type === "energy" ? "energy" : "box"];
    gl.bindBuffer(gl.ARRAY_BUFFER, m.pb); gl.vertexAttribPointer(loc.p, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, m.nb); gl.vertexAttribPointer(loc.n, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, m.ib);
    gl.uniform3f(loc.off, e.position[0], e.position[1], e.position[2]); gl.uniform1f(loc.sc, 0.6);
    gl.uniform3f(loc.col, ((e.color >> 16) & 255) / 255, ((e.color >> 8) & 255) / 255, (e.color & 255) / 255);
    gl.drawElements(gl.TRIANGLES, m.count, gl.UNSIGNED_SHORT, 0);
  }
}

function draw2d() {
  const w = window.innerWidth, h = window.innerHeight, g = ctx2d;
  g.fillStyle = "#0d1114"; g.fillRect(0, 0, w, h);
  const scale = Math.min(w, h) / (cam.dist * 1.6), cx = w / 2, cy = h / 2;
  g.strokeStyle = "#1a2a24"; g.lineWidth = 1;
  for (let i = -20; i <= 20; i += 2) {
    g.beginPath(); g.moveTo(cx + i * scale, cy - 20 * scale); g.lineTo(cx + i * scale, cy + 20 * scale); g.stroke();
    g.beginPath(); g.moveTo(cx - 20 * scale, cy + i * scale); g.lineTo(cx + 20 * scale, cy + i * scale); g.stroke();
  }
  const cs = Math.cos(cam.yaw), sn = Math.sin(cam.yaw);
  for (const e of entities) {
    const x = e.position[0] * cs - e.position[2] * sn, z = e.position[0] * sn + e.position[2] * cs;
    const px = cx + x * scale, py = cy + z * scale, r = Math.max(5, scale * 0.6);
    g.fillStyle = hex(e.color); g.beginPath();
    if (e.type === "energy") { g.moveTo(px, py - r); g.lineTo(px + r, py); g.lineTo(px, py + r); g.lineTo(px - r, py); g.closePath(); }
    else g.arc(px, py, r, 0, 6.2832);
    g.fill(); g.fillStyle = "#cfe8d8"; g.fillText(e.id, px + r + 3, py + 4);
  }
  if (!entities.length) { g.fillStyle = "#7da38f"; g.fillText("No live telemetry - nothing to draw.", cx - 100, cy); }
}

function frame() {
  (gl3 ? draw3d : draw2d)();
  requestAnimationFrame(frame);
}

initRenderer(); resize(); poll(); setInterval(poll, POLL_MS); requestAnimationFrame(frame);
</script>
</body>
</html>
'''


def load_habitat62():
    """Load the supplied modules from this file; no source files are created."""
    import linecache
    import types

    if getattr(sys.modules.get('habitat62_run'), '_hearth_embedded', False):
        return sys.modules['habitat62_run']
    for name in ('world', 'guest', *HABITAT62_SOURCES):
        if name in sys.modules:
            raise RuntimeError(f'Cannot load embedded Habitat: {name} is already imported')
    for package in ('world', 'guest'):
        mod = types.ModuleType(package)
        mod.__path__ = []
        sys.modules[package] = mod
    made = ['world', 'guest']
    try:
        for name, source in HABITAT62_SOURCES.items():
            filename = f'<hearth_combined:{name}>'
            module = types.ModuleType(name)
            module.__file__ = filename
            module.__package__ = name.rpartition('.')[0]
            sys.modules[name] = module
            made.append(name)
            linecache.cache[filename] = (len(source), None, source.splitlines(keepends=True), filename)
            exec(compile(source, filename, 'exec'), module.__dict__)
            if '.' in name:
                parent, _, child = name.rpartition('.')
                setattr(sys.modules[parent], child, module)
            if name == 'server':
                module._EMBEDDED_VIEWER = HABITAT62_VIEWER
            elif name == 'habitat62_run':
                module._hearth_embedded = True
        return sys.modules['habitat62_run']
    except BaseException:
        for name in reversed(made):
            sys.modules.pop(name, None)
        raise


def run_habitat62():
    """Start Habitat 6.2, optionally using the supplied asynchronous Ollama adapter."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--llm', choices=['ollama'])
    parser.add_argument('--llm-model')
    parser.add_argument('--llm-url', default='http://127.0.0.1:11434')
    parser.add_argument('--llm-interval', type=int, default=6)
    parser.add_argument('--llm-timeout', type=float, default=8.0)
    options, remaining = parser.parse_known_args(sys.argv[1:])
    if '-h' in remaining or '--help' in remaining:
        print('Optional local model: --llm ollama --llm-model NAME '
              '[--llm-url URL] [--llm-interval TICKS] [--llm-timeout SECONDS]')
    if options.llm and not options.llm_model:
        parser.error('--llm requires --llm-model')
    if not options.llm and options.llm_model:
        parser.error('--llm-model requires --llm ollama')
    if options.llm_interval < 1 or options.llm_timeout <= 0:
        parser.error('--llm-interval and --llm-timeout must be positive')
    runner = load_habitat62()
    if options.llm:
        try:
            client = sys.modules['guest.llm_clients'].OllamaClient(options.llm_model, options.llm_url)
        except ValueError as exc:
            parser.error(str(exc))
        original = sys.modules['server'].Simulation._make_controller
        adapter = sys.modules['guest.llm_adapter'].AsyncLLMAdapter
        def make_controller(sim):
            original(sim)
            sim.controller.primary = adapter(client, interval_ticks=options.llm_interval,
                                             timeout=options.llm_timeout)
        sys.modules['server'].Simulation._make_controller = make_controller
    sys.argv = [sys.argv[0]] + remaining
    runner.main()


if __name__ == '__main__':
    if '--habitat62' in sys.argv[1:]:
        sys.argv.remove('--habitat62')
        run_habitat62()
    elif '--world-edit' in sys.argv[1:]:
        sys.argv.remove('--world-edit')
        load_habitat62()
        sys.exit(sys.modules['world_edit'].main())
    else:
        main()
