/**
 * Requests that must stay out of the fetch net in site-runtime.ts. A background request nobody is waiting on has
 * nothing to tell the user; a handled one already says what went wrong, and the net would add a second, generic
 * toast. Checked as source, because most of them sit in entry modules that run on import.
 */
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import ts from "typescript";

const ROOT = join(import.meta.dir, "..");

interface Request {
    /** The nearest named function, method or variable around the call. */
    scope: string;
    url: string;
    marked: boolean;
}

function nameOf(node: ts.Node): string | null {
    if ((ts.isFunctionDeclaration(node) || ts.isMethodDeclaration(node)) && node.name) return node.name.getText();
    if ((ts.isArrowFunction(node) || ts.isFunctionExpression(node)) && (ts.isVariableDeclaration(node.parent) || ts.isPropertyAssignment(node.parent))) {
        return node.parent.name.getText();
    }
    return null;
}

function scopeOf(node: ts.Node): string {
    for (let at: ts.Node | undefined = node.parent; at; at = at.parent) {
        const name = nameOf(at);
        if (name) return name;
    }
    return "<module>";
}

/** The initializer of the variable *name* as seen from *from*: the nearest enclosing block that declares it. */
function declaredValue(name: string, from: ts.Node): ts.Expression | undefined {
    for (let at: ts.Node | undefined = from.parent; at; at = at.parent) {
        if (!ts.isBlock(at) && !ts.isSourceFile(at)) continue;
        for (const statement of at.statements) {
            if (!ts.isVariableStatement(statement)) continue;
            const declaration = statement.declarationList.declarations.find((d) => d.name.getText() === name);
            if (declaration) return declaration.initializer;
        }
    }
    return undefined;
}

/** Whether *options* is an object setting *flag* to true, directly or through a variable. */
function setsFlag(options: ts.Expression | undefined, flag: string, call: ts.Node): boolean {
    const value = options && ts.isIdentifier(options) ? declaredValue(options.text, call) : options;
    if (!value || !ts.isObjectLiteralExpression(value)) return false;
    return value.properties.some((p) => ts.isPropertyAssignment(p) && p.name.getText() === flag && p.initializer.kind === ts.SyntaxKind.TrueKeyword);
}

/** How each request function opts out of the net. */
const OPT_OUTS: Record<string, (call: ts.CallExpression) => boolean> = {
    fetch: (call) => setsFlag(call.arguments[1], "__ulReported", call),
    fetchResponse: () => true,
    fetchJson: (call) => setsFlag(call.arguments[1], "reportsItsOwnErrors", call),
    fetchText: (call) => setsFlag(call.arguments[1], "reportsItsOwnErrors", call),
    sendJson: (call) => setsFlag(call.arguments[3], "reportsItsOwnErrors", call),
    sendForText: (call) => setsFlag(call.arguments[3], "reportsItsOwnErrors", call),
    ulSendJson: (call) => setsFlag(call.arguments[3], "reportsItsOwnErrors", call),
};

const parsed = new Map<string, Request[]>();

function requests(file: string): Request[] {
    const cached = parsed.get(file);
    if (cached) return cached;
    const source = ts.createSourceFile(file, readFileSync(join(ROOT, file), "utf8"), ts.ScriptTarget.Latest, true);
    const found: Request[] = [];
    const visit = (node: ts.Node): void => {
        if (ts.isCallExpression(node)) {
            const callee = ts.isIdentifier(node.expression) ? node.expression.text : ts.isPropertyAccessExpression(node.expression) ? node.expression.name.text : "";
            const optsOut = Object.hasOwn(OPT_OUTS, callee) ? OPT_OUTS[callee] : undefined;
            if (optsOut) found.push({ scope: scopeOf(node), url: node.arguments[0]?.getText(source) ?? "", marked: optsOut(node) });
        }
        ts.forEachChild(node, visit);
    };
    visit(source);
    parsed.set(file, found);
    return found;
}

function expectOptedOut(file: string, scope: string, fragment: string): void {
    const calls = requests(file).filter((call) => call.scope === scope && call.url.includes(fragment));
    expect(calls.length, `no request to ${fragment} in ${scope} any more`).toBeGreaterThan(0);
    for (const call of calls) expect(call.marked, `the request to ${call.url} in ${scope} still reaches the fetch net`).toBe(true);
}

