// Intelligent Effect: Sparkle
(function () {
  if (typeof window.registerIntelligentEffect !== "function") return;

  function rand01(seed) {
    const x = Math.sin(seed) * 43758.5453123;
    return x - Math.floor(x);
  }

  window.registerIntelligentEffect({
    id: "sparkle",
    label: "Sparkle",
    targets: ["color", "dimmer"],
    mode: "absolute",
    params: [
      { key: "phase", label: "Phase (ms)", type: "text", default: "0", hint: "0, 0>500, 0<500, 0><500, 0<>500, 0|500, 0||500, 0?500" },
      { key: "density", label: "Density", type: "range", min: 0, max: 1, step: 0.05, default: 0.25 },
      { key: "speed", label: "Speed", type: "range", min: 0.1, max: 10, step: 0.1, default: 3 },
      { key: "hue", label: "Hue", type: "range", min: 0, max: 360, step: 1, default: 50 },
      { key: "saturation", label: "Saturation", type: "range", min: 0, max: 1, step: 0.05, default: 1 },
      { key: "intensity", label: "Intensity", type: "range", min: 0, max: 255, step: 1, default: 255 }
    ],
    apply: (ctx) => {
      const phase = ctx.helpers.phaseOffsetMs(ctx);
      const tMs = ctx.tMs + phase;
      const density = ctx.helpers.clamp(Number(ctx.params.density || 0), 0, 1);
      const speed = Number(ctx.params.speed || 1);
      const hue = Number(ctx.params.hue || 0);
      const sat = ctx.helpers.clamp(Number(ctx.params.saturation || 0), 0, 1);
      const intensity = ctx.helpers.clamp(Number(ctx.params.intensity || 0), 0, 255);

      const tick = Math.floor((tMs / 1000) * speed);
      const r = rand01((ctx.deviceIndex + 1) * 13.37 + tick * 7.13);
      const on = r < density ? 1 : 0;
      const val = (intensity / 255) * on;
      const rgb = ctx.helpers.hueToRgb(hue, sat, val);

      if (ctx.target === "r") return rgb.r;
      if (ctx.target === "g") return rgb.g;
      if (ctx.target === "b") return rgb.b;
      if (ctx.target === "dimmer") return Math.round(intensity * on);
      return 0;
    }
  });
})();
