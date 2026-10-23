// Exercises the real index.html <script> inside a sandboxed VM context to prove the
// industry filter (added alongside the existing email/hiring/search filters) actually
// narrows the visible list, and that the "Top -> Sub" industry string YC returns is
// reduced to its top-level category consistently.
//
// No npm dependencies: node:vm + node:assert only, matching this repo's stdlib-only policy.
// Run: node tests/test_industry_filter.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "..", "index.html"), "utf8");
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(scriptMatch, "index.html must contain a single inline <script> block");

// Glue appended at the very end of the real script, same technique as
// test_sent_tracking.mjs: `let`/`const` top-level bindings in index.html aren't exposed
// on the vm context by default, but a trailing statement in the same scope can still
// read them directly without altering a single line of the shipped code.
const glue = `
;globalThis.__test = {
  industryTop,
  filter,
  populateIndustries,
  setAll: (v) => { ALL = v; },
  setData: (v) => { DATA = v; },
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
    querySelector() { return makeEl(); },
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

function makeDetailsEl({ ind = "", s = "none", jobs = "0", k = "", sent = false } = {}) {
  const d = makeEl();
  d.dataset.i = "0";
  d.dataset.k = k;
  d.dataset.s = s;
  d.dataset.ind = ind;
  d.dataset.jobs = jobs;
  if (sent) d.classList.add("sent");
  return d;
}

const ctx = buildSandbox();
vm.runInContext(source, ctx, { filename: "index.html-inline-script" });

// --- industryTop: reduces YC's "Top -> Sub" string to the top-level category ---
assert.equal(ctx.__test.industryTop("B2B -> Analytics"), "B2B");
assert.equal(ctx.__test.industryTop("Consumer"), "Consumer");
assert.equal(ctx.__test.industryTop(""), "");
assert.equal(ctx.__test.industryTop(undefined), "");
assert.equal(ctx.__test.industryTop("  Fintech ->  Payments  "), "Fintech");
console.log("PASS: industryTop reduces 'Top -> Sub' industry strings to their top-level category.");

// --- populateIndustries: builds the dropdown from the batch's own distinct top-level industries ---
ctx.__test.setAll([
  { industry: "B2B -> Analytics" },
  { industry: "B2B -> Sales" },
  { industry: "Fintech -> Payments" },
  { industry: "" },
  { industry: undefined },
]);
ctx.__test.populateIndustries();
const indSelect = ctx.document.getElementById("ind");
assert.match(indSelect.innerHTML, /All industries/);
assert.match(indSelect.innerHTML, /value="B2B">B2B</);
assert.match(indSelect.innerHTML, /value="Fintech">Fintech</);
// No duplicate "B2B" option despite two B2B companies, and no empty-string option.
assert.equal((indSelect.innerHTML.match(/value="B2B"/g) || []).length, 1);
assert.doesNotMatch(indSelect.innerHTML, /value=""><\/option>/);
console.log("PASS: populateIndustries lists each distinct top-level industry once, skipping blanks.");

// --- filter(): the industry dropdown actually narrows the visible list ---
const b2bRow = makeDetailsEl({ ind: "B2B", k: "acme corp" });
const fintechRow = makeDetailsEl({ ind: "Fintech", k: "payco" });
ctx.__test.setData([{}, {}]); // only length matters for the "shown" summary text
ctx.document.querySelectorAll("details"); // sanity: registry exists
ctx.__detailsRegistry.push(b2bRow, fintechRow);

ctx.document.getElementById("ind").value = "B2B";
ctx.__test.filter();
assert.equal(b2bRow.style.display, "", "the B2B row must stay visible when 'B2B' is selected");
assert.equal(fintechRow.style.display, "none", "the Fintech row must be hidden when 'B2B' is selected");
assert.match(ctx.document.getElementById("shown").textContent, /^1 of 2 shown/);

ctx.document.getElementById("ind").value = "";
ctx.__test.filter();
assert.equal(b2bRow.style.display, "", "clearing the industry filter must show every row again");
assert.equal(fintechRow.style.display, "");
assert.match(ctx.document.getElementById("shown").textContent, /^2 of 2 shown/);

console.log("PASS: the industry filter narrows (and un-narrows) the visible company list.");
