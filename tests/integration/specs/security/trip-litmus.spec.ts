/**
 * GOALS litmus test: if editing a pin's fields ever changes a different user's view (a trip
 * activity, a game, a list), that surface is referencing the pin instead of a copy or the wiki.
 */

import { requireAccount, SHAREE_ROLE } from "../../lib/accounts.js";
import type { ApiClient, CreatedPin } from "../../lib/api-client.js";
import { resourceName } from "../../lib/env.js";
import { expect, ifSharingPair, test } from "../../lib/fixtures.js";
import { ensureFriends } from "../../lib/friendship.js";
import { freshMarker } from "../../lib/pin-share.js";
import { containsMarker, expectIndistinguishableFromMissing, MISSING_SLUG } from "../../lib/security.js";

interface ActivityRow {
    id: number;
    effective_title: string;
    latitude: number | null;
    longitude: number | null;
    location_hidden: boolean;
    pin_slug: string | null;
}

interface MapPoint {
    activity_id: number | null;
    label: string;
    lat: number;
    lng: number;
}

interface SharedTrip {
    pin: CreatedPin;
    marker: string;
    tripSlug: string;
    activityId: number;
    at: { lat: number; lng: number };
}

const LITMUS_QUOTE =
    '"if editing a pin\'s fields ever causes a different user\'s view (a trip activity, a game, a list) to change, that\'s a bug" / ' +
    '"Display data should still come from the right place (the Wiki, or data consensually copied and stored separately), not a live pin reference"';

/** Sharer puts a fresh pin on a new trip as an untitled activity; sharee is invited and joins. */
async function tripWithPinActivity(sharerApi: ApiClient, shareeApi: ApiClient, label: string): Promise<SharedTrip> {
    await ensureFriends(sharerApi, shareeApi);
    const marker = freshMarker(label);
    const pin = await sharerApi.createPin({ name: `${resourceName("trip litmus")} ${marker}` });
    const detail = await sharerApi.json<{ latitude: number; longitude: number }>("get", `pins/${pin.slug}/`);

    const trip = await sharerApi.json<{ slug: string }>("post", "trips/", { name: resourceName(`trip litmus ${marker}`) });
    sharerApi.track("trip", trip.slug, () => sharerApi.delete(`trips/${trip.slug}/`));

    const activity = await sharerApi.post(`trips/${trip.slug}/activities/`, { pin_slug: pin.slug });
    expect(activity.status(), `adding the pin as a trip activity answered ${activity.status()}: ${(await activity.text()).slice(0, 200)}`).toBe(201);
    const activityId = ((await activity.json()) as { id: number }).id;

    const invite = await sharerApi.post(`trips/${trip.slug}/members/`, { username: requireAccount(SHAREE_ROLE).username });
    expect([200, 201], `inviting the sharee answered ${invite.status()}: ${(await invite.text()).slice(0, 200)}`).toContain(invite.status());
    const join = await shareeApi.post(`trips/${trip.slug}/join/`);
    expect(join.status(), `joining the trip answered ${join.status()}: ${(await join.text()).slice(0, 200)}`).toBe(200);

    return { pin, marker, tripSlug: trip.slug, activityId, at: { lat: Number(detail.latitude), lng: Number(detail.longitude) } };
}

async function memberActivity(member: ApiClient, trip: SharedTrip): Promise<ActivityRow> {
    const page = await member.json<{ results: ActivityRow[] }>("get", `trips/${trip.tripSlug}/activities/`);
    const row = page.results.find((candidate) => candidate.id === trip.activityId);
    expect(row, "the joined member's itinerary does not list the activity at all").toBeTruthy();
    return row as ActivityRow;
}

async function memberMapPoint(member: ApiClient, trip: SharedTrip): Promise<MapPoint | undefined> {
    const map = await member.json<{ points: MapPoint[] }>("get", `trips/${trip.tripSlug}/map/`);
    return map.points.find((point) => point.activity_id === trip.activityId);
}

async function editPin(owner: ApiClient, pinSlug: string, edits: Record<string, unknown>): Promise<void> {
    const response = await owner.patch(`pins/${pinSlug}/`, { ...edits, confirm_wiki_loss: true });
    expect(response.status(), `the owner's pin edit answered ${response.status()}: ${(await response.text()).slice(0, 200)}`).toBe(200);
}

