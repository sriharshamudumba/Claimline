// Run: node tests/fillers.test.js
const assert = require("assert");
const FillerController = require("../static/fillers.js");

function harness(extra) {
  let t = 0;
  let timers = [];
  const played = [];
  const latencies = [];
  const log = { stopped: 0, faded: 0 };
  const fc = FillerController(Object.assign({
    now: () => t,
    setTimeout: (fn, ms) => { const h = { at: t + ms, fn }; timers.push(h); return h; },
    clearTimeout: (h) => { timers = timers.filter((x) => x !== h); },
    playClip: (kind) => { played.push(kind); return { stop: () => log.stopped++, fade: () => log.faded++ }; },
    onLatency: (ms) => latencies.push(ms),
  }, extra || {}));
  function advance(ms) {
    const end = t + ms;
    for (;;) {
      timers.sort((a, b) => a.at - b.at);
      const next = timers[0];
      if (!next || next.at > end) break;
      timers.shift();
      t = next.at;
      next.fn();
    }
    t = end;
  }
  return { fc, advance, played, latencies, log };
}

const tests = {
  "fast reply: no filler, latency recorded"() {
    const h = harness();
    h.fc.userStoppedSpeaking(); h.advance(300); h.fc.agentAudioStarted();
    assert.deepStrictEqual(h.played, []);
    assert.deepStrictEqual(h.latencies, [300]);
  },
  "slow reply: one filler at 500 ms, faded when agent speaks"() {
    const h = harness();
    h.fc.userStoppedSpeaking(); h.advance(499);
    assert.deepStrictEqual(h.played, []);
    h.advance(1);
    assert.deepStrictEqual(h.played, ["ack"]);
    h.advance(900); h.fc.agentAudioStarted();
    assert.strictEqual(h.log.faded, 1);
    assert.deepStrictEqual(h.latencies, [1400]);
  },
  "never two fillers in one turn, even with a tool round trip"() {
    const h = harness();
    h.fc.userStoppedSpeaking(); h.advance(600);
    h.fc.toolRoundTrip("lookup"); h.advance(2000);
    assert.deepStrictEqual(h.played, ["ack"]);
  },
  "tool round trip uses the lookup filler and keeps the original deadline"() {
    const h = harness();
    h.fc.userStoppedSpeaking(); h.advance(300);
    h.fc.toolRoundTrip("lookup"); h.advance(199);
    assert.deepStrictEqual(h.played, []);
    h.advance(1);
    assert.deepStrictEqual(h.played, ["lookup"]);
  },
  "back-to-back slow turns: second needs 1.5 s and the cooldown"() {
    const h = harness({ cooldownMs: 0 });
    h.fc.userStoppedSpeaking(); h.advance(600); h.fc.agentAudioStarted();
    h.advance(3000);
    h.fc.userStoppedSpeaking(); h.advance(1000);
    assert.deepStrictEqual(h.played, ["ack"], "no filler at 1 s on a consecutive turn");
    h.advance(500);
    assert.deepStrictEqual(h.played, ["ack", "ack"]);
  },
  "cooldown blocks a filler that would come too soon"() {
    const h = harness();
    h.fc.userStoppedSpeaking(); h.advance(600); h.fc.agentAudioStarted();
    h.advance(1000);
    h.fc.userStoppedSpeaking(); h.advance(2000);
    assert.deepStrictEqual(h.played, ["ack"]);
  },
  "a quiet turn resets the consecutive rule"() {
    const h = harness({ cooldownMs: 0 });
    h.fc.userStoppedSpeaking(); h.advance(600); h.fc.agentAudioStarted();
    h.fc.userStoppedSpeaking(); h.advance(200); h.fc.agentAudioStarted();
    h.fc.userStoppedSpeaking(); h.advance(500);
    assert.deepStrictEqual(h.played, ["ack", "ack"]);
  },
  "barge-in stops the filler immediately and cancels pending ones"() {
    const h = harness();
    h.fc.userStoppedSpeaking(); h.advance(600);
    h.fc.userStartedSpeaking();
    assert.strictEqual(h.log.stopped, 1);
    h.fc.userStoppedSpeaking(); h.advance(100); h.fc.userStartedSpeaking(); h.advance(5000);
    assert.deepStrictEqual(h.played, ["ack"]);
  },
  "threshold 0 disables fillers"() {
    const h = harness({ thresholdMs: 0 });
    h.fc.userStoppedSpeaking(); h.advance(5000);
    assert.deepStrictEqual(h.played, []);
  },
  "no clips loaded: nothing breaks"() {
    const h = harness({ playClip: () => null });
    h.fc.userStoppedSpeaking(); h.advance(5000); h.fc.agentAudioStarted();
    assert.deepStrictEqual(h.latencies, [5000]);
  },
};

let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { fn(); console.log("  ok  ", name); } catch (e) { failed++; console.log("  FAIL", name, "\n", e.message); }
}
console.log(failed ? `${failed} failed` : `${Object.keys(tests).length} passed`);
process.exit(failed ? 1 : 0);
