// Intelligent Effect: Chaser
// This file is loaded by the UI and registers itself via window.registerIntelligentEffect.

(function () {
  if (typeof window.registerIntelligentEffect !== "function") {
    return;
  }

  window.registerIntelligentEffect({
    id: "chaser",
    label: "Chaser",
    targets: ["dimmer"],
    mode: "absolute",
    params: [
      { key: "amplitude", label: "Amplitude", type: "number", min: 0, max: 255, default: 255 },
      { key: "duration", label: "Duration (ms)", type: "number", min: 0, max: 60000, default: 200 },
      { key: "fade", label: "Fade (ms)", type: "number", min: 0, max: 10000, default: 100 },
      { key: "fadeCurve", label: "Fade Curve", type: "select", options: ["Linear", "EaseIn", "EaseOut", "EaseInOut", "Snap", "Smooth"], default: "Linear" },
      { key: "breakStep", label: "Break Step", type: "number", min: 0, max: 100, default: 0 },
      { key: "breakSize", label: "Break Size (ms)", type: "number", min: 0, max: 5000, default: 500 },
      { key: "size", label: "Size", type: "number", min: 1, max: 100, default: 1 },
      { key: "stepSize", label: "Step Size", type: "number", min: 1, max: 100, default: 1 },
      { key: "playMode", label: "Play Mode", type: "select", options: ["Normal", "Reverse", "Bounce", "In", "Out", "InOut", "Random", "Switch"], default: "Normal" }
    ],
    apply: (ctx) => {
      const amp = Math.max(0, Math.min(255, Number(ctx.params.amplitude ?? 255)));
      const on = ctx.helpers.chaserEdgeFade(ctx, ctx.params);
      return Math.round(amp * on);
    },
    preview: (ctx) => {
      const on = ctx.helpers.chaserEdgeFade(ctx, ctx.params);
      return Math.round(255 * on);
    }
  });
})();
