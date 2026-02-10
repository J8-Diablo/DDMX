// Intelligent Effect: Breathing
(function () {
  if (typeof window.registerIntelligentEffect !== "function") return;

  window.registerIntelligentEffect({
    id: "breathing",
    label: "Breathing",
    targets: ["color", "dimmer"],
    mode: "absolute",
    params: [
      { key: "speed", label: "Speed (Hz)", type: "range", min: 0, max: 3, step: 0.05, default: 0.3 },
      { key: "hue", label: "Hue", type: "range", min: 0, max: 360, step: 1, default: 200 },
      { key: "saturation", label: "Saturation", type: "range", min: 0, max: 1, step: 0.05, default: 1 },
      { key: "min", label: "Min", type: "range", min: 0, max: 255, step: 1, default: 10 },
      { key: "max", label: "Max", type: "range", min: 0, max: 255, step: 1, default: 255 }
    ],
    apply: (ctx) => {
      const speed = Number(ctx.params.speed || 0);
      const hue = Number(ctx.params.hue || 0);
      const sat = ctx.helpers.clamp(Number(ctx.params.saturation || 0), 0, 1);
      const minVal = ctx.helpers.clamp(Number(ctx.params.min || 0), 0, 255);
      const maxVal = ctx.helpers.clamp(Number(ctx.params.max || 255), 0, 255);
      const span = Math.max(0, maxVal - minVal);

      const w = ctx.helpers.wave("sinus", ctx.tMs, speed);
      const level = (w * 0.5 + 0.5);
      const intensity = minVal + span * level;
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
