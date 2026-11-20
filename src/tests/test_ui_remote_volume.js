const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("app/ui/app.js", "utf8");
const section = source.slice(source.indexOf("let _isUserAdjustingVolume = false;"), source.indexOf("function updateVolumeRail(value", source.indexOf("let _isUserAdjustingVolume = false;")));
const requests = [];
const errors = [];
let selected = "speaker-a";
let succeeds = false;
const context = {
  Math, isRemoteTarget: () => true, remoteDeviceId: () => selected,
  alert: message => errors.push(message),
  coreControl: async (action, value) => { requests.push({ target:selected, action, value }); return succeeds ? {ok:true} : false; },
};
vm.runInNewContext(section + ";this.setVolume = _syncRemoteVolume; this.flush = _flushRemoteVolume;", context);
(async () => {
  context.setVolume(0.28);
  await new Promise(setImmediate);
  assert.equal(errors.length, 1);
  succeeds = true;
  context.setVolume(0.28);
  await new Promise(setImmediate);
  assert.equal(requests.length, 2, "same value must retry after failure");
  context.setVolume(0.28);
  await new Promise(setImmediate);
  assert.equal(requests.length, 2, "confirmed duplicate must not resend");
  selected = "speaker-b";
  context.setVolume(0.28);
  await new Promise(setImmediate);
  assert.equal(requests.length, 3, "a different device must receive the same numeric volume");
  assert.deepEqual(requests[2], {target:"speaker-b", action:"volume", value:28});
  console.log("Remote volume retry and device switching passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
