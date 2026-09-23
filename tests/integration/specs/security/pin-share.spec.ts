/**
 * GOALS, pin -> pin: the sender shares coordinates (+ optional bundled fields) as a suggestion; on
 * accept it becomes the recipient's own independent pin, and the recipient never gets access to the
 * sender's actual pin, before or after acceptance.
 */

import type { Page } from "@playwright/test";

import { requireAccount, SHAREE_ROLE, SHARER_ROLE } from "../../lib/accounts.js";
import type { ApiClient, CreatedPin } from "../../lib/api-client.js";
import { resourceName } from "../../lib/env.js";
import { expect, ifSharingPair, test } from "../../lib/fixtures.js";
import { ensureFriends } from "../../lib/friendship.js";
import {
    acceptShare,
    freshMarker,
    friendProfileId,
    newSharesAt,
    pinShareRoutes,
    postPinShare,
    receivedShareIds,
    sessionPost,
    sharePin,
    storedCoordinates,
    type DeliveredShare,
} from "../../lib/pin-share.js";
import { containsMarker, expectIndistinguishableFromMissing, expectNotServerError, MISSING_SLUG } from "../../lib/security.js";

const LIVE_NAME_CONFLICT =
    '"The recipient never gets access to the sender\'s actual pin, before or after acceptance" / "never a live reference" vs ' +
    "templates/dashboard/pages/pin_share/detail.html:16 (share.safe_place_label -> models/pin_share/model.py place_label reads pin.display_label live) " +
    "and controllers/memories.py:141-161 + partials/memories/_sharing_received.html:10 (group.pin.effective_name)";

/** A share id no recipient holds; ids are sequential integers. */
const MISSING_SHARE_ID = 2_147_000_000;

interface SharedPin {
    pin: CreatedPin;
    marker: string;
    share: DeliveredShare;
}

interface ShareOptions {
    /** Extra share-form fields, e.g. `custom_name`. */
    extra?: Record<string, string>;
    /** Pin fields to set before the share is sent. */
    before?: Record<string, unknown>;
}

async function shareFreshPin(sharerApi: ApiClient, shareeApi: ApiClient, sharerPage: Page, shareePage: Page, label: string, options: ShareOptions = {}): Promise<SharedPin> {
    await ensureFriends(sharerApi, shareeApi);
    const marker = freshMarker(label);
    const pin = await sharerApi.createPin({
        name: `${resourceName("pin share")} ${marker}`,
        description: `private notes ${marker}notes`,
    });
    if (options.before) {
        await sharerApi.json("patch", `pins/${pin.slug}/`, options.before);
    }
    const share = await sharePin(sharerApi, sharerPage, shareePage, pin, requireAccount(SHAREE_ROLE).username, options.extra);
    return { pin, marker, share };
}

/** Every route that could hand the recipient the sender's pin answers exactly as for a pin that never existed. */
async function expectSenderPinUnreachable(shareeApi: ApiClient, shareePage: Page, pin: CreatedPin, marker: string, when: string): Promise<void> {
    await expectIndistinguishableFromMissing(await shareeApi.get(`pins/${pin.slug}/`), await shareeApi.get(`pins/${MISSING_SLUG}/`), `the sender's pin over the API ${when}`);
    await expectIndistinguishableFromMissing(
        await shareePage.request.get(`/dashboard/map/pins/${pin.slug}/`),
        await shareePage.request.get(`/dashboard/map/pins/${MISSING_SLUG}/`),
        `the sender's pin in the map JSON ${when}`,
    );

    const page = await shareePage.request.get(`/dashboard/map/pin/${pin.slug}/`);
    await expectNotServerError(page, `the sender's pin detail page ${when}`);
    expect(page.status(), `the recipient loaded the sender's pin detail page ${when}`).toBe(404);
    // The 404 page echoes the requested slug, which carries the marker; the recipient typed it.
    const body = (await page.text()).split(pin.slug).join("[slug]");
    expect(containsMarker(body, marker), `the recipient's 404 for the sender's pin still names it ${when}`).toBeFalsy();
}

