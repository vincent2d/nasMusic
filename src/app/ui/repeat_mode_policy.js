(function (root) {
  "use strict";

  const SETTINGS = Object.freeze({
    all: Object.freeze({ repeat: "all", shuffle: false }),
    one: Object.freeze({ repeat: "one", shuffle: false }),
    shuffle: Object.freeze({ repeat: "all", shuffle: true }),
  });
  const LABELS = Object.freeze({ off: "顺序播放", all: "列表循环", one: "单曲循环", shuffle: "随机播放" });
  const NEXT = Object.freeze({ off: "all", all: "one", one: "shuffle", shuffle: "all" });

  function mode(repeat, shuffle) {
    if (repeat === "one") return "one";
    if (shuffle === true) return "shuffle";
    return repeat === "all" ? "all" : "off";
  }

  function iconMarkup(value) {
    if (value === "shuffle") {
      return '<path d="M3 6h2c5 0 9 12 14 12h2m-3-3 3 3-3 3M3 18h2c2.3 0 4.2-2.6 6.2-5.4M14 8c1.5-1.3 2.8-2 5-2h2m-3-3 3 3-3 3"/>';
    }
    if (value === "off") {
      return '<path d="M3 6h13M3 12h9M3 18h9m4-4 5 3-5 3z"/>';
    }
    const loop = '<path d="m17 3 4 4-4 4M3 11V9a2 2 0 0 1 2-2h16M7 21l-4-4 4-4m14 0v2a2 2 0 0 1-2 2H3"/>';
    return value === "one" ? loop + '<path d="m10.5 11 1.5-1v4"/>' : loop;
  }

  root.fnAudioRepeatModePolicy = Object.freeze({ mode, iconMarkup, labels: LABELS, next: NEXT, settings: SETTINGS });
})(globalThis);
