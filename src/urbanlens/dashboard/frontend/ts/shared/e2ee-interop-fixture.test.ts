/**
 * Guards the committed E2EE interop fixture against the live crypto.
 */

import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import sodium from "libsodium-wrappers-sumo";

import { KDF_MEMLIMIT, KDF_OPSLIMIT, cryptoReady, deriveKey, unwrapSecretKey } from "./e2ee-crypto";

const FIXTURE = join(import.meta.dir, "../../../../../../docs/e2ee-interop-fixture.json");
const fixture = JSON.parse(readFileSync(FIXTURE, "utf8"));

await cryptoReady();

const b64 = (bytes: Uint8Array): string => sodium.to_base64(bytes, sodium.base64_variants.ORIGINAL);

describe("committed E2EE interop fixture still matches this client's crypto", () => {
    test("the fixture records the KDF parameters this client uses", () => {
        // A native client hard-codes these from the fixture.
        expect(fixture.kdf.opslimit).toBe(KDF_OPSLIMIT);
        expect(fixture.kdf.memlimit).toBe(KDF_MEMLIMIT);
    });

    test("auth_key re-derives byte-for-byte", () => {
        const derived = deriveKey(fixture.password, fixture.auth_salt, fixture.kdf.opslimit, fixture.kdf.memlimit);

        expect(b64(derived)).toBe(fixture.expected.auth_key);
    });

    test("wrap_key re-derives byte-for-byte", () => {
        const derived = deriveKey(fixture.password, fixture.wrap_salt, fixture.kdf.opslimit, fixture.kdf.memlimit);

        expect(b64(derived)).toBe(fixture.expected.wrap_key);
    });

    test("the wrapped private key still unwraps to the recorded identity", () => {
        const wrapKey = deriveKey(fixture.password, fixture.wrap_salt, fixture.kdf.opslimit, fixture.kdf.memlimit);

        const unwrapped = unwrapSecretKey(fixture.wrapped_private_key, wrapKey);

        expect(unwrapped).not.toBeNull();
        expect(b64(unwrapped as Uint8Array)).toBe(fixture.identity.private_key);
    });
});
