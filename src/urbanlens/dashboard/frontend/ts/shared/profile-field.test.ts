import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { avatarSrc, saveAvatar, saveProfileField, showAvatar, showSavedAvatar, showUserAvatar, usernameAvailability, withVersion } from "./profile-field";

const realFetch = globalThis.fetch;
const realToken = window.csrftoken;
let respond: () => Response = () => new Response(JSON.stringify({ ok: true }), { status: 200 });
let sent: { url: string; headers: HeadersInit | undefined; body: Record<string, FormDataEntryValue> } | null = null;

beforeAll(() => {
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL, init?: RequestInit) => {
        sent = { url: String(input), headers: init?.headers, body: init?.body instanceof FormData ? Object.fromEntries(init.body) : {} };
        return respond();
    }, realFetch);
});

afterAll(() => {
    globalThis.fetch = realFetch;
    window.csrftoken = realToken;
});

beforeEach(() => {
    sent = null;
    window.csrftoken = "tok123";
    respond = () => new Response(JSON.stringify({ ok: true }), { status: 200 });
});

describe("saveProfileField", () => {
    test("posts the field with the CSRF header", async () => {
        expect(await saveProfileField("/f/", "username", "owl")).toEqual({ ok: true });
        expect(sent?.body).toEqual({ field: "username", value: "owl" });
        expect(sent?.headers).toEqual({ "X-CSRFToken": "tok123" });
    });

    test("a refusal in a 200 and a refusal in a 400 read the same", async () => {
        respond = () => new Response(JSON.stringify({ ok: false, error: "Not a date." }), { status: 200 });
        expect(await saveProfileField("/f/", "birthday", "x")).toEqual({ ok: false, refused: true, error: "Not a date." });
        respond = () => new Response(JSON.stringify({ error: "Not a date." }), { status: 400 });
        expect(await saveProfileField("/f/", "birthday", "x")).toEqual({ ok: false, refused: true, error: "Not a date." });
    });

    test("a server error is a failed save, not a refusal of the value", async () => {
        respond = () => new Response("<h1>Server Error</h1>", { status: 500 });
        expect(await saveProfileField("/f/", "bio", "x")).toEqual({ ok: false, refused: false, error: undefined });
    });

    test("a server that can't save right now still says why", async () => {
        respond = () => new Response(JSON.stringify({ error: "Our antivirus scanner is temporarily unavailable." }), { status: 503 });
        expect(await saveProfileField("/f/", "avatar", "x")).toEqual({ ok: false, refused: false, error: "Our antivirus scanner is temporarily unavailable." });
    });

    test("an emoji avatar sends its animal and colour", async () => {
        await saveAvatar("/f/", { kind: "emoji", animal: "fox", color: "red" });
        expect(sent?.body).toEqual({ field: "avatar_emoji", animal: "fox", color: "red" });
    });
});

describe("usernameAvailability", () => {
    test("says why a name is taken, and null when the check failed", async () => {
        respond = () => new Response(JSON.stringify({ available: false, reason: "Taken." }), { status: 200 });
        expect(await usernameAvailability("/f/", "a b")).toEqual({ available: false, reason: "Taken." });
        expect(sent?.url).toBe("/f/?field=username&value=a%20b");
        respond = () => new Response("", { status: 500 });
        expect(await usernameAvailability("/f/", "owl")).toBeNull();
    });
});

describe("withVersion", () => {
    test("adds a v parameter to a path, keeping what it already has", () => {
        expect(withVersion("/media/a.png", 7)).toBe("/media/a.png?v=7");
        expect(withVersion("/media/a.png?sig=abc&v=1#x", 7)).toBe("/media/a.png?sig=abc&v=7#x");
    });

    test("leaves another origin's address on that origin and a blob or data URL alone", () => {
        expect(withVersion("https://cdn.example.com/a.png?x=1", 7)).toBe("https://cdn.example.com/a.png?x=1&v=7");
        expect(withVersion("blob:http://localhost/1234", 7)).toBe("blob:http://localhost/1234");
        expect(withVersion("data:image/png;base64,AAAA", 7)).toBe("data:image/png;base64,AAAA");
    });
});