test.describe("pin -> pin share: the recipient never gets the sender's pin", () => {
    ifSharingPair()("the recipient cannot open the sender's pin by any route, before or after accepting", async ({ sharerApi, shareeApi, sharerPage, shareePage }) => {
        const { pin, marker, share } = await shareFreshPin(sharerApi, shareeApi, sharerPage, shareePage, "reach");

        expect((await sharerApi.get(`pins/${pin.slug}/`)).status(), "the sender cannot read their own pin, so the recipient's 404s would prove nothing").toBe(200);
        await expectSenderPinUnreachable(shareeApi, shareePage, pin, marker, "while the share is pending");

        const copySlug = await acceptShare(shareeApi, share.shareId);
        expect(copySlug, "accepting handed the recipient the sender's own pin slug").not.toBe(pin.slug);
        expect((await shareeApi.get(`pins/${copySlug}/`)).status(), "the recipient cannot read the pin acceptance created").toBe(200);
        await expectSenderPinUnreachable(shareeApi, shareePage, pin, marker, "after accepting");
    });

    ifSharingPair()("the accepted copy carries opted-in fields and none of the sender's private ones", async ({ sharerApi, shareeApi, sharerPage, shareePage }) => {
        const dateBuilt = "1901-02-03";
        const label = await sharerApi.json<{ uuid: string; name: string }>("post", "labels/", { name: `${resourceName("private label")} ${freshMarker("lbl")}`, kind: "tag" });
        sharerApi.track("label", label.uuid, () => sharerApi.delete(`labels/${label.uuid}/`));
        const plain = await shareFreshPin(sharerApi, shareeApi, sharerPage, shareePage, "copy", { before: { date_built: dateBuilt, label_uuids: [label.uuid] } });
        const senderView = await sharerApi.json<{ tags: Array<{ name: string }> }>("get", `pins/${plain.pin.slug}/`);
        expect(senderView.tags.map((tag) => tag.name), "the sender's pin never got the private label, so its absence on the copy would prove nothing").toContain(label.name);

        const customName = `${resourceName("shared as")} ${freshMarker("cust")}`;
        const named = await shareFreshPin(sharerApi, shareeApi, sharerPage, shareePage, "copyn", { extra: { custom_name: customName } });

        type Copy = { name: string | null; description: string; date_built: string | null; latitude: number; longitude: number; tags: Array<{ name: string }> };
        const plainCopy = await shareeApi.json<Copy>("get", `pins/${await acceptShare(shareeApi, plain.share.shareId)}/`);
        expect(plainCopy.date_built, "a site fact (date_built) did not travel with the share, so the absences below could mean nothing was copied at all").toBe(dateBuilt);
        expect(plainCopy.latitude, "the copy is not at the shared coordinates").toBeCloseTo(plain.share.at.lat, 5);
        expect(plainCopy.longitude, "the copy is not at the shared coordinates").toBeCloseTo(plain.share.at.lng, 5);
        expect(containsMarker(String(plainCopy.name), plain.marker), "the copy carries the sender's private pin name, which they never opted to share").toBeFalsy();
        expect(containsMarker(String(plainCopy.description), plain.marker), "the copy carries the sender's private notes").toBeFalsy();
        expect(plainCopy.tags.map((tag) => tag.name), "the copy carries the sender's private label").not.toContain(label.name);

        const namedCopy = await shareeApi.json<Copy>("get", `pins/${await acceptShare(shareeApi, named.share.shareId)}/`);
        expect(namedCopy.name, "the name the sender opted to share did not reach the copy").toBe(customName);
        expect(containsMarker(String(namedCopy.description), named.marker), "the copy carries the sender's private notes").toBeFalsy();
    });

    ifSharingPair()("a copy accepted later carries the facts as they were shared, not the sender's edits in between", async ({ sharerApi, shareeApi, sharerPage, shareePage }) => {
        test.fail();
        test.info().annotations.push({
            type: "goals-conflict",
            description:
                '"sender shares coordinates (+ optional bundled fields) as a suggestion ... never a live reference" vs ' +
                "services/sharing/pin_sharing.py:136-172 (create_pin_from_share reads share.pin's date_built/security at accept time, not a snapshot taken at share time)",
        });
        // Its precondition (date_built travels at all) is asserted by "the accepted copy carries opted-in fields ...".
        const { pin, share } = await shareFreshPin(sharerApi, shareeApi, sharerPage, shareePage, "snap", { before: { date_built: "1901-02-03" } });
        await sharerApi.json("patch", `pins/${pin.slug}/`, { date_built: "1977-07-07" });

        const copy = await shareeApi.json<{ date_built: string | null }>("get", `pins/${await acceptShare(shareeApi, share.shareId)}/`);
        expect(copy.date_built, "the copy took the sender's post-share edit: a pending share reads through to the live pin until it is accepted").toBe("1901-02-03");
    });

    ifSharingPair()("the sender's later edits never reach the recipient's accepted pin", async ({ sharerApi, shareeApi, sharerPage, shareePage }) => {
        const { pin, share } = await shareFreshPin(sharerApi, shareeApi, sharerPage, shareePage, "indep", {
            before: { date_built: "1901-02-03", security: { fences: "some" } },
        });
        const copySlug = await acceptShare(shareeApi, share.shareId);

        type Detail = { name: string; description: string; date_built: string | null; latitude: number; longitude: number; security: Record<string, string> };
        const before = await shareeApi.json<Detail>("get", `pins/${copySlug}/`);
        expect(before.date_built, "the copy did not receive date_built, so an unchanged value below would prove nothing").toBe("1901-02-03");
        expect(before.security.fences, "the copy did not receive the fences indicator").toBe("some");

        const renameMarker = freshMarker("indepren");
        const renamed = `${resourceName("pin share renamed")} ${renameMarker}`;
        const edited = await sharerApi.patch(`pins/${pin.slug}/`, {
            name: renamed,
            description: `edited notes ${freshMarker("indepdesc")}`,
            date_built: "1950-06-15",
            security: { fences: "everywhere", cameras: "everywhere" },
            latitude: share.at.lat + 0.005,
            longitude: share.at.lng + 0.005,
            confirm_wiki_loss: true,
        });
        expect(edited.status(), `the sender's edit answered ${edited.status()}: ${(await edited.text()).slice(0, 200)}`).toBe(200);
        const senderNow = await sharerApi.json<Detail>("get", `pins/${pin.slug}/`);
        expect(senderNow.name, "the sender's rename did not take, so the recipient's unchanged copy would prove nothing").toBe(renamed);
        expect(senderNow.latitude, "the sender's move did not take").toBeCloseTo(share.at.lat + 0.005, 5);

        const after = await shareeApi.json<Detail>("get", `pins/${copySlug}/`);
        // Not compared for equality: an unnamed copy's display name may legitimately fill in from its wiki meanwhile.
        expect(containsMarker(String(after.name), renameMarker), "renaming the sender's pin renamed the recipient's copy").toBeFalsy();
        expect(after.description, "editing the sender's notes changed the recipient's copy").toBe(before.description);
        expect(after.date_built, "editing the sender's date_built changed the recipient's copy").toBe("1901-02-03");
        expect(after.security, "editing the sender's security indicators changed the recipient's copy").toEqual(before.security);
        expect(after.latitude, "moving the sender's pin moved the recipient's copy").toBeCloseTo(before.latitude, 6);
        expect(after.longitude, "moving the sender's pin moved the recipient's copy").toBeCloseTo(before.longitude, 6);
    });

    ifSharingPair()("the recipient's share preview is reachable before and after accepting", async ({ sharerApi, shareeApi, sharerPage, shareePage }) => {
        // Preconditions for the two expected-failure tests below, kept apart so a broken setup cannot pass as their expected failure.
        const { pin, share } = await shareFreshPin(sharerApi, shareeApi, sharerPage, shareePage, "prev");
        const sharer = requireAccount(SHARER_ROLE).username;

        const pending = await shareePage.request.get(pinShareRoutes.detail(share.shareId));
        expect(pending.status()).toBe(200);
        expect(await pending.text(), "the preview does not name who shared it").toContain(`<strong>${sharer}</strong> shared a place with you`);
        expect([...(await receivedShareIds(shareePage))], "the received list does not link the share").toContain(share.shareId);

        const renamed = `${resourceName("pin share renamed")} ${freshMarker("prevren")}`;
        await sharerApi.json("patch", `pins/${pin.slug}/`, { name: renamed });
        expect((await sharerApi.json<{ name: string }>("get", `pins/${pin.slug}/`)).name, "the sender's rename did not take").toBe(renamed);

        await acceptShare(shareeApi, share.shareId);
        const accepted = await shareePage.request.get(pinShareRoutes.detail(share.shareId));
        expect(accepted.status(), "the preview disappeared after accepting").toBe(200);
        expect(await accepted.text()).toContain("This share is accepted.");
        expect([...(await receivedShareIds(shareePage))], "the received list dropped the share after accepting").toContain(share.shareId);
    });

    ifSharingPair()("a pending share's preview does not follow the sender's later rename", async ({ sharerApi, shareeApi, sharerPage, shareePage }) => {
        test.fail();
        test.info().annotations.push({ type: "goals-conflict", description: LIVE_NAME_CONFLICT });
        const { pin, share } = await shareFreshPin(sharerApi, shareeApi, sharerPage, shareePage, "pend");

        const later = freshMarker("pendren");
        await sharerApi.json("patch", `pins/${pin.slug}/`, { name: `${resourceName("pin share renamed")} ${later}` });

        const preview = await (await shareePage.request.get(pinShareRoutes.detail(share.shareId))).text();
        expect(containsMarker(preview, later), "the pending share's preview shows the name the sender gave their pin after sharing it - a live reference").toBeFalsy();
        const received = await (await shareePage.request.get(pinShareRoutes.received)).text();
        expect(containsMarker(received, later), "the received-shares list shows the sender's post-share rename - a live reference").toBeFalsy();
    });

    ifSharingPair()("after accepting, the sender's rename reaches none of the recipient's share surfaces", async ({ sharerApi, shareeApi, sharerPage, shareePage }) => {
        test.fail();
        test.info().annotations.push({ type: "goals-conflict", description: LIVE_NAME_CONFLICT });
        const { pin, share } = await shareFreshPin(sharerApi, shareeApi, sharerPage, shareePage, "acc");
        await acceptShare(shareeApi, share.shareId);

        const later = freshMarker("accren");
        await sharerApi.json("patch", `pins/${pin.slug}/`, { name: `${resourceName("pin share renamed")} ${later}` });

        const preview = await (await shareePage.request.get(pinShareRoutes.detail(share.shareId))).text();
        expect(containsMarker(preview, later), "an accepted share's detail page shows the sender's current pin name - a live reference into their pin").toBeFalsy();
        const received = await (await shareePage.request.get(pinShareRoutes.received)).text();
        expect(containsMarker(received, later), "the received-shares list shows the sender's current pin name after acceptance").toBeFalsy();
    });
});

