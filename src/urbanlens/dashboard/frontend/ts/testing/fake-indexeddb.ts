/**
 * Minimal in-memory IndexedDB stand-in for tests.
 *
 * happy-dom ships no IndexedDB, which is why the E2EE key store and the client
 * that depends on it had no test coverage. This implements exactly the surface
 * `shared/e2ee-store.ts` uses - open/upgrade, put/get/delete, and a
 * forward-only cursor - and dispatches every callback asynchronously, as the
 * real API does: callers create a request and assign `onsuccess` afterwards, so
 * firing synchronously would run no handler at all.
 */

type Handler = (() => void) | null;

interface FakeRequest<T> {
    result: T;
    error: unknown;
    onsuccess: Handler;
    onerror: Handler;
    onupgradeneeded?: Handler;
}

function request<T>(result: T): FakeRequest<T> {
    return { result, error: null, onsuccess: null, onerror: null };
}

/** Resolve a request on a microtask, once the caller has attached handlers. */
function succeed<T>(req: FakeRequest<T>): FakeRequest<T> {
    queueMicrotask(() => req.onsuccess?.());
    return req;
}

class FakeTransaction {
    oncomplete: Handler = null;
    onerror: Handler = null;
    error: unknown = null;
    private pending = 0;
    private done = false;

    constructor(private readonly data: Map<string, unknown>) {}

    objectStore(): FakeObjectStore {
        return new FakeObjectStore(this.data, this);
    }

    /** Mark one operation in flight; the transaction completes when all finish. */
    begin(): void {
        this.pending += 1;
    }

    end(): void {
        this.pending -= 1;
        if (this.pending > 0) return;
        queueMicrotask(() => {
            if (this.done) return;
            this.done = true;
            this.oncomplete?.();
        });
    }
}

class FakeObjectStore {
    constructor(
        private readonly data: Map<string, unknown>,
        private readonly tx: FakeTransaction,
    ) {}

    put(value: unknown, key: string): FakeRequest<undefined> {
        this.tx.begin();
        this.data.set(key, value);
        this.tx.end();
        return succeed(request(undefined));
    }

    get(key: string): FakeRequest<unknown> {
        this.tx.begin();
        const req = succeed(request(this.data.get(key)));
        this.tx.end();
        return req;
    }

    delete(key: string): FakeRequest<undefined> {
        this.tx.begin();
        this.data.delete(key);
        this.tx.end();
        return succeed(request(undefined));
    }

    openCursor(): FakeRequest<{ key: string; delete(): void; continue(): void } | null> {
        const req = request<{ key: string; delete(): void; continue(): void } | null>(null);
        // Snapshotted so deleting through the cursor cannot disturb iteration.
        const keys = [...this.data.keys()];
        const data = this.data;
        const tx = this.tx;
        let index = 0;

        tx.begin();
        const step = (): void => {
            if (index >= keys.length) {
                req.result = null;
                req.onsuccess?.();
                tx.end();
                return;
            }
            const key = keys[index]!;
            index += 1;
            req.result = {
                key,
                delete: () => data.delete(key),
                continue: () => queueMicrotask(step),
            };
            req.onsuccess?.();
        };
        queueMicrotask(step);
        return req;
    }
}

class FakeDatabase {
    readonly objectStoreNames = {
        contains: (name: string): boolean => this.stores.has(name),
    };

    constructor(private readonly stores: Map<string, Map<string, unknown>>) {}

    createObjectStore(name: string): void {
        if (!this.stores.has(name)) this.stores.set(name, new Map());
    }

    transaction(name: string, _mode?: string): FakeTransaction {
        return new FakeTransaction(this.stores.get(name) ?? new Map());
    }

    close(): void {
        // Nothing to release; the data outlives the connection, as it would.
    }
}

/** A handle on the fake's contents, for arranging and asserting state. */
export interface FakeIndexedDB {
    /** Every key currently stored, across all object stores. */
    keys(): string[];
    /** Read one value back out. */
    get(key: string, store?: string): unknown;
    /** Seed one value without going through the module under test. */
    set(key: string, value: unknown, store?: string): void;
    /** Remove the fake and restore whatever was on globalThis before. */
    uninstall(): void;
}

const DEFAULT_STORE = "keys";

/**
 * Install the fake as `globalThis.indexedDB`.
 *
 * @param storeName - Object store to pre-create, matching the module under test.
 * @returns A handle for seeding and inspecting the stored data.
 */
export function installFakeIndexedDB(storeName: string = DEFAULT_STORE): FakeIndexedDB {
    const stores = new Map<string, Map<string, unknown>>([[storeName, new Map()]]);
    const previous = (globalThis as { indexedDB?: unknown }).indexedDB;

    const fake = {
        open: (_name: string, _version?: number) => {
            const db = new FakeDatabase(stores);
            const req = request(db) as FakeRequest<FakeDatabase>;
            queueMicrotask(() => {
                req.onupgradeneeded?.();
                req.onsuccess?.();
            });
            return req;
        },
    };

    (globalThis as { indexedDB?: unknown }).indexedDB = fake;

    return {
        keys: () => [...stores.values()].flatMap((store) => [...store.keys()]),
        get: (key, store = storeName) => stores.get(store)?.get(key),
        set: (key, value, store = storeName) => {
            if (!stores.has(store)) stores.set(store, new Map());
            stores.get(store)!.set(key, value);
        },
        uninstall: () => {
            (globalThis as { indexedDB?: unknown }).indexedDB = previous;
        },
    };
}
