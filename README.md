# Hearth + Habitat bridge (experiment, r2)

An experimental wrapper that puts Habitat-style safety checks in front of the one decision
Hearth lets its proxy being "Claude" make. It sits **beside** the existing files and changes
none of them:

- `hearth_combined.py` is the earlier combined build, byte-for-byte unchanged
  (SHA-256 `39c5387dffca346f2d0849402505d468d6d2454c7dc0a3707e0080ea449a1765`).
- The audited Habitat 6.2 package is not touched or reissued.

## How it works

Hearth already asks a "client" for a JSON choice (`go`, `talk`, `propose_child`, `use_bulb`,
`stay`) and applies it. The bridge supplies a stand-in client. Every proposal, from whatever
source, must pass strict checks before Hearth ever sees it:

1. Exactly the keys `action`, `target`, `reason`; plain JSON only; no duplicate keys.
2. The action must be on the allowed list. Default: `go`, `talk`, `stay`.
   `propose_child` and `use_bulb` stay off unless you pass `--allow-births` / `--allow-portal`.
3. The target must be real and right here (a known place; a living being in the same place).
4. Text is bounded and has no control characters.

If a proposal is refused, or the model does not answer, a deterministic fallback picks a safe
move (go where the lowest need is restored, or stay). After 3 failures in a row the fallback takes
over completely; the model is trusted again only after 2 good proposals in a row. Every decision is
logged (`--log-json PATH`).

Proposal sources:
- `--proposer scripted` (default): offline, deterministic. `--scripted-noise N` sends a bad reply
  every Nth time so you can watch refusals and the fallback.
- `--proposer ollama --llm-model NAME`: an Ollama-style server **on this computer only**
  (127.0.0.1, localhost or ::1; no redirects, no proxies). Asked in the background so a slow model
  never stalls the world.

No cloud model is used and `ANTHROPIC_API_KEY` is ignored.

## Run it

Open a terminal **in this folder** (on Windows: open the folder in File Explorer, click the address
bar, type `cmd`, press Enter), then:

    python hearth_bridge.py                       # viewer at http://127.0.0.1:8765/
    python hearth_bridge.py --ticks 200 --fresh --state demo.json --log-json log.jsonl
    python hearth_bridge.py --ticks 200 --fresh --scripted-noise 2 --state demo.json
    python hearth_bridge.py --proposer ollama --llm-model YOURMODEL      # needs Ollama running locally

With a real model in a headless `--ticks` run, add `--pace 0.05` (seconds between ticks) so the model has
time to answer; without it the run finishes before any answer arrives and the fallback does the steering.

Ctrl+C stops the viewer, saves, prints a short report and writes the log. Use `--state` to choose
a folder you own. A different `hearth_combined.py` is refused unless you pass `--allow-unpinned`
(review that file first; the newer model-birth build `43ccbd37...6ba5d` has not been reviewed).

## Tests

    python -m unittest discover -s tests -v

41 tests, standard library only (about 25 seconds). In the build this README ships with, a
deliberate-breakage check applied 23 separate sabotages to `hearth_bridge.py` (removing each check,
loosening each limit, reversing the fallback rules, following redirects, and so on) and the tests
caught all 23.

r2 change: the three end-to-end tests that use a stand-in local model server now pass `--pace 0.05`, so they
no longer depend on how fast the machine runs (two of them failed on one machine without it). The bridge code
is identical to r1.

## What is and is not verified

Verified here (Python 3.13 on Linux only): the tests above; the viewer starts and serves; Ctrl+C
saves, reports and writes the log; the Ollama client and background asking against a stand-in
local server (good replies, bad replies, errors, timeouts, redirects, oversized replies).

**Not verified:** any real language model; Windows; other Python versions; the model-birth build;
how a real model's replies hold up (strict JSON means real models may be refused often at first,
and then the fallback runs the world, which is by design).

## Known limits

- It copies Habitat's pattern; it does not use Habitat's real bond, sleep or lifeboat modules, so
  Claude's identity here is Hearth's own, not a Habitat bond.
- The viewer does not display bridge status. Use the report or the log.
- Only the proxy's choices are checked. Hearth's other beings and world rules are unchanged.
- Hearth's own save format is used; the bridge adds no save data (the decision log is separate).

## For anyone reviewing or extending this (including another AI tool)

Please keep the checks in front of Hearth. Do not edit `hearth_combined.py` in place, loosen the
allowed-action list, or let proposals reach `apply()` without `validate_proposal`. If you change
the code, rerun the tests and say what you changed and what you could not test.
