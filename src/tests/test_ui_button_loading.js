const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("app/ui/app.js", "utf8");
const start = source.indexOf("function setButtonLoading(");
const end = source.indexOf("function writeStoredUserMedia(", start);
const context = {};
vm.runInNewContext(source.slice(start, end) + "; this.run = withButtonLoading;", context);

(async () => {
  const attrs = new Map();
  const classes = new Set();
  const button = {
    getAttribute: name => attrs.get(name),
    setAttribute: (name, value) => attrs.set(name, value),
    removeAttribute: name => attrs.delete(name),
    classList: { toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name) },
  };
  let complete;
  const pending = context.run(button, () => new Promise(resolve => { complete = resolve; }));
  assert.equal(attrs.get("aria-busy"), "true", "feedback must start before the network reply");
  assert.ok(classes.has("is-loading"));
  let duplicate = false;
  await context.run(button, () => { duplicate = true; });
  assert.equal(duplicate, false, "repeated clicks must not send duplicate commands");
  complete(42);
  assert.equal(await pending, 42);
  assert.equal(attrs.has("aria-busy"), false);
  assert.equal(classes.has("is-loading"), false);
  await assert.rejects(context.run(button, async () => { throw new Error("network failed"); }), /network failed/);
  assert.equal(attrs.has("aria-busy"), false, "errors must leave the button retryable");
  assert.equal(await context.run(button, () => "retried"), "retried");
  console.log("button loading tests passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