describe("showAvatar", () => {
    test("replaces a placeholder with an image, and reloads an image in place", () => {
        document.body.innerHTML = `<div id="av">JM</div>`;
        showAvatar(document.getElementById("av"), "/a.png", "avatar");
        const img = document.getElementById("av");
        expect(img instanceof HTMLImageElement && img.className === "avatar").toBe(true);
        showAvatar(img, "/b.png", "avatar");
        expect(document.getElementById("av")).toBe(img);
        expect(img?.getAttribute("src")).toStartWith("/b.png?v=");
    });

    test("the image replacing a placeholder keeps the marks that make it the user's avatar", () => {
        document.body.innerHTML = `<span id="nav" class="initial" data-user-avatar="nav-img" data-avatar-alt="">J</span>`;
        showUserAvatar("/a.png");
        const img = document.getElementById("nav");
        expect(img instanceof HTMLImageElement && img.className === "nav-img" && img.alt === "" && img.dataset.userAvatar === "nav-img").toBe(true);
        // ... so the next one finds it again.
        showUserAvatar("/b.png");
        expect(document.getElementById("nav")?.getAttribute("src")).toStartWith("/b.png?v=");
    });
});

describe("avatarSrc", () => {
    test("lets through a path or an address on this page's origin, as a path that keeps its query and fragment", () => {
        expect(avatarSrc("/media/avatars/a.webp")).toBe("/media/avatars/a.webp");
        // This site's copy of a Gravatar is not under /media/.
        expect(avatarSrc("/media-copy/0123abcd/")).toBe("/media-copy/0123abcd/");
        expect(avatarSrc("/static/dashboard/g.png?s=200&d=identicon#top")).toBe("/static/dashboard/g.png?s=200&d=identicon#top");
        expect(avatarSrc("https://urbanlens.test/media/a.png?v=1")).toBe("/media/a.png?v=1");
        // A path without its slash is read against the origin, not the page's own path.
        expect(avatarSrc("media/a.png")).toBe("/media/a.png");
    });

    test("refuses every scheme but http and https, however it is spelled", () => {
        for (const address of [
            "javascript:alert(1)",
            "JaVaScRiPt:alert(1)",
            "  javascript:alert(1)",
            "java\nscript:alert(1)",
            "vbscript:msgbox(1)",
            "data:image/png;base64,AAAA",
            "data:image/svg+xml,<svg onload=alert(1)>",
            "file:///etc/passwd",
            "ftp://urbanlens.test/a.png",
        ]) {
            expect(avatarSrc(address)).toBeNull();
        }
    });

    test("refuses another origin, however it is dressed up", () => {
        for (const address of [
            "https://evil.example/media/a.png",
            "//evil.example/a.png",
            "/\\evil.example/a.png",
            "https://urbanlens.test.evil.example/a.png",
            "https://urbanlens.test@evil.example/a.png",
            "https://evil.example\\@urbanlens.test/a.png",
            // Same host, other scheme: not the page's origin.
            "http://urbanlens.test/a.png",
            "https://urbanlens.test:8443/a.png",
        ]) {
            expect(avatarSrc(address)).toBeNull();
        }
    });

    describe("on a site that serves uploads from their own origin", () => {
        const MEDIA = "https://media.example.org";

        test("lets through that origin, in full, and this page's origin still as a path", () => {
            expect(avatarSrc("https://media.example.org/media/avatars/a.webp", MEDIA)).toBe("https://media.example.org/media/avatars/a.webp");
            expect(avatarSrc("https://media.example.org/media/a.png?sig=abc&v=1#x", MEDIA)).toBe("https://media.example.org/media/a.png?sig=abc&v=1#x");
            expect(avatarSrc("/media/avatars/a.webp", MEDIA)).toBe("/media/avatars/a.webp");
            // The attribute is read as an origin: a trailing slash or a path on it does not narrow or widen it.
            expect(avatarSrc("https://media.example.org/a.png", "https://media.example.org/")).toBe("https://media.example.org/a.png");
            expect(avatarSrc("https://media.example.org/a.png", "https://media.example.org/media/")).toBe("https://media.example.org/a.png");
            // Nothing in front of the host comes along.
            expect(avatarSrc("https://user:secret@media.example.org/a.png", MEDIA)).toBe("https://media.example.org/a.png");
        });

        test("is that origin exactly: no other origin, subdomain, lookalike, scheme or port, and no wildcard", () => {
            for (const address of [
                "https://evil.example/media/a.png",
                "https://media.example.org.evil.example/a.png",
                "https://evil.media.example.org/a.png",
                "https://example.org/a.png",
                "https://media.example.org@evil.example/a.png",
                "//evil.example/a.png",
                "http://media.example.org/a.png",
                "https://media.example.org:8443/a.png",
            ]) {
                expect(avatarSrc(address, MEDIA)).toBeNull();
            }
            for (const wildcard of ["*", "https://*.example.org", "https://*", ".example.org", "example.org"]) {
                expect(avatarSrc("https://media.example.org/a.png", wildcard)).toBeNull();
                expect(avatarSrc("https://evil.example/a.png", wildcard)).toBeNull();
            }
        });

        test("still refuses every scheme but http and https, and a blob: nobody here made", () => {
            for (const address of ["javascript:alert(1)", "data:image/png;base64,AAAA", "file:///etc/passwd", "ftp://media.example.org/a.png", "blob:https://media.example.org/1234"]) {
                expect(avatarSrc(address, MEDIA)).toBeNull();
            }
        });

        test("is ignored when it is empty, is not an address, or is not an http(s) one: only this page's origin is let through", () => {
            for (const media of ["", "not an address", "javascript:alert(1)", "data:text/plain,x", "null", "ftp://media.example.org"]) {
                expect(avatarSrc("https://media.example.org/a.png", media)).toBeNull();
                expect(avatarSrc("javascript:alert(1)", media)).toBeNull();
                expect(avatarSrc("/media/a.png", media)).toBe("/media/a.png");
            }
            expect(avatarSrc("https://media.example.org/a.png")).toBeNull();
        });
    });

    test("refuses a blob: address this page did not make, and an address that is not one", () => {
        expect(avatarSrc("blob:https://urbanlens.test/1234")).toBeNull();
        expect(avatarSrc("blob:https://evil.example/1234")).toBeNull();
        expect(avatarSrc("https://")).toBeNull();
    });
});

