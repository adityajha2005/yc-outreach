// Exercises the real index.html <script> inside a sandboxed VM context to prove that
// marking one company's pipeline stage does not affect a different company that happens
// to share the same display name (YC has many name collisions across/within batches, e.g.
// two distinct "Palisade" companies both appear in Summer 2026 with slugs palisade/palisade-2).
//
// Pipeline state moved from a boolean "sent" Set (commit bfca064) to a per-slug stage enum
// (not_sent/sent/followed_up/replied/interview/rejected/closed, each with a timestamp) --
// this test now drives that stage select instead of the old checkbox, same slug-isolation intent.
//
// No npm dependencies: node:vm + node:assert only, matching this repo's stdlib-only policy.
// Run: node tests/test_sent_tracking.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "..", "index.html"), "utf8");
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(scriptMatch, "index.html must contain a single inline <script> block");

// Glue appended at the very end of the real script. Because the script declares its
// top-level state with `let`/`const`, those bindings are not exposed on the vm context
// object by default -- this trailing statement runs in the *same* top-level scope, so it
// can still read/write them directly, without altering a single line of the shipped code.
const glue = `
;globalThis.__test = {
  setData: (v) => { DATA = v; },
  getStages: () => stages,
};
`;
const source = scriptMatch[1] + glue;

function makeEl(id) {
  return {
    id, value: "", textContent: "", innerHTML: "", checked: false, disabled: false, hidden: false,
    dataset: {}, style: {}, href: "", _listeners: {},
    classList: {
      _set: new Set(),
      toggle(cls, force) {
        if (force === undefined) { this._set.has(cls) ? this._set.delete(cls) : this._set.add(cls); }
        else if (force) this._set.add(cls); else this._set.delete(cls);
      },
      contains(cls) { return this._set.has(cls); },
      add(cls) { this._set.add(cls); },
      remove(cls) { this._set.delete(cls); },
    },
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
    closest() { return this; },
    querySelector() { return makeEl(); },
    querySelectorAll() { return []; },
  };
}

function buildSandbox() {
  const elements = new Map();
  const detailsRegistry = [];
  const dataKFields = ["my_name", "portfolio", "github", "resume", "subject", "body"].map(k => {
    const el = makeEl();
    el.dataset.k = k;
    return el;
  });
  const storageMap = new Map();

  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeEl(id));
      return elements.get(id);
    },
    querySelectorAll(sel) {
      if (sel === "details" || sel === "#list details") return detailsRegistry;
      if (sel === "[data-k]") return dataKFields;
      return [];
    },
    querySelector(sel) {
      if (sel.includes('data-k="subject"')) return dataKFields.find(e => e.dataset.k === "subject");
      if (sel.includes('data-k="body"')) return dataKFields.find(e => e.dataset.k === "body");
      return makeEl();
    },
  };

  const localStorage = {
    getItem(k) { return storageMap.has(k) ? storageMap.get(k) : null; },
    setItem(k, v) { storageMap.set(k, String(v)); },
    removeItem(k) { storageMap.delete(k); },
  };
  // The real script also reads/writes sessionStorage (Gmail OAuth token) and uses
  // setInterval (queue countdown refresh) at the top level -- stub both so the whole
  // inline script can run unmodified inside this sandbox.
  const sessionMap = new Map();
  const sessionStorage = {
    getItem(k) { return sessionMap.has(k) ? sessionMap.get(k) : null; },
    setItem(k, v) { sessionMap.set(k, String(v)); },
    removeItem(k) { sessionMap.delete(k); },
  };

  const sandbox = {
    document, localStorage, sessionStorage,
    fetch: async () => { throw new Error("network disabled in test"); },
    setTimeout, clearTimeout,
    // unref so the one real interval the script starts (the queue countdown ticker)
    // can't keep this short-lived test process alive after the assertions run.
    setInterval: (fn, ms) => { const t = setInterval(fn, ms); t.unref?.(); return t; },
    clearInterval, URLSearchParams, console,
    __detailsRegistry: detailsRegistry,
  };
  return vm.createContext(sandbox);
}

function makeDetailsEl(index) {
  const d = makeEl();
  d.dataset.i = String(index);
  d.dataset.k = "";
  d.dataset.s = "none";
  d.dataset.jobs = "0";
  return d;
}

function run(companyA, companyB) {
  const ctx = buildSandbox();
  vm.runInContext(source, ctx, { filename: "index.html-inline-script" });

  const detailsA = makeDetailsEl(0);
  const detailsB = makeDetailsEl(1);
  ctx.__detailsRegistry.push(detailsA, detailsB);
  ctx.__test.setData([companyA, companyB]);

  const listEl = ctx.document.getElementById("list");
  const changeHandler = listEl._listeners.change[0];

  // Simulate picking "Sent" in company A's (index 0) pipeline-stage <select>.
  const stageSelect = makeEl();
  stageSelect.dataset.a = "stage";
  stageSelect.value = "sent";
  stageSelect.closest = (sel) => (sel === '[data-a="stage"]' ? stageSelect : sel === "details" ? detailsA : null);

  changeHandler({ target: stageSelect });

  return { stages: ctx.__test.getStages(), detailsA, detailsB };
}

// Two distinct real YC companies that share a display name but have different slugs
// (both from Summer 2026): this is the exact collision confirmed live against the API.
const companyA = { name: "Palisade", slug: "palisade" };
const companyB = { name: "Palisade", slug: "palisade-2" };

const { stages } = run(companyA, companyB);

// The bug this guards against: stage tracking keyed by company *name* would mark both
// same-named companies as sent, even though only the first one was ever actually marked.
const keyedByNameWouldCollide = stages[companyA.name] !== undefined || stages[companyB.name] !== undefined;
// The fix: keyed by slug, marking A must not affect B.
const keyedBySlugCorrectly = stages[companyA.slug]?.stage === "sent" && stages[companyB.slug] === undefined;

assert.equal(
  keyedBySlugCorrectly,
  true,
  `Expected pipeline stage to be tracked by the unique company slug, not the (possibly duplicate) name. ` +
  `stages=${JSON.stringify(stages)} -- marking "${companyA.name}" (slug ${companyA.slug}) as sent must not ` +
  `also mark the unrelated company "${companyB.name}" (slug ${companyB.slug}) as sent.`
);
assert.equal(
  keyedByNameWouldCollide,
  false,
  "pipeline-stage tracking must not use the non-unique company name as its key (YC has real name collisions, " +
  'e.g. two distinct "Palisade" companies both in Summer 2026: slugs palisade / palisade-2)'
);

console.log("PASS: pipeline-stage tracking is keyed by unique company slug, not by the collidable display name.");