/** [file, scope, a fragment of the URL argument, why the net must not report it]. */
const BACKGROUND: Array<[string, string, string, string]> = [
    ["entries/map-page.ts", "_recordGeolocationVisit", "_GEOLOCATION_VISIT_URL", "visit tracking on every position fix"],
    ["entries/map-page.ts", "_saveMapPosition", "settingsSaveMapPosition", "saves the view after every pan"],
    ["entries/map-page.ts", "_buildPlacesMarker", "en.wikipedia.org/api/rest_v1/page/summary", "a place without an article is a 404"],
    ["entries/map-page.ts", "_buildPlacesMarker", "mapPlacesDetails", "fills in a popup that already shows the place"],
    ["shared/safety-map.ts", "saveViewSoon", "url", "saves the view after every pan"],
];

describe("background requests stay out of the fetch net", () => {
    test.each(BACKGROUND)("%s %s (%s): %s", (file, scope, fragment) => expectOptedOut(file, scope, fragment));
});

/** [file, scope, a fragment of the URL argument, what the caller shows instead]. */
const REPORTS_ITS_OWN: Array<[string, string, string, string]> = [
    ["entries/article-wysiwyg.ts", "uploadAndInsertImage", "uploadUrl", "toast"],
    ["entries/floorplan-editor.ts", "load", "jsonUrl", "toast"],
    ["entries/floorplan-editor.ts", "switchVersion", "?version=", "toast"],
    ["entries/floorplan-editor.ts", "save", "saveUrl", "toast, and the save status"],
    ["entries/floorplan-editor.ts", "boot", "publishUrl", "toast"],
    ["entries/import-wizard.ts", "readImportState", "url", "the poll's own give-up message"],
    ["entries/import-wizard.ts", "uploadMediaFiles", "mediaUploadUrl", "toast per file"],
    ["entries/import-wizard.ts", "initImportWizard", "previewUrl", "toast"],
    ["entries/import-wizard.ts", "initImportWizard", "confirmUrl", "toast"],
    ["entries/map-annotations.ts", "onPointerUp", "map-height", "toast"],
    ["entries/map-annotations.ts", "buildDetailList", "dp.uuid", "toast"],
    ["entries/map-annotations.ts", "buildDetailList", "markupEditUrlTemplate", "toast"],
    ["entries/map-annotations.ts", "promotePinToParent", "swap-parent", "toast"],
    ["entries/map-annotations.ts", "detailPinPopupContent", "entry.uuid", "toast"],
    ["entries/map-annotations.ts", "loadDetailPins", "detailPinsJsonUrl", "toast"],
    ["entries/map-annotations.ts", "loadDetailPins", "dp.uuid", "toast"],
    ["entries/map-annotations.ts", "init", "detailPinsBulkEditUrl", "toast"],
    ["entries/map-annotations.ts", "doPromoteSelectedDp", "detach-parent", "one summary toast"],
    ["entries/map-annotations.ts", "doShareSelectedDp", "url", "toast"],
    ["entries/map-annotations.ts", "doSendSelectedDpToWiki", "detailPinsSendToWikiUrl", "toast"],
    ["entries/map-annotations.ts", "doDeleteSelectedDp", "dpEditBase", "one summary toast"],
    ["entries/map-annotations.ts", "postBoundary", "boundaryApiUrl", "toast"],
    ["entries/map-annotations.ts", "createDpImmediately", "detailPinCreateUrl", "toast"],
    ["entries/map-annotations.ts", "flushDpAutoSave", "dpEditBase", "toast"],
    ["entries/map-annotations.ts", "init", "editingDp", "toast"],
    ["entries/map-page.ts", "_loadInfrastructure", "mapInfrastructure", "toast"],
    ["entries/map-page.ts", "_showPlaceInfoPanel", "mapPlacesDetails", "the panel's own line"],
    ["entries/map-page.ts", "_loadIconCatalogue", "url", "the picker's own line"],
    ["entries/map-page.ts", "<module>", "createUrl", "toast"],
    ["entries/map-page.ts", "<module>", "quick-edit", "toast"],
    ["entries/map-page.ts", "<module>", "pinAdd", "toast"],
    ["entries/map-page.ts", "_linkPin", "/link/", "toast"],
    ["entries/map-page.ts", "_sendJson", "url", "every caller toasts"],
    ["entries/memories.ts", "requestPage", "url", "the timeline's own line, or a toast"],
    ["entries/photo-location-scan.ts", "upload", "uploadUrl", "toast"],
    ["entries/photo-location-scan.ts", "uploadSelectedPhotos", "uploadPhotoUrl", "one summary toast"],
    ["entries/pin-detail.ts", "postForm", "url", "toast"],
    ["entries/pin-detail.ts", "bindDebugOverlay", "debugClearUrl", "toast"],
    ["entries/pin-detail.ts", "createListAndAdd", "listCreateUrl", "toast"],
    ["entries/pin-list-detail.ts", "postJson", "url", "toast"],
    ["entries/pin-list-detail.ts", "refreshItems", "itemsUrl", "toast"],
    ["entries/pin-list-detail.ts", "deleteList", "deleteUrl", "toast"],
    ["entries/pin-list-detail.ts", "bindAddPins", "pinAddUrl", "toast"],
    ["entries/pin-list-detail.ts", "addPinBySlug", "itemsAddUrl", "toast"],
    ["entries/profile.ts", "post", "updateUrl", "toast"],
    ["entries/settings.ts", "bindSecurity", "form.action", "toast"],
    ["entries/settings.ts", "geocode", "geocodeUrl", "the form's own line"],
    ["entries/trip-detail.ts", "wireActivityMarker", "positionUrl", "toast"],
    ["entries/trip-detail.ts", "load", "url", "toast"],
    ["entries/trip-detail.ts", "saveTripField", "editUrl", "toast"],
    ["shared/album-picker.ts", "submitToAlbum", "addUrl", "toast"],
    ["shared/e2ee-client.ts", "postJson", "url", "each caller's own result"],
    ["shared/e2ee-client.ts", "enrollPasskeyUnlock", "urls.keys", "the dialog's own line, or a toast"],
    ["shared/e2ee-client.ts", "changePassword", "loginParams", "toast"],
    ["shared/e2ee-client.ts", "currentPasswordProof", "loginParams", "each caller's own result"],
    ["shared/e2ee-client.ts", "resetKeys", "urls.keys", "the dialog's own line"],
    ["shared/e2ee-client.ts", "resetKeys", "rewrapAll", "the dialog's own line"],
    ["shared/e2ee-client.ts", "ensureConversationKey", "conversationKeyBase", "the composer's toast"],
    ["shared/e2ee-client.ts", "createConversationKeyVersion", "partnerKeyBase", "the composer's toast; a 404 is by design"],
    ["shared/e2ee-client.ts", "ensureGroupKey", "groupKeyUrl", "the composer's toast"],
    ["shared/e2ee-client.ts", "fetchOwnBundle", "urls.keys", "the unlock dialog's own line"],
    ["shared/external-tag-mapping.ts", "moveEntry", "moveUrl", "toast"],
    ["shared/external-tag-mapping.ts", "groupSelected", "groupUrl", "toast"],
    ["shared/icon-picker.ts", "loadCatalogue", "url", "the picker's own line"],
    ["shared/location-search-engine.ts", "buildSuggestionItem", "resolvePlaceUrl", "toast"],
    ["shared/map-image-overlays.ts", "saveCorners", "cornersUrl", "toast"],
    ["shared/map-image-overlays.ts", "pickFromMedia", "galleryJsonUrl", "the picker's own line"],
    ["shared/markup-toolbar.ts", "ensureMarkupTarget", "markupMapCreateUrl", "toast"],
    ["shared/markup-toolbar.ts", "createMarkupItem", "markupPostUrl", "toast"],
    ["shared/markup-toolbar.ts", "reloadMarkupAndOpenEdit", "markupJsonUrl", "toast"],
    ["shared/markup-toolbar.ts", "createMarkupToolbar", "markupEditBase", "toast"],
    ["shared/markup-toolbar.ts", "deleteMarkupEdit", "markupEditBase", "toast"],
    ["shared/organize-priority.ts", "sendPriorityOrder", "saveUrl", "toast"],
    ["shared/pin-media-gallery.ts", "setRelevance", "relevanceUrl", "toast"],
    ["shared/pin-media-gallery.ts", "bulkSendToWiki", "sendToWikiUrl", "toast"],
    ["shared/pin-media-gallery.ts", "setSort", "sortUrl", "toast"],
    ["shared/safety-live-location.ts", "post", "url", "its own warning once reports keep failing"],
    ["shared/temporal-imagery.ts", "fetchYear", "temporalFeaturesUrl", "toast"],
    ["shared/undo-bar.ts", "postStack", "url", "toast"],
    ["shared/vault-uploader.ts", "postUpload", "url", "toast per file"],
    ["shared/vault-uploader.ts", "remove", "actionBase", "toast"],
    ["shared/webauthn-client.ts", "registerPasskey", "optionsUrl", "the caller's toast"],
    ["shared/webauthn-client.ts", "registerPasskey", "registerUrl", "the caller's toast"],
];

describe("requests that report their own failures stay out of the fetch net", () => {
    test.each(REPORTS_ITS_OWN)("%s %s (%s): %s", (file, scope, fragment) => expectOptedOut(file, scope, fragment));
});
