import assert from "node:assert/strict";
import { it } from "node:test";
import {
  defaultLayoutPreferences,
  parseLayoutPreferences,
  resolveWorkspaceWidths,
  rightSidebarVisible,
  workspaceLayoutMode,
} from "./workspaceLayout.ts";

it("falls back safely for damaged, obsolete and invalid layout preferences", () => {
  for (const raw of [null, "broken", "null", "[]", "5", '{"version":2,"leftVisible":false}']) {
    assert.deepEqual(parseLayoutPreferences(raw), defaultLayoutPreferences());
  }
  assert.deepEqual(parseLayoutPreferences(JSON.stringify({
    version: 1, leftWidth: -200, rightWidth: 999999,
    leftVisible: "false", rightVisibility: "sometimes",
  })), { ...defaultLayoutPreferences(), leftWidth: 190, rightWidth: 800 });
  assert.deepEqual(parseLayoutPreferences(JSON.stringify({
    version: 1, leftWidth: 350, rightWidth: 440, leftVisible: false, rightVisibility: "hidden",
  })), { ...defaultLayoutPreferences(), leftWidth: 350, rightWidth: 440, leftVisible: false, rightVisibility: "hidden" });
});

it("restores chat placement without changing visibility or older layout preferences", () => {
  const old = { version: 1, leftWidth: 280, rightWidth: 600, leftVisible: false, rightVisibility: "hidden" };
  assert.deepEqual(parseLayoutPreferences(JSON.stringify(old)), { ...old, chatPlacement: "center" });
  const docked = { ...old, chatPlacement: "right" };
  assert.deepEqual(parseLayoutPreferences(JSON.stringify(docked)), docked);
  for (const chatPlacement of [null, "left", true, {}, 1]) {
    assert.deepEqual(parseLayoutPreferences(JSON.stringify({ ...old, chatPlacement })), { ...old, chatPlacement: "center" });
  }
});

it("shows the inspector by default and preserves explicit visibility choices", () => {
  assert.equal(rightSidebarVisible("auto"), true);
  assert.equal(rightSidebarVisible("shown"), true);
  assert.equal(rightSidebarVisible("hidden"), false);
});

it("keeps breakpoint boundaries consistent for pointer resizing", () => {
  assert.equal(workspaceLayoutMode(760), "stacked");
  assert.equal(workspaceLayoutMode(761), "compact");
  assert.equal(workspaceLayoutMode(1120), "compact");
  assert.equal(workspaceLayoutMode(1121), "desktop");
});

it("keeps the center usable for every visibility combination and requested width", () => {
  for (const workspaceWidth of [761, 800, 914, 915, 1000, 1120, 1121, 1280, 1440, 1700]) {
    for (const leftVisible of [false, true]) {
      for (const rightVisible of [false, true]) {
        for (const [leftWidth, rightWidth] of [[190, 245], [240, 310], [420, 480], [420, 800]]) {
          const resolved = resolveWorkspaceWidths({
            workspaceWidth, viewportWidth: workspaceWidth,
            preferences: { ...defaultLayoutPreferences(), leftWidth, rightWidth }, leftVisible, rightVisible,
          });
          assert.ok(resolved.centerWidth >= (workspaceWidth >= 915 ? 480 : 320));
          if (leftVisible) assert.ok(resolved.leftWidth >= 190);
          if (rightVisible) assert.ok(resolved.rightWidth >= 245);
          assert.equal(resolved.leftWidth + resolved.centerWidth + resolved.rightWidth, workspaceWidth);
          assert.ok(resolved.leftWidth <= 420 && resolved.rightWidth <= 800);
          assert.ok(resolved.leftWidth >= resolved.leftBounds.min && resolved.leftWidth <= resolved.leftBounds.max);
          assert.ok(resolved.rightWidth >= resolved.rightBounds.min && resolved.rightWidth <= resolved.rightBounds.max);
          if (!leftVisible) assert.equal(resolved.leftWidth, 0);
          if (!rightVisible) assert.equal(resolved.rightWidth, 0);
        }
      }
    }
  }
});

it("fits the actual workspace and restores requested widths after compression", () => {
  const preferences = { ...defaultLayoutPreferences(), leftWidth: 420, rightWidth: 480 };
  const resolve = (workspaceWidth: number) => resolveWorkspaceWidths({
    workspaceWidth, viewportWidth: 1800, preferences, leftVisible: true, rightVisible: true,
  });
  const compressed = resolve(1121);
  assert.ok(compressed.leftWidth < preferences.leftWidth);
  assert.ok(compressed.rightWidth < preferences.rightWidth);
  assert.ok(compressed.centerWidth >= 480);
  assert.deepEqual([preferences.leftWidth, preferences.rightWidth], [420, 480]);
  assert.deepEqual([resolve(1700).leftWidth, resolve(1700).rightWidth], [420, 480]);
  assert.ok(resolve(800).centerWidth >= 320);
  assert.equal(resolve(300).centerWidth, 300);
});

it("keeps the inspector in its column until mobile layout without changing preferences", () => {
  const preferences = { ...defaultLayoutPreferences(), leftWidth: 420, rightWidth: 480 };
  const compact = resolveWorkspaceWidths({ workspaceWidth: 800, viewportWidth: 800, preferences, leftVisible: true, rightVisible: true });
  assert.equal(compact.rightWidth, 245);
  assert.equal(compact.leftWidth, 190);
  assert.equal(compact.centerWidth, 365);
  const stacked = resolveWorkspaceWidths({ workspaceWidth: 390, viewportWidth: 390, preferences, leftVisible: true, rightVisible: true });
  assert.equal(stacked.leftWidth, 0);
  assert.equal(stacked.rightWidth, 0);
  assert.deepEqual([preferences.leftWidth, preferences.rightWidth], [420, 480]);
});

it("a manual change uses actual pixels even after viewport compression", () => {
  const resolve = (leftWidth: number, rightWidth: number) => resolveWorkspaceWidths({
    workspaceWidth: 1121, viewportWidth: 1121,
    preferences: { ...defaultLayoutPreferences(), leftWidth, rightWidth }, leftVisible: true, rightVisible: true,
  });
  const initial = resolve(420, 480);
  assert.equal(resolve(initial.leftWidth - 10, 480).leftWidth, initial.leftWidth - 10);
  const compressedRight = resolve(240, 480);
  assert.equal(resolve(240, compressedRight.rightWidth - 10).rightWidth, compressedRight.rightWidth - 10);
});


it("allows a wide chat rail while preserving the left rail and reading area", () => {
  const preferences = { ...defaultLayoutPreferences(), rightWidth: 800 };
  const resolve = (workspaceWidth: number) => resolveWorkspaceWidths({
    workspaceWidth, viewportWidth: workspaceWidth, preferences, leftVisible: true, rightVisible: true,
  });
  assert.equal(resolve(1700).rightWidth, 800);
  assert.equal(resolve(1440).rightWidth, 720);
  assert.equal(resolve(1000).rightWidth, 280);
  for (const width of [1000, 1440, 1700]) {
    assert.equal(resolve(width).leftWidth, 240);
    assert.ok(resolve(width).centerWidth >= 480);
  }
  assert.equal(parseLayoutPreferences(JSON.stringify(preferences)).rightWidth, 800);
});
