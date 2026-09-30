import { afterEach, beforeEach, expect, test } from "bun:test";

import { watchSectionTabs } from "./section-tabs";

type Entry = { isIntersecting: boolean; target: Element };
const realObserver = globalThis.IntersectionObserver;
let report: (entries: Entry[]) => void = () => {};
let observed: Element[] = [];

beforeEach(() => {
    observed = [];
    Reflect.set(
        globalThis,
        "IntersectionObserver",
        class {
            constructor(callback: (entries: Entry[]) => void) {
                report = callback;
            }
            observe(el: Element): void {
                observed.push(el);
            }
            disconnect(): void {
                observed = [];
            }
        },
    );
    document.body.innerHTML = `
      <nav><a href="#one" data-section-tab>One</a><a href="#two" data-section-tab>Two</a><a href="#gone" data-section-tab>Gone</a></nav>
      <section id="one"></section><section id="two"></section>`;
});

afterEach(() => Reflect.set(globalThis, "IntersectionObserver", realObserver));

const active = () => Array.from(document.querySelectorAll("[data-section-tab]")).map((tab) => tab.classList.contains("active"));
const section = (id: string): Element => {
    const el = document.getElementById(id);
    if (!el) throw new Error(id);
    return el;
};

test("the tab for the section in view is the active one", () => {
    const stop = watchSectionTabs(document);
    expect(observed.map((el) => el.id)).toEqual(["one", "two"]);
    report([{ isIntersecting: true, target: section("two") }]);
    expect(active()).toEqual([false, true, false]);
    report([
        { isIntersecting: false, target: section("two") },
        { isIntersecting: true, target: section("one") },
    ]);
    expect(active()).toEqual([true, false, false]);
    stop();
    expect(observed).toEqual([]);
});

test("a page without section tabs watches nothing", () => {
    document.body.innerHTML = `<section id="one"></section>`;
    watchSectionTabs(document)();
    expect(observed).toEqual([]);
});