test.describe("editing a pin never changes what trip members see", () => {
    ifSharingPair()("a joined member sees the activity but never the pin, and outsiders see neither", async ({ api, sharerApi, shareeApi }) => {
        // Preconditions for the expected-failure tests below, kept apart so a broken setup cannot pass as their expected failure.
        const trip = await tripWithPinActivity(sharerApi, shareeApi, "pre");

        const row = await memberActivity(shareeApi, trip);
        expect(containsMarker(row.effective_title, trip.marker), `the member's activity title "${row.effective_title}" does not show the place at all, so an unchanged title would prove nothing`).toBeTruthy();
        expect(row.location_hidden, "the activity's location is hidden from the member, so unchanged coordinates would prove nothing").toBe(false);
        expect(row.latitude, "the member sees no coordinates for the activity").toBeCloseTo(trip.at.lat, 5);
        expect(row.longitude, "the member sees no coordinates for the activity").toBeCloseTo(trip.at.lng, 5);
        expect(row.pin_slug, "the activity hands the member the owner's pin slug").toBeNull();

        const point = await memberMapPoint(shareeApi, trip);
        expect(point, "the member's trip map has no marker for the activity").toBeTruthy();
        expect(containsMarker(point?.label ?? "", trip.marker), "the member's map marker is not labelled with the place").toBeTruthy();

        await expectIndistinguishableFromMissing(await shareeApi.get(`pins/${trip.pin.slug}/`), await shareeApi.get(`pins/${MISSING_SLUG}/`), "a trip member reading the activity's pin");
        for (const path of ["activities/", "map/"]) {
            await expectIndistinguishableFromMissing(
                await api.get(`trips/${trip.tripSlug}/${path}`),
                await api.get(`trips/${MISSING_SLUG}/${path}`),
                `a non-member reading trips/{slug}/${path}`,
            );
        }

        const renameMarker = freshMarker("preren");
        const renamed = `${resourceName("trip litmus renamed")} ${renameMarker}`;
        await editPin(sharerApi, trip.pin.slug, { name: renamed, latitude: trip.at.lat + 0.005, longitude: trip.at.lng + 0.005 });
        const owner = await sharerApi.json<{ name: string; latitude: number }>("get", `pins/${trip.pin.slug}/`);
        expect(owner.name, "the owner's rename did not take, so the member's unchanged view would prove nothing").toBe(renamed);
        expect(owner.latitude, "the owner's move did not take").toBeCloseTo(trip.at.lat + 0.005, 5);

        await expectIndistinguishableFromMissing(await shareeApi.get(`pins/${trip.pin.slug}/`), await shareeApi.get(`pins/${MISSING_SLUG}/`), "a trip member reading the pin after it was edited");
        for (const path of ["activities/", "map/"]) {
            const outsider = await api.get(`trips/${trip.tripSlug}/${path}`);
            expect(outsider.status()).toBe(404);
            expect(containsMarker(await outsider.text(), renameMarker), "a non-member's 404 names the edited pin").toBeFalsy();
        }
    });

    ifSharingPair()("renaming the pin does not rename the member's activity or map marker", async ({ sharerApi, shareeApi }) => {
        test.fail();
        test.info().annotations.push({
            type: "goals-conflict",
            description: `${LITMUS_QUOTE} vs models/trips/model.py:314 (TripActivity.effective_title reads self.pin.display_label live) -> services/trips/trip_activities.py:315 (API effective_title) and services/trips/trip_map.py:44 (map label)`,
        });
        const trip = await tripWithPinActivity(sharerApi, shareeApi, "ren");
        const baselineTitle = (await memberActivity(shareeApi, trip)).effective_title;
        const baselineLabel = (await memberMapPoint(shareeApi, trip))?.label;

        const renameMarker = freshMarker("renlater");
        await editPin(sharerApi, trip.pin.slug, { name: `${resourceName("trip litmus renamed")} ${renameMarker}` });

        const title = (await memberActivity(shareeApi, trip)).effective_title;
        const label = (await memberMapPoint(shareeApi, trip))?.label;
        expect(containsMarker(title, renameMarker), `the owner renaming their private pin renamed another member's activity to "${title}"`).toBeFalsy();
        expect(title, "the member's activity title changed when the owner edited their pin").toBe(baselineTitle);
        expect(label, "the member's map marker label changed when the owner edited their pin").toBe(baselineLabel);
    });

    ifSharingPair()("moving the pin does not move the member's activity or map marker", async ({ sharerApi, shareeApi }) => {
        test.fail();
        test.info().annotations.push({
            type: "goals-conflict",
            description: `${LITMUS_QUOTE} vs services/trips/trip_legs.py:35 (activity_coords reads act.pin.effective_latitude live, ahead of the activity's own location) -> TripActivitySerializer latitude/longitude and services/trips/trip_map.py lat/lng`,
        });
        const trip = await tripWithPinActivity(sharerApi, shareeApi, "mov");
        const baseline = await memberActivity(shareeApi, trip);
        const baselinePoint = await memberMapPoint(shareeApi, trip);

        await editPin(sharerApi, trip.pin.slug, { latitude: trip.at.lat + 0.005, longitude: trip.at.lng + 0.005 });

        const row = await memberActivity(shareeApi, trip);
        const point = await memberMapPoint(shareeApi, trip);
        expect(row.latitude, "the owner moving their private pin moved another member's activity").toBe(baseline.latitude);
        expect(row.longitude, "the owner moving their private pin moved another member's activity").toBe(baseline.longitude);
        expect(point?.lat, "the owner moving their private pin moved another member's map marker").toBe(baselinePoint?.lat);
        expect(point?.lng, "the owner moving their private pin moved another member's map marker").toBe(baselinePoint?.lng);
    });
});