describe("showAvatar refusing an address", () => {
    const warn = console.warn;
    let warned = 0;
    beforeEach(() => {
        warned = 0;
        console.warn = () => void warned++;
    });
    afterEach(() => {
        console.warn = warn;
    });

    test("leaves an image as it was", () => {
        document.body.innerHTML = `<img id="av" class="avatar" src="/media/old.png" alt="">`;
        const img = document.getElementById("av");
        for (const address of ["javascript:alert(1)", "data:image/svg+xml,<svg onload=alert(1)>", "https://evil.example/a.png", "blob:https://urbanlens.test/1234"]) {
            showAvatar(img, address, "avatar");
        }
        expect(document.getElementById("av")).toBe(img);
        expect(img?.getAttribute("src")).toBe("/media/old.png");
        expect(warned).toBe(4);
    });

    test("draws from the media origin it was given, and still refuses another", () => {
        document.body.innerHTML = `<div id="av" data-user-avatar="avatar">JM</div>`;
        showUserAvatar("https://evil.example/a.png", "https://media.example.org");
        expect(document.getElementById("av")?.tagName).toBe("DIV");
        showUserAvatar("https://media.example.org/media/a.png", "https://media.example.org");
        expect(document.getElementById("av")?.getAttribute("src")).toStartWith("https://media.example.org/media/a.png?v=");
        const img = document.getElementById("av");
        showAvatar(img, "https://media.example.org/media/b.png", "avatar", "Avatar", "https://media.example.org");
        expect(img?.getAttribute("src")).toStartWith("https://media.example.org/media/b.png?v=");
        expect(warned).toBe(1);
    });

    test("leaves a placeholder where it was", () => {
        document.body.innerHTML = `<div id="av" data-user-avatar="avatar">JM</div>`;
        showUserAvatar("javascript:alert(1)");
        expect(document.getElementById("av")?.tagName).toBe("DIV");
        expect(document.querySelector("img")).toBeNull();
    });
});

