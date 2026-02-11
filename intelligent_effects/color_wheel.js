// Intelligent Effect: Color Wheel
(function () {
  if (typeof window.registerIntelligentEffect !== "function") return;

  window.registerIntelligentEffect({
    id: "color_wheel",
    label: "Color Wheel",
    targets: ["color", "dimmer"],
    mode: "absolute",
    params: [
      { key: "phase", label: "Phase (ms)", type: "text", default: "0", hint: "0, 0>500, 0<500, 0><500, 0<>500, 0|500, 0||500, 0?500" },
      { key: "speed", label: "Speed (steps/s)", type: "range", min: 0, max: 5, step: 0.05, default: 0.6 },
      { key: "steps", label: "Steps", type: "number", min: 2, max: 24, default: 8 },
      { key: "spread", label: "Spread (deg/device)", type: "range", min: 0, max: 60, step: 1, default: 0 },
      { key: "saturation", label: "Saturation", type: "range", min: 0, max: 1, step: 0.05, default: 1 },
      { key: "intensity", label: "Intensity", type: "range", min: 0, max: 255, step: 1, default: 255 }
    ],
    apply: (ctx) => {
      const phase = ctx.helpers.phaseOffsetMs(ctx);
      const tMs = ctx.tMs + phase;
      const speed = Number(ctx.params.speed || 0);
      const steps = Math.max(2, Math.floor(Number(ctx.params.steps || 8)));
      const spread = Number(ctx.params.spread || 0);
      const sat = ctx.helpers.clamp(Number(ctx.params.saturation || 0), 0, 1);
      const intensity = ctx.helpers.clamp(Number(ctx.params.intensity || 0), 0, 255);

      const t = (tMs / 1000) * speed;
      const stepIndex = Math.floor(t) % steps;
      const hueBase = (stepIndex * (360 / steps)) % 360;
      const hue = (hueBase + ctx.deviceIndex * spread) % 360;
      const val = intensity / 255;
      const rgb = ctx.helpers.hsvToRgb(hue, sat, val);

      if (ctx.target === "r") return rgb.r;
      if (ctx.target === "g") return rgb.g;
      if (ctx.target === "b") return rgb.b;
      if (ctx.target === "dimmer") return Math.round(intensity);
      return 0;
    }
  });
})();
