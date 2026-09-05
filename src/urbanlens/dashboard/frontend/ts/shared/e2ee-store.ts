/**
 * IndexedDB cache for decrypted E2EE key material.
 *
 * After a successful unlock (login-derived wrap key, or recovery key), the
 * decrypted identity private key and any unsealed conversation keys are
 * cached here so day-to-day use never prompts for anything. Entries are keyed
 * by profile slug so two accounts sharing a browser can't read each other's
 * cache rows by accident (same-origin storage is the trust boundary either
 * way - this is bookkeeping, not isolation).
 */

const DB_NAME = "urbanlens-e2ee";
const DB_VERSION = 1;
const STORE = "keys";

/** A cached identity: the decrypted private key plus bundle bookkeeping. */
export interface CachedIdentity {
    privateKey: Uint8Array;
    publicKey: string;
    /** MessagingKeyBundle.version this identity belongs to. */
    version: number;
}

function openDb(): Promise<IDBDatabase> {
    return new Promise((resolve, reject) => {
        const request = indexedDB.open(DB_NAME, DB_VERSION);
        request.onupgradeneeded = () => {
            if (!request.result.objectStoreNames.contains(STORE)) {
                request.result.createObjectStore(STORE);
            }
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error ?? new Error("IndexedDB open failed"));
    });
}

async function put(key: string, value: unknown): Promise<void> {
    const db = await openDb();
    await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(STORE, "readwrite");
        tx.objectStore(STORE).put(value, key);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error ?? new Error("IndexedDB write failed"));
    });
    db.close();
}

async function get<T>(key: string): Promise<T | null> {
    const db = await openDb();
    const value = await new Promise<T | null>((resolve, reject) => {
        const request = db.transaction(STORE, "readonly").objectStore(STORE).get(key);
        request.onsuccess = () => resolve((request.result as T | undefined) ?? null);
        request.onerror = () => reject(request.error ?? new Error("IndexedDB read failed"));
    });
    db.close();
    return value;
}

async function remove(key: string): Promise<void> {
    const db = await openDb();
    await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(STORE, "readwrite");
        tx.objectStore(STORE).delete(key);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error ?? new Error("IndexedDB delete failed"));
    });
    db.close();
}

async function removeByPrefix(prefix: string): Promise<void> {
    const db = await openDb();
    await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(STORE, "readwrite");
        const store = tx.objectStore(STORE);
        const request = store.openCursor();
        request.onsuccess = () => {
            const cursor = request.result;
            if (cursor) {
                if (typeof cursor.key === "string" && cursor.key.startsWith(prefix)) {
                    cursor.delete();
                }
                cursor.continue();
            }
        };
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error ?? new Error("IndexedDB delete failed"));
    });
    db.close();
}

function identityKey(selfSlug: string): string {
    return `identity:${selfSlug}`;
}

function conversationKeyKey(selfSlug: string, partnerSlug: string, version: number): string {
    return `conv:${selfSlug}:${partnerSlug}:${version}`;
}

/**
 * Ask the browser to exempt this origin's storage from automatic eviction.
 *
 * Best-effort and deliberately un-awaited by callers' critical paths: browsers
 * either grant it silently (Chromium/Firefox weigh engagement and installed
 * state) or ignore it entirely (Safari, which evicts on its own 7-day
 * inactivity schedule regardless). It matters most for OAuth-only accounts,
 * whose cached identity is the ONLY unlock path that costs the user nothing -
 * eviction there means falling back to a recovery key they may not have kept,
 * since an SSO signin has no password from which to re-derive the wrap key.
 */
async function requestPersistentStorage(): Promise<void> {
    try {
        if (await navigator.storage?.persisted?.()) {
            return;
        }
        await navigator.storage?.persist?.();
    } catch {
        // Not supported, or blocked by policy - the cache still works, it is
        // just evictable. Never let this fail an unlock.
    }
}

/** Cache the decrypted identity for a profile. */
export async function putIdentity(selfSlug: string, identity: CachedIdentity): Promise<void> {
    await put(identityKey(selfSlug), identity);
    await requestPersistentStorage();
}

/** Load the cached identity for a profile, or null when locked. */
export async function getIdentity(selfSlug: string): Promise<CachedIdentity | null> {
    try {
        return await get<CachedIdentity>(identityKey(selfSlug));
    } catch {
        return null;
    }
}

/** Cache one unsealed conversation key version. */
export async function putConversationKey(selfSlug: string, partnerSlug: string, version: number, key: Uint8Array): Promise<void> {
    await put(conversationKeyKey(selfSlug, partnerSlug, version), key);
}

/** Load one cached conversation key version, or null. */
export async function getConversationKey(selfSlug: string, partnerSlug: string, version: number): Promise<Uint8Array | null> {
    try {
        return await get<Uint8Array>(conversationKeyKey(selfSlug, partnerSlug, version));
    } catch {
        return null;
    }
}

function groupKeyKey(selfSlug: string, groupUuid: string, version: number): string {
    return `group:${selfSlug}:${groupUuid}:${version}`;
}

/** Cache one unsealed group key version. */
export async function putGroupKey(selfSlug: string, groupUuid: string, version: number, key: Uint8Array): Promise<void> {
    await put(groupKeyKey(selfSlug, groupUuid, version), key);
}

/** Load one cached group key version, or null. */
export async function getGroupKey(selfSlug: string, groupUuid: string, version: number): Promise<Uint8Array | null> {
    try {
        return await get<Uint8Array>(groupKeyKey(selfSlug, groupUuid, version));
    } catch {
        return null;
    }
}

/**
 * Wipe every cached key for a profile.
 *
 * Called by the key-reset flow, and by nothing else. It is deliberately *not*
 * called on logout: whether an explicit sign-out should discard decrypted keys
 * is a product question - a shared machine says yes, one's own laptop says no,
 * and re-entering a recovery key is the cost of the second answer. See P48 in
 * `docs/PROBLEMS.md` ("Logging out leaves every decrypted E2EE key cached").
 *
 * This used to say "logout-everywhere / key reset". There is no
 * logout-everywhere feature anywhere in this codebase, which made the sentence
 * read as though logout already cleared these.
 */
export async function clearProfileKeys(selfSlug: string): Promise<void> {
    // Deleted by exact key, not by prefix: "identity:jess" is a prefix of
    // "identity:jess2", so resetting one account would evict a second account
    // that shares the browser and silently lock it. The conv:/group: prefixes
    // are safe because the slug there is followed by a ":" terminator.
    await remove(identityKey(selfSlug));
    await removeByPrefix(`conv:${selfSlug}:`);
    await removeByPrefix(`group:${selfSlug}:`);
}
