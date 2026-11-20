const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("app/ui/app.js", "utf8");
const body = source.slice(source.indexOf("async function prefetchNeteaseArtwork("), source.indexOf("function prefetchCurrentTrackMetadata("));
const state = { queue: [{ id: "netease:123" }, { id: "netease:456" }], index: 0, itemCache: new Map() };
const pending = [];
const painted = [];
const context = {
  state, AbortController,
  isNeteaseTrack: id => id.startsWith("netease:"),
  coverUrl: item => item.cover_url || "",
  usableCoverSource: value => value || "",
  currentItem: () => state.queue[state.index],
  fetchLibraryItemsByIds: (ids, signal) => new Promise((resolve, reject) => pending.push({ ids, signal, resolve, reject })),
  setNowPlayingCover: cover => painted.push(cover),
  updateLyricsMeta: item => painted.push(item.id),
  updateMediaSessionMetadata: () => {},
};
vm.runInNewContext(body + "; this.load = prefetchNeteaseArtwork;", context);
(async () => {
  const first = context.load(state.queue[0]);
  await context.load(state.queue[0]);
  assert.equal(pending.length, 1, "player and lyrics must share the pending artwork read");
  state.index = 1;
  const second = context.load(state.queue[1]);
  assert.equal(pending[0].signal.aborted, true);
  pending[0].resolve([{ id: "netease:123", cover_url: "old-cover" }]);
  await first;
  assert.deepEqual(painted, [], "an old song's reply must not repaint the current song");
  pending[1].reject(new Error("temporary failure"));
  await second;
  assert.equal(state.neteaseArtworkRequest, null);
  const retry = context.load(state.queue[1]);
  pending[2].resolve([{ id: "netease:456", cover_url: "current-cover" }]);
  await retry;
  assert.equal(state.queue[1].cover_url, "current-cover");
  assert.deepEqual(painted, ["current-cover", "netease:456"]);
  await context.load(state.queue[1]);
  assert.equal(pending.length, 3, "resolved artwork must not trigger another request");
  await context.load({ id: "local-track" });
  assert.equal(pending.length, 3, "local tracks retain their existing metadata path");
  console.log("NetEase playback artwork tests passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
