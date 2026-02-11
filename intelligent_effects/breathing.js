// Intelligent Effect: Breathing (single or alternating color)
(function () {
  if (typeof window.registerIntelligentEffect !== "function") return;

  window.registerIntelligentEffect({
    id: "breathing",
    label: "Breathing",
    targets: ["color", "dimmer"],
    mode: "absolute",
    params: [
      { key: "phase", label: "Phase (ms)", type: "text", default: "0", hint: "0, 0>500, 0<500, 0><500, 0<>500, 0|500, 0||500, 0?500" },
      { key: "speed", label: "Speed (Hz)", type: "range", min: 0, max: 3, step: 0.05, default: 0.3 },

      // ✅ UI-friendly selects: options are STRINGS, not objects
      {
        key: "colorMode",
        label: "Color Mode",
        type: "select",
        default: "Single color",
        options: ["Single color", "Alternating colors"]
      },
      {
        key: "alternateMode",
        label: "Alternate On",
        type: "select",
        default: "Each full cycle",
        options: ["Each full cycle", "Each half-cycle"],
        hint: "full cycle = change every breathing cycle, half = change on inhale/exhale"
      },

      { key: "hue", label: "Hue (Color A)", type: "range", min: 0, max: 360, step: 1, default: 200 },
      { key: "hue2", label: "Hue (Color B)", type: "range", min: 0, max: 360, step: 1, default: 20 },

      { key: "saturation", label: "Saturation", type: "range", min: 0, max: 1, step: 0.05, default: 1 },
      { key: "min", label: "Min", type: "range", min: 0, max: 255, step: 1, default: 10 },
      { key: "max", label: "Max", type: "range", min: 0, max: 255, step: 1, default: 255 }
    ],
    apply: (ctx) => {
      const phaseMs = ctx.helpers.phaseOffsetMs(ctx);
      const tMs = ctx.tMs + phaseMs;

      const speed = Number(ctx.params.speed || 0);

      const colorModeRaw = String(ctx.params.colorMode || "Single color");
      const colorMode = (colorModeRaw.toLowerCase().includes("altern")) ? "alternate" : "single";

      const alternateModeRaw = String(ctx.params.alternateMode || "Each full cycle");
      const alternateMode = (alternateModeRaw.toLowerCase().includes("half")) ? "half" : "cycle";

      const hueA = Number(ctx.params.hue || 0);
      const hueB = Number(ctx.params.hue2 || 0);

      const sat = ctx.helpers.clamp(Number(ctx.params.saturation || 0), 0, 1);
      const minVal = ctx.helpers.clamp(Number(ctx.params.min || 0), 0, 255);
      const maxVal = ctx.helpers.clamp(Number(ctx.params.max || 255), 0, 255);
      const span = Math.max(0, maxVal - minVal);

      // Breathing waveform (sin) => [-1..1]
      const w = ctx.helpers.wave("sinus", tMs, speed);
      const level = (w * 0.5 + 0.5); // [0..1]
      const intensity = minVal + span * level;
      const val = intensity / 255;

      // --- Color selection (with proper phase alignment + smooth fades) ---

      // Helpers
      const clamp01 = (x) => Math.max(0, Math.min(1, x));
      const smoothstep = (a, b, x) => {
        const t = clamp01((x - a) / (b - a));
        return t * t * (3 - 2 * t);
      };
      const lerpHue = (a, b, t) => {
        // shortest path around the 0/360 wrap
        const d = ((b - a + 540) % 360) - 180;
        return (a + d * t + 360) % 360;
      };

      let hue = hueA;

      if (colorMode === "alternate" && speed > 0) {
        const periodMs = 1000 / speed;

        // Align phase so that phase=0 is the MINIMUM of the sinus breathing:
        // sin() minimum is at 3/4 cycle -> shift by +1/4 so minimum becomes 0.
        const u = (tMs / periodMs) + 0.25;
        const cycleIdx = Math.floor(u);
        const p = u - cycleIdx; // [0..1)

        // Fade window in ms (depends on speed via period). Capped to avoid eating the whole cycle.
        const fadeMs = 200; // tweak if you want (e.g. 100..300)
        const fade = Math.min(0.25, fadeMs / periodMs); // fraction of cycle

        if (alternateMode === "cycle") {
          // Alternate per full cycle, switch at p=0 (minimum), with fade around p=0
          const cur = (cycleIdx % 2 === 0) ? hueA : hueB;
          const prev = ((cycleIdx - 1) % 2 === 0) ? hueA : hueB;

          if (p < fade) {
            const t = smoothstep(0, fade, p);
            hue = lerpHue(prev, cur, t);
          } else {
            hue = cur;
          }
        } else {
          // Half-cycle: 0-50% A, 50-100% B, with smooth crossfade around 0.5
          const mid = 0.5;
          const aStart = Math.max(0, mid - fade);
          const aEnd   = Math.min(1, mid + fade);

          if (p < aStart) {
            hue = hueA;
          } else if (p > aEnd) {
            hue = hueB;
          } else {
            const t = smoothstep(aStart, aEnd, p);
            hue = lerpHue(hueA, hueB, t);
          }
        }
      }

      const rgb = ctx.helpers.hueToRgb(hue, sat, val);

      if (ctx.target === "r") return rgb.r;
      if (ctx.target === "g") return rgb.g;
      if (ctx.target === "b") return rgb.b;
      if (ctx.target === "dimmer") return Math.round(intensity);
      return 0;
    }

  });
})();
