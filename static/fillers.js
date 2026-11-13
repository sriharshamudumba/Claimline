// Latency-gated conversational fillers.
//
// Plays a short pre-recorded "mm-hmm" / "okay" / "let me pull that up" only when the
// agent's first audio is late (default > 500 ms after the caller stops talking).
// Rules that keep it from getting irritating:
//   - at most one filler per caller turn
//   - a cooldown between fillers (default 6 s)
//   - if the previous turn already had a filler, this turn must wait longer (default 1.5 s)
//   - barge-in stops the filler at once; agent audio fades it out
// Pure logic with injected clock/timers/player so it can be unit-tested in Node.

(function (root) {
  function FillerController(opts) {
    const o = Object.assign(
      {
        thresholdMs: 500,
        cooldownMs: 6000,
        consecutiveMs: 1500,
        now: () => Date.now(),
        setTimeout: (fn, ms) => setTimeout(fn, ms),
        clearTimeout: (t) => clearTimeout(t),
        playClip: () => null, // (kind) -> { stop(), fade() } | null
        onFiller: () => {},   // ({ kind, waitedMs })
        onLatency: () => {},  // (ms) time from caller stop to first agent audio
      },
      opts || {}
    );
    let turn = 0;
    let firedTurn = -1;
    let lastFillerTurn = -10;
    let lastFillerAt = -Infinity;
    let turnStart = null;
    let timer = null;
    let current = null;

    function clear() {
      if (timer !== null) { o.clearTimeout(timer); timer = null; }
    }

    function schedule(kind) {
      clear();
      if (!o.thresholdMs || o.thresholdMs <= 0) return;
      if (firedTurn === turn) return;
      const base = lastFillerTurn === turn - 1 ? Math.max(o.consecutiveMs, o.thresholdMs) : o.thresholdMs;
      const elapsed = turnStart === null ? 0 : o.now() - turnStart;
      const delay = Math.max(0, base - elapsed);
      timer = o.setTimeout(() => fire(kind), delay);
    }

    function fire(kind) {
      timer = null;
      if (firedTurn === turn) return;
      if (o.now() - lastFillerAt < o.cooldownMs) return;
      const clip = o.playClip(kind);
      if (!clip) return;
      current = clip;
      firedTurn = turn;
      lastFillerTurn = turn;
      lastFillerAt = o.now();
      o.onFiller({ kind, waitedMs: turnStart === null ? 0 : o.now() - turnStart });
    }

    return {
      // Server VAD says the caller finished a turn.
      userStoppedSpeaking() {
        turn += 1;
        turnStart = o.now();
        schedule("ack");
      },
      // Caller started talking (barge-in): never talk over them.
      userStartedSpeaking() {
        clear();
        turnStart = null;
        if (current) { current.stop(); current = null; }
      },
      // A tool round trip is about to delay the reply (e.g. policy lookup).
      toolRoundTrip(kind) {
        if (turnStart === null) turnStart = o.now();
        schedule(kind || "ack");
      },
      // First agent audio (or transcript) for this reply arrived.
      agentAudioStarted() {
        clear();
        if (turnStart !== null) { o.onLatency(o.now() - turnStart); turnStart = null; }
        if (current) { current.fade(); current = null; }
      },
      reset() {
        clear();
        if (current) { current.stop(); current = null; }
        turn = 0; firedTurn = -1; lastFillerTurn = -10; lastFillerAt = -Infinity; turnStart = null;
      },
      get stats() { return { turn, firedTurn, lastFillerTurn }; },
    };
  }

  root.FillerController = FillerController;
  if (typeof module !== "undefined" && module.exports) module.exports = FillerController;
})(typeof window !== "undefined" ? window : globalThis);
