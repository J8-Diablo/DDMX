// Intelligent Effect: Strobe
(function () {
  if (typeof window.registerIntelligentEffect !== "function") return;

  window.registerIntelligentEffect({
    id: "strobe",
    label: "Strobe",
    targets: ["color", "dimmer"],
    mode: "absolute",
    params: [
      { key: "speed", label: "Speed (Hz)", type: "range", min: 0, max: 15, step: 0.1, default: 8 },
      { key: "duty", label: "Duty", type: "range", min: 0.05, max: 0.95, step: 0.05, default: 0.2 },
      { key: "hue", label: "Hue", type: "range", min: 0, max: 360, step: 1, default: 0 },
      { key: "saturation", label: "Saturation", type: "range", min: 0, max: 1, step: 0.05, default: 1 },
      { key: "intensity", label: "Intensity", type: "range", min: 0, max: 255, step: 1, default: 255 }
    ],
    apply: (ctx) => {
      const speed = Number(ctx.params.speed || 0);
      const duty = ctx.helpers.clamp(Number(ctx.params.duty || 0.2), 0.05, 0.95);
      const hue = Number(ctx.params.hue || 0);
      const sat = ctx.helpers.clamp(Number(ctx.params.saturation || 0), 0, 1);
      const intensity = ctx.helpers.clamp(Number(ctx.params.intensity || 0), 0, 255);

      const w = ctx.helpers.wave("rectangle", ctx.tMs, speed);
      const on = w > (1 - 2 * duty) ? 1 : 0;
      const val = (intensity / 255) * on;
      const rgb = ctx.helpers.hsvToRgb(hue, sat, val);

      if (ctx.target === "r") return rgb.r;
      if (ctx.target === "g") return rgb.g;
      if (ctx.target === "b") return rgb.b;
      if (ctx.target === "dimmer") return Math.round(intensity * on);
      return 0;
    }
  });
})();
