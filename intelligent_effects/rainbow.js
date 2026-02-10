// Intelligent Effect: Rainbow
(function () {
  if (typeof window.registerIntelligentEffect !== "function") return;

  window.registerIntelligentEffect({
    id: "rainbow",
    label: "Rainbow",
    targets: ["color", "dimmer"],
    mode: "absolute",
    params: [
      { key: "speed", label: "Speed (Hz)", type: "range", min: 0, max: 3, step: 0.05, default: 0.2 },
      { key: "spread", label: "Spread (deg/device)", type: "range", min: 0, max: 60, step: 1, default: 10 },
      { key: "saturation", label: "Saturation", type: "range", min: 0, max: 1, step: 0.05, default: 1 },
      { key: "intensity", label: "Intensity", type: "range", min: 0, max: 255, step: 1, default: 255 }
    ],
    apply: (ctx) => {
      const speed = Number(ctx.params.speed || 0);
      const spread = Number(ctx.params.spread || 0);
      const sat = ctx.helpers.clamp(Number(ctx.params.saturation || 0), 0, 1);
      const intensity = ctx.helpers.clamp(Number(ctx.params.intensity || 0), 0, 255);

      const hue = ((ctx.tMs * speed * 360) / 1000 + ctx.deviceIndex * spread) % 360;
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
