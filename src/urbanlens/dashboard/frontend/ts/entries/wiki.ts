/**
 * The community wiki page (``pages/location/wiki.html``). Its values arrive on ``#wiki-page-config``; the map
 * is ``map-annotations.ts``'s.
 */

import { installActionsFab } from "../shared/actions-fab";
import { type BoundaryVote, installBoundaryVote } from "../shared/boundary-vote";
import { installExternalPanelFallbacks } from "../shared/external-panel-fallbacks";
import { initOnboardingTour } from "../shared/onboarding-tour";
import { WikiMedia } from "../shared/wiki-media";
import { installWikiEditForm, installWikiNoticePin, installWikiRename, rememberRecentWiki } from "../shared/wiki-page";

function initOnboarding(): void {
    initOnboardingTour({
        // Per user, not per location, like the pin page's.
        prefix: "ul_onboarding_v1_wiki",
        hostSelector: "#wiki-onboarding",
        cards: [
            {
                id: "shared-edits",
                icon: "edit_note",
                target: ".wiki-edit-btn",
                eyebrow: "Community page",
                title: "Suggest improvements that everyone can use",
                body: "Names, descriptions, dates, and security indicators here are shared with other users who pinned this location - your edits help everyone.",
                button: "Suggest an edit",
                watchSelector: ".wiki-edit-btn",
                action: () => document.querySelector<HTMLElement>(".wiki-edit-btn")?.click(),
                ready: () => !!document.querySelector(".wiki-edit-btn"),
            },
            {
                id: "community-detail-pins",
                icon: "add_location_alt",
                target: "#markup-pin-button",
                eyebrow: "Shared map details",
                title: "Add community detail pins for entrances and hazards",
                body: "Community detail pins appear on this wiki map for everyone. They are separate from your private detail pins on your personal pin page.",
                button: "Add detail pin",
                watchSelector: "#markup-pin-button",
                action: () => document.getElementById("markup-pin-button")?.click(),
                ready: () => !!document.getElementById("markup-pin-button"),
            },
            {
                id: "wiki-photos",
                icon: "photo_library",
                target: "#wiki-media-section",
                eyebrow: "Shared photos",
                title: "Photos here are visible to the whole community",
                body: 'Open the "Manage" tab in the Media section to upload exterior, access, and historical photos that help other users. GPS-tagged images can be positioned directly on the wiki map.',
                button: "Manage photos",
                watchSelector: '#wiki-media-tabs .media-tab[data-tab="manage"]',
                action: () => {
                    document.querySelector<HTMLElement>('#wiki-media-tabs .media-tab[data-tab="manage"]')?.click();
                    document.getElementById("wiki-media-section")?.scrollIntoView({ behavior: "smooth", block: "center" });
                },
                ready: () => !!document.getElementById("wiki-media-section"),
            },
        ],
    });
}

let boundaryVote: BoundaryVote | null = null;

function onClick(event: MouseEvent): void {
    const control = event.target instanceof Element ? event.target.closest<HTMLElement>("[data-wiki-action]") : null;
    switch (control?.dataset.wikiAction) {
        case "dismiss-conflict":
            document.getElementById("wiki-location-conflict")?.remove();
            break;
        case "boundary-vote":
            boundaryVote?.open();
            break;
    }
}

function init(): void {
    const cfg = document.getElementById("wiki-page-config")?.dataset;
    if (!cfg) return;
    installExternalPanelFallbacks();
    rememberRecentWiki(cfg.recentKey ?? "", {
        slug: cfg.recentSlug ?? "",
        title: cfg.recentTitle ?? "",
        subtitle: cfg.recentSubtitle ?? "",
        url: cfg.recentUrl ?? "",
    });
    installWikiEditForm(document.getElementById("wiki-edit-form"), cfg.editUrl ?? "");
    installWikiRename();
    new WikiMedia(cfg.voteUrl ?? "").install();
    installWikiNoticePin();
    const fab = document.getElementById("pin-actions-fab");
    if (fab) installActionsFab(fab);
    const voteDialog = document.getElementById("boundary-vote-dialog");
    if (voteDialog instanceof HTMLDialogElement) boundaryVote = installBoundaryVote(voteDialog);
    document.addEventListener("click", onClick);
    if (cfg.showTips === "1") initOnboarding();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();