describe("showSavedAvatar", () => {
    const now = async () => undefined;
    const placeholders = () => {
        document.body.innerHTML = `
          <div id="hero" class="hero-placeholder" data-user-avatar="hero-img">J</div>
          <img id="nav" class="nav-img" data-user-avatar="nav-img" src="/media/old.png" alt="">`;
    };
    const srcs = () => ["hero", "nav"].map((id) => document.getElementById(id)?.getAttribute("src") ?? document.getElementById(id)?.tagName);
    let states: (Response | Error)[] = [];
    let asked = 0;

    beforeEach(() => {
        states = [];
        asked = 0;
        globalThis.fetch = Object.assign(async () => {
            asked++;
            const next = states.shift();
            if (next instanceof Error) throw next;
            return next ?? new Response(JSON.stringify({ avatar_url: "/media/old.png", avatar_pending: true }), { status: 200 });
        }, realFetch);
    });

    const state = (avatar_url: string | null, avatar_pending: boolean) => new Response(JSON.stringify({ avatar_url, avatar_pending }), { status: 200 });

    test("a stored picture is drawn from the address the save answered with", async () => {
        placeholders();
        expect(await showSavedAvatar("/f/", { kind: "emoji", animal: "owl", color: "teal" }, { ok: true, avatar_url: "/media/new.svg" }, { sleep: now })).toBe("shown");
        expect(srcs().every((src) => src?.startsWith("/media/new.svg?v="))).toBe(true);
        expect(asked).toBe(0);
    });

    test("a held Gravatar is drawn from its preview, then replaced by the stored copy once it exists", async () => {
        placeholders();
        states = [state("/media/old.png", true), state("/media/new.png", false)];
        const outcome = showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/g.png" }, { ok: true, avatar_url: "/media/old.png", avatar_pending: true }, { sleep: now });
        expect(srcs().every((src) => src?.startsWith("/static/g.png?v="))).toBe(true);
        expect(await outcome).toBe("published");
        expect(srcs().every((src) => src?.startsWith("/media/new.png?v="))).toBe(true);
        expect(asked).toBe(2);
    });

    test("an upload that was dropped puts the stored picture back", async () => {
        document.body.innerHTML = `
          <img id="hero" class="hero-img" data-user-avatar="hero-img" src="/media/old.png" alt="">
          <img id="nav" class="nav-img" data-user-avatar="nav-img" src="/media/old.png" alt="">`;
        states = [state("/media/old.png", false)];
        const outcome = await showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/g.png" }, { ok: true, avatar_url: "/media/old.png", avatar_pending: true }, { sleep: now });
        expect(outcome).toBe("unchanged");
        expect(srcs().every((src) => src?.startsWith("/media/old.png?v="))).toBe(true);
    });

    test("an upload that was dropped, with no picture stored, puts the placeholder back", async () => {
        document.body.innerHTML = `<div id="hero" class="hero-placeholder" data-user-avatar="hero-img">J</div><div id="nav" class="nav-placeholder" data-user-avatar="nav-img">J</div>`;
        states = [state(null, false)];
        const outcome = await showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/g.png" }, { ok: true, avatar_url: null, avatar_pending: true }, { sleep: now });
        expect(outcome).toBe("unchanged");
        expect(srcs()).toEqual(["DIV", "DIV"]);
        expect(document.getElementById("hero")?.textContent).toBe("J");
    });

    test("a held choice dropped after an earlier held one shows what is stored, not the earlier one's preview", async () => {
        placeholders();
        let release: () => void = () => undefined;
        const held = new Promise<void>((resolve) => (release = resolve));
        const first = showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/A.png" }, { ok: true, avatar_url: "/media/old.png", avatar_pending: true }, { sleep: () => held });
        expect(srcs().every((src) => src?.startsWith("/static/A.png?v="))).toBe(true);
        states = [state("/media/old.png", false)];
        const second = await showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/B.png" }, { ok: true, avatar_url: "/media/old.png", avatar_pending: true }, { sleep: now });
        expect(second).toBe("unchanged");
        expect(srcs().every((src) => src?.startsWith("/media/old.png?v="))).toBe(true);
        release();
        expect(await first).toBe("superseded");
    });

    test("with nothing stored, the placeholder an earlier held choice replaced comes back", async () => {
        document.body.innerHTML = `<div id="hero" class="hero-placeholder" data-user-avatar="hero-img">J</div>`;
        let release: () => void = () => undefined;
        const held = new Promise<void>((resolve) => (release = resolve));
        const first = showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/A.png" }, { ok: true, avatar_url: null, avatar_pending: true }, { sleep: () => held });
        states = [state(null, false)];
        await showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/B.png" }, { ok: true, avatar_url: null, avatar_pending: true }, { sleep: now });
        expect(document.getElementById("hero")?.tagName).toBe("DIV");
        release();
        await first;
    });

    test("the previous picture's address coming back with another query string is still the same picture", async () => {
        placeholders();
        states = [state("/media/old.png?sig=later", false)];
        const outcome = await showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/g.png" }, { ok: true, avatar_url: "/media/old.png?sig=first", avatar_pending: true }, { sleep: now });
        expect(outcome).toBe("unchanged");
    });

    test("an upload the browser cannot draw is left to the stored copy", async () => {
        placeholders();
        states = [state("/media/new.png", false)];
        const heic = new File([new Uint8Array([0])], "me.heic", { type: "image/heic" });
        const outcome = showSavedAvatar("/f/", { kind: "upload", file: heic }, { ok: true, avatar_url: null, avatar_pending: true }, { sleep: now });
        expect(srcs()).toEqual(["DIV", "/media/old.png"]);
        expect(await outcome).toBe("published");
        expect(srcs().every((src) => src?.startsWith("/media/new.png?v="))).toBe(true);
    });

    test("a wait that outlasts its tries leaves the chosen picture on show", async () => {
        placeholders();
        states = [state("/media/old.png", true), new Error("offline")];
        const outcome = await showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/g.png" }, { ok: true, avatar_url: "/media/old.png", avatar_pending: true }, { sleep: now, delaysMs: [1, 1, 1] });
        expect(outcome).toBe("waiting");
        expect(srcs().every((src) => src?.startsWith("/static/g.png?v="))).toBe(true);
    });

    test("a later choice ends the wait for an earlier one", async () => {
        placeholders();
        let release: () => void = () => undefined;
        const held = new Promise<void>((resolve) => (release = resolve));
        const first = showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/g.png" }, { ok: true, avatar_url: "/media/old.png", avatar_pending: true }, { sleep: () => held });
        expect(await showSavedAvatar("/f/", { kind: "emoji", animal: "owl", color: "teal" }, { ok: true, avatar_url: "/media/owl.svg" }, { sleep: now })).toBe("shown");
        release();
        expect(await first).toBe("superseded");
        expect(srcs().every((src) => src?.startsWith("/media/owl.svg?v="))).toBe(true);
        expect(asked).toBe(0);
    });

    describe("a preview made from the user's own file", () => {
        const png = (bytes = 8) => new File([new Uint8Array(bytes)], "me.png", { type: "image/png" });
        const realCreate = URL.createObjectURL;
        const realRevoke = URL.revokeObjectURL;
        let revoked: string[] = [];
        let made = 0;
        let blobAddress = "";

        beforeEach(() => {
            revoked = [];
            made = 0;
            // One a test: a preview that is never let go of (see the wait that goes unanswered) stays drawable.
            blobAddress = `blob:https://urbanlens.test/mine-${crypto.randomUUID()}`;
            URL.createObjectURL = () => {
                made++;
                return blobAddress;
            };
            URL.revokeObjectURL = (address: string) => void revoked.push(address);
        });

        afterEach(() => {
            URL.createObjectURL = realCreate;
            URL.revokeObjectURL = realRevoke;
        });

        test("is drawn as it is, then let go of once the stored copy has replaced it", async () => {
            placeholders();
            states = [state("/media/new.png", false)];
            const outcome = showSavedAvatar("/f/", { kind: "upload", file: png() }, { ok: true, avatar_url: "/media/old.png", avatar_pending: true }, { sleep: now });
            expect(srcs()).toEqual([blobAddress, blobAddress]);
            expect(avatarSrc(blobAddress)).toBe(blobAddress);
            expect(await outcome).toBe("published");
            expect(srcs().every((src) => src?.startsWith("/media/new.png?v="))).toBe(true);
            expect(revoked).toEqual([blobAddress]);
            // Revoked, so no longer one this page will draw from.
            expect(avatarSrc(blobAddress)).toBeNull();
        });

        test("stays on show, and stays drawable, while the wait goes unanswered", async () => {
            placeholders();
            states = [state("/media/old.png", true)];
            const outcome = await showSavedAvatar("/f/", { kind: "upload", file: png() }, { ok: true, avatar_url: "/media/old.png", avatar_pending: true }, { sleep: now, delaysMs: [1] });
            expect(outcome).toBe("waiting");
            expect(srcs()).toEqual([blobAddress, blobAddress]);
            expect(revoked).toEqual([]);
            expect(avatarSrc(blobAddress)).toBe(blobAddress);
        });

        test("is not made from an empty file or one the browser cannot draw", async () => {
            placeholders();
            states = [state("/media/new.png", false), state("/media/new.png", false)];
            const empty = showSavedAvatar("/f/", { kind: "upload", file: png(0) }, { ok: true, avatar_url: null, avatar_pending: true }, { sleep: now });
            expect(srcs()).toEqual(["DIV", "/media/old.png"]);
            expect(await empty).toBe("published");
            placeholders();
            const svg = new File(["<svg onload=alert(1)>"], "me.svg", { type: "image/svg+xml" });
            const held = showSavedAvatar("/f/", { kind: "upload", file: svg }, { ok: true, avatar_url: null, avatar_pending: true }, { sleep: now });
            expect(srcs()).toEqual(["DIV", "/media/old.png"]);
            await held;
            expect(made).toBe(0);
            expect(revoked).toEqual([]);
        });
    });

    test("a stored picture on the media origin is drawn from the address the save answered with, and the stored copy found by the wait", async () => {
        const MEDIA = "https://media.example.org";
        placeholders();
        expect(await showSavedAvatar("/f/", { kind: "emoji", animal: "owl", color: "teal" }, { ok: true, avatar_url: `${MEDIA}/media/new.svg` }, { sleep: now, mediaOrigin: MEDIA })).toBe("shown");
        expect(srcs().every((src) => src?.startsWith(`${MEDIA}/media/new.svg?v=`))).toBe(true);

        placeholders();
        states = [state(`${MEDIA}/media/new.png`, false)];
        const outcome = await showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/g.png" }, { ok: true, avatar_url: `${MEDIA}/media/old.png`, avatar_pending: true }, { sleep: now, mediaOrigin: MEDIA });
        expect(outcome).toBe("published");
        expect(srcs().every((src) => src?.startsWith(`${MEDIA}/media/new.png?v=`))).toBe(true);
    });

    test("a stored picture on another origin is not drawn when the page names no media origin", async () => {
        placeholders();
        const warn = console.warn;
        console.warn = () => undefined;
        expect(await showSavedAvatar("/f/", { kind: "emoji", animal: "owl", color: "teal" }, { ok: true, avatar_url: "https://media.example.org/media/new.svg" }, { sleep: now })).toBe("shown");
        console.warn = warn;
        expect(srcs()).toEqual(["DIV", "/media/old.png"]);
    });

    test("a Gravatar preview that is not an address this page may load is not drawn, and the stored copy still takes over", async () => {
        placeholders();
        states = [state("/media/new.png", false)];
        const warn = console.warn;
        console.warn = () => undefined;
        const outcome = showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "javascript:alert(1)" }, { ok: true, avatar_url: "/media/old.png", avatar_pending: true }, { sleep: now });
        console.warn = warn;
        expect(srcs()).toEqual(["DIV", "/media/old.png"]);
        expect(await outcome).toBe("published");
        expect(srcs().every((src) => src?.startsWith("/media/new.png?v="))).toBe(true);
    });
});
