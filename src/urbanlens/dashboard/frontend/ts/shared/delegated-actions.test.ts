import { beforeEach, describe, expect, test } from "bun:test";

import { delegateActions } from "./delegated-actions";

let ran: string[] = [];

beforeEach(() => {
    ran = [];
    document.body.innerHTML = `
      <div id="panel">
        <button type="button" id="go" data-demo-action="go" data-arg="7"><i id="go-icon">play</i></button>
        <button type="button" id="stop" data-demo-action="stop">Stop</button>
        <button type="button" id="proto" data-demo-action="toString">Sneaky</button>
      </div>
      <button type="button" id="outside" data-demo-action="go">Outside</button>`;
});

describe("delegateActions", () => {
    test("a click anywhere in a control runs its named action with the control", () => {
        const stop = delegateActions(document, "demo-action", { go: (control) => void ran.push(`go ${control.dataset.arg}`) });
        document.getElementById("go-icon")!.click();
        stop();
        expect(ran).toEqual(["go 7"]);
    });

    test("a name the page did not list does nothing, inherited object keys included", () => {
        const stop = delegateActions(document, "demo-action", { go: () => void ran.push("go") });
        document.getElementById("stop")!.click();
        document.getElementById("proto")!.click();
        stop();
        expect(ran).toEqual([]);
    });

    test("bound to an element, it answers only for controls inside it, and can keep the click from going further", () => {
        let reachedDocument = 0;
        const count = () => void reachedDocument++;
        document.addEventListener("click", count);
        const stop = delegateActions(document.getElementById("panel")!, "demo-action", {
            go: (_control, event) => {
                ran.push("go");
                event.stopPropagation();
            },
        });
        document.getElementById("go")!.click();
        document.getElementById("outside")!.click();
        stop();
        document.removeEventListener("click", count);
        expect(ran).toEqual(["go"]);
        expect(reachedDocument).toBe(1);
    });

    test("stopping removes the listener", () => {
        const stop = delegateActions(document, "demo-action", { go: () => void ran.push("go") });
        stop();
        document.getElementById("go")!.click();
        expect(ran).toEqual([]);
    });
});
