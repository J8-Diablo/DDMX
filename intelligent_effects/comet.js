// Intelligent Effect: Comet
(function () {
  if (typeof window.registerIntelligentEffect !== "function") return;

  window.registerIntelligentEffect({
    id: "comet",
    label: "Comet",
    targets: ["color", "dimmer"],
    mode: "absolute",
    params: [
      { key: "phase", label: "Phase (ms)", type: "text", default: "0", hint: "0, 0>500, 0<500, 0><500, 0<>500, 0|500, 0||500, 0?500" },
      { key: "speed", label: "Speed (cycles/s)", type: "range", min: 0, max: 3, step: 0.05, default: 0.5 },
      { key: "length", label: "Tail (devices)", type: "range", min: 1, max: 30, step: 1, default: 8 },
      { key: "reverse", label: "Reverse", type: "select", options: ["Off", "On"], default: "Off" },
      { key: "hue", label: "Hue", type: "range", min: 0, max: 360, step: 1, default: 40 },
      { key: "saturation", label: "Saturation", type: "range", min: 0, max: 1, step: 0.05, default: 1 },
      { key: "intensity", label: "Intensity", type: "range", min: 0, max: 255, step: 1, default: 255 }
    ],
    apply: (ctx) => {
      const count = Math.max(1, ctx.deviceCount || 1);
      const phase = ctx.helpers.phaseOffsetMs(ctx);
      const tMs = ctx.tMs + phase;
      const speed = Number(ctx.params.speed || 0);
      const length = Math.max(1, Number(ctx.params.length || 1));
      const reverse = String(ctx.params.reverse || "Off").toLowerCase() === "on";
      const hue = Number(ctx.params.hue || 0);
      const sat = ctx.helpers.clamp(Number(ctx.params.saturation || 0), 0, 1);
      const intensity = ctx.helpers.clamp(Number(ctx.params.intensity || 0), 0, 255);

      const head = (tMs / 1000) * speed * count;
      const pos = ((reverse ? -head : head) % count + count) % count;

      let delta = pos - ctx.deviceIndex;
      if (delta < 0) delta += count;

      let level = 0;
      if (delta <= length) {
        level = 1 - delta / Math.max(1, length);
      }

      const val = (intensity / 255) * level;
      const rgb = ctx.helpers.hueToRgb(hue, sat, val);

      if (ctx.target === "r") return rgb.r;
      if (ctx.target === "g") return rgb.g;
      if (ctx.target === "b") return rgb.b;
      if (ctx.target === "dimmer") return Math.round(intensity * level);
      return 0;
    }
  });
})();
