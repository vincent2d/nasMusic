const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("app/ui/app.js", "utf8");
const policy = fs.readFileSync("app/ui/user_media_state_policy.js", "utf8");
const outboxKey = "fnaudio.neteaseFavoriteOutbox.v1";
function hostFunction(name) {
  const match = source.match(new RegExp(`(?:async )?function ${name}\\([^]*?\\n\\}`));
  assert.ok(match, `missing ${name}`);
  return match[0];
}
function harness(storage = new Map()) {
  const nodes = new Map();
  const requests = [];
  const alerts = [];
  const state = { favorites: [], playlists: [], userStateBase: { favorites: [], playlists: [] } };
  const context = vm.createContext({
    state, Map, Date, NETEASE_FAVORITE_OUTBOX_KEY: outboxKey,
    FAVORITE_STORE_KEY: "fnaudio.favorites", PLAYLIST_STORE_KEY: "fnaudio.playlists",
    MAX_LOCAL_FAVORITES: 50000, MAX_USER_MEDIA_STORAGE_CHARS: 4 * 1024 * 1024,
    localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    parseStoredJson: (key, fallback) => { try { return JSON.parse(storage.get(key)); } catch (_) { return fallback; } },
    writeStoredUserMedia: (key, value) => storage.set(key, JSON.stringify(value)),
    updateFavoriteControls() {}, refreshFavoritePresentation() {}, hydrateReferencedItems() {},
    userStateRevision: value => Number(value) || 0,
    isNeteaseTrack: id => String(id).startsWith("netease:"),
    alert: message => alerts.push(message), confirm: () => true,
    $: id => {
      if (!nodes.has(id)) nodes.set(id, { open: false, showModal() { this.open = true; }, reset() {} });
      return nodes.get(id);
    },
    fetchJsonResponseBounded: async (url, options) => {
      const body = options?.body ? JSON.parse(options.body) : null;
      requests.push({ url, body });
      return context.reply(url, body);
    },
    reply: () => ({ response: { ok: false, status: 403 }, data: { error: "netease_mobile_authorization_required" } }),
  });
  vm.runInContext(policy, context);
  vm.runInContext([
    "normalizeUserMediaState", "userStateChanges", "applyUserStateChanges",
    "persistUserMediaState", "applyUserMediaAcknowledgement",
  ].map(hostFunction).join("\n"), context);
  vm.runInContext(source.slice(source.indexOf("function loadNeteaseFavoriteOutbox("), source.indexOf("function toggleCurrentFavorite(")), context);
  state.neteaseFavoriteOutbox = context.loadNeteaseFavoriteOutbox();
  state.favorites = context.withPendingNeteaseFavorites(JSON.parse(storage.get("fnaudio.favorites") || "[]"));
  return { context, state, storage, requests, alerts, nodes };
}
const ok = (favorites, revision) => ({ response: { ok: true, status: 200 }, data: { ok: true, state: { favorites, revision, playlists: [] } } });
const ids = state => [...state.favorites];

(async () => {
  let h = harness();
  await h.context.syncNeteaseFavorite("netease:123", true);
  await h.context.syncNeteaseFavorite("netease:456", true);
  assert.deepEqual(ids(h.state), ["netease:123", "netease:456"]);
  assert.equal(h.state.neteaseFavoriteOutbox.size, 2);
  assert.equal(h.alerts.length, 0, "unauthorized favorites must not interrupt listening");
  assert.equal(h.nodes.get("neteaseMobileDialog")?.open || false, false);
  assert.deepEqual([...h.context.userStateChanges().favorites_add], [], "pending online likes must not bypass the authorized endpoint");

  h = harness(h.storage); // reload/redeployment: both the intent and its presentation survive
  h.context.persistUserMediaState({ favorites: ["local"], playlists: [] });
  assert.deepEqual(ids(h.state), ["netease:123", "netease:456", "local"], "server hydration must preserve pending likes");
  h.context.applyUserMediaAcknowledgement({ favorites: ["local"], playlists: [], revision: 1 });
  assert.ok(h.state.favorites.includes("netease:123"), "unrelated acknowledgements must preserve pending likes");

  h.context.reply = (_, body) => body.track_id === "netease:123"
    ? ok(["netease:123", "local"], 2) : { response: { ok: false, status: 502 }, data: { message: "temporary failure" } };
  await h.context.flushPendingNeteaseFavorites(true);
  assert.deepEqual([...h.state.neteaseFavoriteOutbox], [["netease:456", true]], "only confirmed operations can leave the outbox");
  assert.ok(h.state.favorites.includes("netease:456"), "a failed remainder stays locally liked");
  const sent = h.requests.length;
  await h.context.flushPendingNeteaseFavorites();
  assert.equal(h.requests.length, sent, "background failures must be throttled");

  h.context.reply = () => { throw new Error("offline"); };
  await h.context.flushPendingNeteaseFavorites(true);
  assert.equal(h.state.neteaseFavoriteOutbox.size, 1);
  h = harness(h.storage);
  h.context.reply = () => ok(["netease:123", "netease:456", "local"], 3);
  await h.context.flushPendingNeteaseFavorites(true);
  assert.deepEqual(h.requests.map(r => r.body.track_id), ["netease:456"], "retry must resume from the unconfirmed remainder");
  assert.equal(h.state.neteaseFavoriteOutbox.size, 0);

  h.context.stageNeteaseFavorite("netease:123", true);
  h.context.stageNeteaseFavorite("netease:123", false);
  h.context.persistUserMediaState({ favorites: ["netease:123", "local"], playlists: [] });
  assert.deepEqual(ids(h.state), ["local"], "queued cancellation must win over an old server like");
  h = harness(h.storage);
  let release;
  h.context.reply = (_, body) => new Promise(resolve => { release = () => resolve(ok(["local"], 4)); assert.equal(body.liked, false); });
  const flush = h.context.flushPendingNeteaseFavorites(true);
  await h.context.flushPendingNeteaseFavorites(true);
  assert.equal(h.requests.length, 1, "concurrent retry triggers must share one writer");
  release();
  await flush;
  assert.deepEqual(ids(h.state), ["local"]);
  assert.equal(h.storage.get(outboxKey), "[]");

  h = harness(new Map([[outboxKey, JSON.stringify([["netease:0", true], ["local", true], ["netease:123", "true"], ["netease:456", false]])]]));
  assert.deepEqual([...h.state.neteaseFavoriteOutbox], [["netease:456", false]]);
  h.context.reply = url => url.endsWith("/mobile")
    ? { response: { ok: true }, data: { ok: true, mode: "fnos", url: "" } } : ok([], 1);
  await h.context.openNeteaseMobileAuth();
  assert.equal(h.state.neteaseFavoriteOutbox.size, 0, "fnOS authorization must flush even without a phone HTTPS URL");
  assert.match(h.nodes.get("neteaseMobileStatus").textContent, /全部同步/);

  h = harness();
  h.context.localStorage.setItem = () => { throw new Error("quota"); };
  h.context.stageNeteaseFavorite("netease:123", true);
  assert.deepEqual(ids(h.state), ["netease:123"]);
  assert.match(h.alerts[0], /关闭页面前/);
  console.log("NetEase favorite outbox tests passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
