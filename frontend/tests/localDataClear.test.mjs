import test from "node:test";
import assert from "node:assert/strict";

const clearedStores = new Set();

class FakeTransaction extends EventTarget {
  objectStore(name) {
    return {
      clear() {
        clearedStores.add(name);
        if (clearedStores.size === 2) {
          queueMicrotask(() => {
            fakeTransaction.dispatchEvent(new Event("complete"));
          });
        }
      },
    };
  }
}

const fakeTransaction = new FakeTransaction();
const fakeDatabase = {
  transaction(storeNames, mode) {
    assert.deepEqual(storeNames, ["settings", "history"]);
    assert.equal(mode, "readwrite");
    return fakeTransaction;
  },
};

globalThis.indexedDB = {
  open() {
    const request = new EventTarget();
    request.result = fakeDatabase;
    queueMicrotask(() => request.dispatchEvent(new Event("success")));
    return request;
  },
};

const { clearAllLocalData } = await import("../src/db.js");

test("清空本地数据会同时清除设置、精简 Cookie 和历史记录所在存储", async () => {
  await clearAllLocalData();

  assert.deepEqual([...clearedStores].sort(), ["history", "settings"]);
});
