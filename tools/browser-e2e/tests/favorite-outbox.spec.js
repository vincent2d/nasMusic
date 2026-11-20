const path = require("path");
const { spawn } = require("child_process");
const { test, expect } = require("@playwright/test");

let fixture, baseUrl;
test.beforeAll(async () => {
  fixture = spawn(process.env.PYTHON || "python3", ["-B", path.resolve(__dirname, "../../ui_e2e_fixture.py"), "--port", "0"], {
    cwd: path.resolve(__dirname, "../../.."), stdio: ["ignore", "pipe", "pipe"],
  });
  baseUrl = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("fixture startup timed out")), 8000);
    fixture.once("error", reject);
    fixture.stdout.once("data", data => { clearTimeout(timer); resolve(String(data).trim()); });
  });
});
test.afterAll(() => fixture?.kill());

for (const width of [390, 1280]) {
  test(`unauthorized favorites survive reload and batch sync after approval at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 844 });
    const songs = [123, 456].map(id => ({ id: `netease:${id}`, title: `待同步歌曲${id}`, artist: "测试歌手", duration: 120, format: "网易云" }));
    const serverState = { initialized: true, revision: 1, favorites: ["fixture-1"], playlists: [], playback: {} };
    let authorized = false;
    const confirmed = [];
    const alerts = [];
    page.on("dialog", async dialog => { alerts.push(dialog.message()); await dialog.dismiss(); });
    await page.route("**/api/state/user", route => route.fulfill({ json: { ok: true, state: serverState } }));
    await page.route("**/api/library/items?*", route => route.fulfill({ json: { items: songs } }));
    await page.route("**/api/netease/mobile", route => route.fulfill({ json: { ok: true, mode: authorized ? "phone" : "direct", authorized, url: "" } }));
    await page.route("**/api/netease/favorite", route => {
      if (!authorized) return route.fulfill({ status: 403, json: { ok: false, error: "netease_mobile_authorization_required" } });
      const payload = route.request().postDataJSON();
      confirmed.push(payload);
      serverState.favorites = serverState.favorites.filter(id => id !== payload.track_id);
      if (payload.liked) serverState.favorites.unshift(payload.track_id);
      serverState.revision += 1;
      return route.fulfill({ json: { ok: true, state: serverState } });
    });
    await page.goto(baseUrl);
    await expect.poll(() => page.evaluate(() => state.userStateHydrated)).toBe(true);
    for (const song of songs) {
      // Seed only playback: exercise the actual favorite button and persistence.
      await page.evaluate(song => { state.queue = [song]; state.index = 0; state.itemCache.set(song.id, song); updateFavoriteControls(); }, song);
      await page.locator("#favoriteCurrentBtn").click();
      await expect(page.locator("#favoriteCurrentBtn")).toHaveAttribute("aria-pressed", "true");
      await expect(page.locator("#favoriteCurrentBtn")).toBeEnabled();
    }
    expect(alerts).toEqual([]);
    await expect(page.locator("#neteaseMobileDialog")).not.toBeVisible();
    await page.reload();
    await expect.poll(() => page.evaluate(() => state.userStateHydrated)).toBe(true);
    await page.evaluate(() => setView("favorites"));
    await expect(page.locator('.track[data-track-id="netease:123"]')).toBeVisible();
    await expect(page.locator('.track[data-track-id="netease:456"]')).toBeVisible();
    await expect(page.locator(".playlist-toolbar")).toContainText("2 项更改待同步");
    await page.evaluate(() => openNeteaseMobileAuth());
    await expect(page.locator("#neteaseMobileHelp")).toContainText("尚未配置");
    await expect(page.locator("#neteaseMobileStatus")).toContainText("2 项更改已保存在本机");
    await page.screenshot({ path: info.outputPath(`pending-${width}.png`) });
    authorized = true;
    await page.locator("#neteaseMobileContinue").click();
    await expect(page.locator("#neteaseMobileStatus")).toContainText("全部同步");
    expect(confirmed).toEqual(songs.map(song => ({ track_id: song.id, liked: true })));
    expect(await page.evaluate(() => JSON.parse(localStorage.getItem("fnaudio.neteaseFavoriteOutbox.v1")))).toEqual([]);
    await page.keyboard.press("Escape");
    await expect(page.locator(".playlist-toolbar")).not.toContainText("待同步");
    expect(serverState.favorites).toContain("fixture-1");
    await page.reload();
    await page.evaluate(() => setView("favorites"));
    await expect(page.locator('.track[data-track-id="netease:123"]')).toBeVisible();
  });
}
