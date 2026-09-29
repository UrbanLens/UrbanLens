/**
 * A trip member's activity title is data in every other member's browser, including the map popup.
 */

import { requireAccount, SHAREE_ROLE } from "../../lib/accounts.js";
import { resourceName } from "../../lib/env.js";
import { expect, ifSharingPair } from "../../lib/fixtures.js";
import { ensureFriends } from "../../lib/friendship.js";
import { expectCanaryNotInDom, markupCanary, uniqueMarker } from "../../lib/security.js";

ifSharingPair()("an activity title opens as text in another member's map popup", async ({ sharerApi, shareeApi, shareePage }) => {
    await ensureFriends(sharerApi, shareeApi);
    const marker = uniqueMarker("tripmap");

    const trip = await sharerApi.json<{ slug: string }>("post", "trips/", { name: resourceName(`trip map markup ${marker}`) });
    sharerApi.track("trip", trip.slug, () => sharerApi.delete(`trips/${trip.slug}/`));
    const activity = await sharerApi.post(`trips/${trip.slug}/activities/`, { title: markupCanary(marker), latitude: 41.73, longitude: -73.92, status: "confirmed" });
    expect(activity.status(), `adding the activity answered ${activity.status()}: ${(await activity.text()).slice(0, 200)}`).toBe(201);

    const invite = await sharerApi.post(`trips/${trip.slug}/members/`, { username: requireAccount(SHAREE_ROLE).username });
    expect([200, 201], `inviting the sharee answered ${invite.status()}`).toContain(invite.status());
    const join = await shareeApi.post(`trips/${trip.slug}/join/`);
    expect(join.status(), `joining the trip answered ${join.status()}`).toBe(200);

    await shareePage.goto(`/dashboard/trips/${trip.slug}/`);
    const pin = shareePage.locator("#trip-map .trip-map-marker").first();
    await expect(pin, "the member's map shows no marker for the activity, so there is no popup to open").toBeVisible();
    await pin.click();
    await expect(shareePage.locator(".leaflet-popup-content")).toContainText(marker);

    await expectCanaryNotInDom(shareePage, marker);
});
