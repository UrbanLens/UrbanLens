import { afterEach, describe, expect, test } from "bun:test";

import { positionAboveColliders } from "./floating-controls";

function makeBottomRightRect(collider: HTMLElement): void {
    collider.style.cssText = "position:fixed;right:10px;bottom:10px;width:80px;height:40px;";
    Object.defineProperty(collider, "getBoundingClientRect", {
        value: () => ({ top: window.innerHeight - 50, bottom: window.innerHeight - 10, left: window.innerWidth - 100, right: window.innerWidth - 10, width: 90, height: 40, x: 0, y: 0, toJSON: () => ({}) }),
    });
}

afterEach(() => {
    document.body.innerHTML = "";
});

describe("positionAboveColliders", () => {
    test("sets no offset when nothing else occupies the corner", () => {
        const root = document.createElement("div");
        document.body.appendChild(root);

        positionAboveColliders(root, "--offset-y", [".some-widget"]);

        expect(root.style.getPropertyValue("--offset-y")).toBe("0px");
    });

    test("lifts above a visible collider in the same corner", () => {
        const root = document.createElement("div");
        document.body.appendChild(root);
        const collider = document.createElement("div");
        collider.className = "some-widget";
        makeBottomRightRect(collider);
        document.body.appendChild(collider);

        positionAboveColliders(root, "--offset-y", [".some-widget"]);

        expect(Number.parseFloat(root.style.getPropertyValue("--offset-y"))).toBeGreaterThan(40);
    });

    test("ignores a hidden collider", () => {
        const root = document.createElement("div");
        document.body.appendChild(root);
        const collider = document.createElement("div");
        collider.className = "some-widget";
        collider.hidden = true;
        makeBottomRightRect(collider);
        document.body.appendChild(collider);

        positionAboveColliders(root, "--offset-y", [".some-widget"]);

        expect(root.style.getPropertyValue("--offset-y")).toBe("0px");
    });

    test("ignores itself and its own descendants", () => {
        const root = document.createElement("div");
        root.className = "some-widget";
        const child = document.createElement("div");
        child.className = "some-widget";
        makeBottomRightRect(child);
        root.appendChild(child);
        document.body.appendChild(root);

        positionAboveColliders(root, "--offset-y", [".some-widget"]);

        expect(root.style.getPropertyValue("--offset-y")).toBe("0px");
    });
});