test.describe("pin -> pin share: consent is enforced on both ends", () => {
    ifSharingPair()("pins can only be shared with friends", async ({ api, page, sharerApi, shareeApi, sharerPage, shareePage }) => {
        await ensureFriends(sharerApi, shareeApi);
        const sharee = requireAccount(SHAREE_ROLE).username;

        // Control: the friend path works and yields the sharee's profile id.
        const friendPin = await sharerApi.createPin({ name: `${resourceName("friend share")} ${freshMarker("frnd")}` });
        const friendShare = await sharePin(sharerApi, sharerPage, shareePage, friendPin, sharee);
        expect(friendShare.shareId).toBeGreaterThan(0);
        const shareeProfileId = await friendProfileId(sharerPage, friendPin.slug, sharee);

        const strangerMarker = freshMarker("strg");
        const strangerPin = await api.createPin({ name: `${resourceName("stranger share")} ${strangerMarker}` });
        const strangerAt = await storedCoordinates(api, strangerPin.slug);
        const before = await receivedShareIds(shareePage);

        const refused = await postPinShare(page, strangerPin.slug, shareeProfileId);
        await expectNotServerError(refused, "a non-friend sending a pin share");
        expect(refused.status(), `a non-friend's pin share answered ${refused.status()} rather than 403`).toBe(403);

        expect(await newSharesAt(shareePage, before, strangerAt), "a refused share still reached the recipient").toHaveLength(0);
        const inbox = await shareeApi.get("notifications/", { limit: 50 });
        expect(inbox.status()).toBe(200);
        expect(containsMarker(await inbox.text(), strangerMarker), "a refused share still notified the recipient").toBeFalsy();
    });

    ifSharingPair()("a share can only be seen or answered by its recipient", async ({ api, page, sharerApi, shareeApi, sharerPage, shareePage }) => {
        const { share } = await shareFreshPin(sharerApi, shareeApi, sharerPage, shareePage, "answ");

        for (const action of ["accept", "reject"]) {
            await expectIndistinguishableFromMissing(
                await api.post(`pin-shares/${share.shareId}/respond/`, { action }),
                await api.post(`pin-shares/${MISSING_SHARE_ID}/respond/`, { action }),
                `a stranger's ${action} of someone else's share over the API`,
            );
        }

        const view = await page.request.get(pinShareRoutes.detail(share.shareId));
        expect(view.status(), "a stranger opened someone else's share preview").toBe(404);
        const answer = await sessionPost(page, pinShareRoutes.respond(share.shareId), { action: "accept" });
        await expectNotServerError(answer, "a stranger answering a share from the web");
        expect(answer.status(), `a stranger answering someone else's share from the web answered ${answer.status()}`).toBe(404);

        const still = await (await shareePage.request.get(pinShareRoutes.detail(share.shareId))).text();
        expect(still, "a stranger's answer changed the share's state").toContain("Accept and add to my map");

        await acceptShare(shareeApi, share.shareId);
        const again = await shareeApi.post(`pin-shares/${share.shareId}/respond/`, { action: "accept" });
        expect(again.status(), "answering an already-accepted share ran the acceptance path again").toBe(400);
    });
});
