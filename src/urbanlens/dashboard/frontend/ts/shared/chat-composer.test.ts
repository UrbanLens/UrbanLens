/**
 * The composer's whole job is the gap between "sent" and "accepted".
 *
 * All three game clients cleared their input the moment `send()` returned true,
 * which reports transmission. The consumer answers a refused frame with
 * `{"type": "error"}` - an out-of-scope credential, a failed write, and soon a
 * volume limit - and none of them had a `case "error"` at all, so a refusal
 * arrived as a message that simply vanished (P31).
 *
 * The ordering tests are the ones that matter. Under a rate limit, refusals
 * arrive for the *oldest* unacknowledged send while newer ones may already have
 * succeeded, so a composer that just remembers "the last thing I sent" hands
 * back the wrong message exactly when it is being used most.
 */

import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";

import { ChatComposer, toastRefusal } from "./chat-composer";

const toasted: string[] = [];

mock.module("./dialogs", () => ({
    toast: {
        error: (message: string) => toasted.push(message),
        success: () => {},
        info: () => {},
        warning: () => {},
    },
}));

function makeInput(value = ""): HTMLInputElement {
    document.body.innerHTML = '<input id="composer">';
    const input = document.getElementById("composer") as HTMLInputElement;
    input.value = value;
    return input;
}

describe("ChatComposer", () => {
    beforeEach(() => {
        toasted.length = 0;
    });

    afterEach(() => {
        document.body.innerHTML = "";
    });

    test("a sent message clears the input", () => {
        const input = makeInput("hello");
        const composer = new ChatComposer(input, () => true);

        expect(composer.submit()).toBe(true);
        expect(input.value).toBe("");
    });

    test("a message that never left is kept in the box", () => {
        // send() is false while the socket is reconnecting.
        const input = makeInput("hello");
        const composer = new ChatComposer(input, () => false);

        expect(composer.submit()).toBe(false);
        expect(input.value).toBe("hello");
    });

    test("whitespace is not sent", () => {
        const send = mock(() => true);
        const composer = new ChatComposer(makeInput("   "), send);

        expect(composer.submit()).toBe(false);
        expect(send.mock.calls.length).toBe(0);
    });

    test("the body is trimmed before it goes out", () => {
        const sent: { body: string }[] = [];
        new ChatComposer(makeInput("  hi  "), (payload) => {
            sent.push(payload);
            return true;
        }).submit();

        expect(sent).toEqual([{ body: "hi" }]);
    });

    test("a refusal gives the text back and says why", () => {
        const input = makeInput("hello");
        const composer = new ChatComposer(input, () => true);
        composer.submit();

        composer.reportRefusal("You're sending messages too quickly.");

        expect(input.value).toBe("hello");
        expect(toasted).toEqual(["You're sending messages too quickly."]);
    });

    test("a refusal with no detail still says something", () => {
        const composer = new ChatComposer(makeInput("hello"), () => true);
        composer.submit();

        composer.reportRefusal(undefined);

        expect(toasted[0]).toContain("couldn't be sent");
    });

    test("a refusal does not clobber a message already being typed", () => {
        const input = makeInput("first");
        const composer = new ChatComposer(input, () => true);
        composer.submit();
        input.value = "second, still typing";

        composer.reportRefusal("nope");

        expect(input.value).toBe("second, still typing");
        expect(toasted).toEqual(["nope"]);
    });

    test("refusals are handed back oldest first", () => {
        // The shape a rate limit produces: several sends in flight, and the
        // refusal that comes back is about the earliest one.
        const input = makeInput("");
        const composer = new ChatComposer(input, () => true);
        for (const body of ["one", "two", "three"]) {
            input.value = body;
            composer.submit();
        }

        composer.reportRefusal("too fast");

        expect(input.value).toBe("one");
    });

    test("a confirmed message is not handed back by a later refusal", () => {
        const input = makeInput("");
        const composer = new ChatComposer(input, () => true);
        for (const body of ["one", "two"]) {
            input.value = body;
            composer.submit();
        }

        composer.confirm("one");
        input.value = "";
        composer.reportRefusal("too fast");

        expect(input.value).toBe("two");
    });

    test("confirming something that was never sent changes nothing", () => {
        const input = makeInput("mine");
        const composer = new ChatComposer(input, () => true);
        composer.submit();

        // Someone else's message on the same broadcast.
        composer.confirm("a stranger's message");
        composer.reportRefusal("nope");

        expect(input.value).toBe("mine");
    });

    test("a refusal with nothing in flight still reports", () => {
        // A scope refusal on a frame the composer did not send, e.g. a ping path
        // or a frame from another part of the page.
        const input = makeInput("");
        const composer = new ChatComposer(input, () => true);

        composer.reportRefusal("This credential isn't allowed to send here.");

        expect(input.value).toBe("");
        expect(toasted).toEqual(["This credential isn't allowed to send here."]);
    });

    test("a blank detail falls back rather than toasting an empty string", () => {
        const composer = new ChatComposer(makeInput("x"), () => true);
        composer.submit();

        composer.reportRefusal("   ");

        expect(toasted[0]).toContain("couldn't be sent");
    });
});

describe("toastRefusal", () => {
    beforeEach(() => {
        toasted.length = 0;
    });

    test("reports when there is no composer to give the text back to", () => {
        toastRefusal("This credential isn't allowed to send here.");

        expect(toasted).toEqual(["This credential isn't allowed to send here."]);
    });

    test("falls back when the server sent no detail", () => {
        toastRefusal(null);

        expect(toasted[0]).toContain("couldn't be sent");
    });
});
