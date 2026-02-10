// Intelligent Effect: Visor
(function () {
  if (typeof window.registerIntelligentEffect !== "function") return;

  window.registerIntelligentEffect({
    id: "visor",
    label: "Visor",
    targets: ["color", "dimmer"],
    mode: "absolute",
    params: [
      { key: "speed", label: "Speed (cycles/s)", type: "range", min: 0, max: 3, step: 0.05, default: 0.6 },
      { key: "width", label: "Width (devices)", type: "range", min: 1, max: 20, step: 1, default: 5 },
      { key: "softness", label: "Softness", type: "range", min: 0, max: 1, step: 0.05, default: 0.4 },
      { key: "hue", label: "Hue", type: "range", min: 0, max: 360, step: 1, default: 200 },
      { key: "saturation", label: "Saturation", type: "range", min: 0, max: 1, step: 0.05, default: 1 },
      { key: "intensity", label: "Intensity", type: "range", min: 0, max: 255, step: 1, default: 255 }
    ],
    apply: (ctx) => {
      const count = Math.max(1, ctx.deviceCount || 1);
      const speed = Number(ctx.params.speed || 0);
      const width = Math.max(1, Number(ctx.params.width || 1));
      const softness = ctx.helpers.clamp(Number(ctx.params.softness || 0), 0, 1);
      const hue = Number(ctx.params.hue || 0);
      const sat = ctx.helpers.clamp(Number(ctx.params.saturation || 0), 0, 1);
      const intensity = ctx.helpers.clamp(Number(ctx.params.intensity || 0), 0, 255);

      const head = (ctx.tMs / 1000) * speed * count;
      const pos = head % count;
      const half = width / 2;
      const dist = Math.abs(((ctx.deviceIndex - pos + count / 2) % count) - count / 2);
      let level = 0;
      if (dist <= half) {
        const t = 1 - dist / Math.max(1e-6, half);
        level = softness > 0 ? Math.pow(t, 1 + softness * 4) : t;
      }

      const val = (intensity / 255) * level;
      const rgb = ctx.helpers.hsvToRgb(hue, sat, val);

      if (ctx.target === "r") return rgb.r;
      if (ctx.target === "g") return rgb.g;
      if (ctx.target === "b") return rgb.b;
      if (ctx.target === "dimmer") return Math.round(intensity * level);
      return 0;
    }
  });
})();
