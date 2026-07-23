/**
 * These primitives implement a fixed wire protocol shared with the server
 * (models/e2ee/key_bundle.py, PyNaCl interop tests) - format or parameter
 * drift here breaks decryption for every existing user, silently, so the
 * round-trip and cross-derivation guarantees below are worth pinning down.
 */
import { beforeAll, describe, expect, test } from "bun:test";
import {
    cryptoReady,
    decryptMessage,
    deriveKey,
    deriveLoginKeys,
    encryptMessage,
    generateConversationKey,
    generateIdentity,
    generateRecoveryKey,
    KDF_MEMLIMIT,
    KDF_OPSLIMIT,
    parseRecoveryKey,
    randomSalt,
    sealToPublicKey,
    unseal,
    unwrapSecretKey,
    wrapSecretKey,
} from "./e2ee-crypto";

beforeAll(async () => {
    await cryptoReady();
});

describe("deriveKey / deriveLoginKeys", () => {
    test("is deterministic for the same password + salt", () => {
        const salt = randomSalt();
        const a = deriveKey("hunter2", salt);
        const b = deriveKey("hunter2", salt);
        expect(Array.from(a)).toEqual(Array.from(b));
    });

    test("differs when the password differs", () => {
        const salt = randomSalt();
        const a = deriveKey("hunter2", salt);
        const b = deriveKey("hunter3", salt);
        expect(Array.from(a)).not.toEqual(Array.from(b));
    });

    test("differs when the salt differs", () => {
        const a = deriveKey("hunter2", randomSalt());
        const b = deriveKey("hunter2", randomSalt());
        expect(Array.from(a)).not.toEqual(Array.from(b));
    });

    test("authKey and wrapKey are independent given independent salts", () => {
        // This independence is the entire point of the two-salt scheme: the
        // server learns authKey but must not be able to compute wrapKey from it.
        const authSalt = randomSalt();
        const wrapSalt = randomSalt();
        const { authKey, wrapKey } = deriveLoginKeys("hunter2", authSalt, wrapSalt);
        const directWrapKey = deriveKey("hunter2", wrapSalt);
        expect(Array.from(wrapKey)).toEqual(Array.from(directWrapKey));
        expect(authKey).not.toEqual(deriveKey("hunter2", authSalt).toString());
    });

    test("reusing the same salt for both derivations makes them equal", () => {
        // Sanity check on the domain-separation claim above: if the salts
        // ever collapse to one, authKey and wrapKey collapse too.
        const salt = randomSalt();
        const { wrapKey } = deriveLoginKeys("hunter2", salt, salt);
        const authKeyDirect = deriveKey("hunter2", salt);
        expect(Array.from(wrapKey)).toEqual(Array.from(authKeyDirect));
    });

    test("honors explicit opslimit/memlimit overrides", () => {
        const salt = randomSalt();
        const pinned = deriveKey("hunter2", salt, KDF_OPSLIMIT, KDF_MEMLIMIT);
        const overridden = deriveKey("hunter2", salt, KDF_OPSLIMIT + 1, KDF_MEMLIMIT);
        expect(Array.from(pinned)).not.toEqual(Array.from(overridden));
    });
});

describe("generateIdentity", () => {
    test("produces unique keypairs each call", () => {
        const a = generateIdentity();
        const b = generateIdentity();
        expect(a.publicKey).not.toEqual(b.publicKey);
        expect(Array.from(a.privateKey)).not.toEqual(Array.from(b.privateKey));
    });

    test("public key is non-empty base64", () => {
        const { publicKey } = generateIdentity();
        expect(publicKey.length).toBeGreaterThan(0);
        expect(() => atob(publicKey)).not.toThrow();
    });
});

describe("wrapSecretKey / unwrapSecretKey", () => {
    test("round-trips a private key under the correct wrap key", () => {
        const { privateKey } = generateIdentity();
        const wrapKey = deriveKey("hunter2", randomSalt());
        const blob = wrapSecretKey(privateKey, wrapKey);
        const unwrapped = unwrapSecretKey(blob, wrapKey);
        expect(unwrapped).not.toBeNull();
        expect(Array.from(unwrapped!)).toEqual(Array.from(privateKey));
    });

    test("fails closed (null, not throw) under the wrong wrap key", () => {
        const { privateKey } = generateIdentity();
        const blob = wrapSecretKey(privateKey, deriveKey("hunter2", randomSalt()));
        const wrongKey = deriveKey("wrong-password", randomSalt());
        expect(unwrapSecretKey(blob, wrongKey)).toBeNull();
    });

    test("fails closed on a corrupted blob", () => {
        const { privateKey } = generateIdentity();
        const wrapKey = deriveKey("hunter2", randomSalt());
        const blob = wrapSecretKey(privateKey, wrapKey);
        const corrupted = `${blob.slice(0, -4)}abcd`;
        expect(unwrapSecretKey(corrupted, wrapKey)).toBeNull();
    });

    test("each wrap produces a fresh nonce (ciphertext is not reused)", () => {
        const { privateKey } = generateIdentity();
        const wrapKey = deriveKey("hunter2", randomSalt());
        const blobA = wrapSecretKey(privateKey, wrapKey);
        const blobB = wrapSecretKey(privateKey, wrapKey);
        expect(blobA).not.toEqual(blobB);
    });
});

describe("recovery key generation / parsing", () => {
    test("round-trips through its own display format", () => {
        const { key, display } = generateRecoveryKey();
        const parsed = parseRecoveryKey(display);
        expect(parsed).not.toBeNull();
        expect(Array.from(parsed!)).toEqual(Array.from(key));
    });

    test("display form is grouped in dashed 4-character blocks", () => {
        const { display } = generateRecoveryKey();
        expect(display).toMatch(/^[A-Z2-7]{4}(-[A-Z2-7]{4})*$/);
    });

    test("parsing is forgiving of case, spaces, and dashes", () => {
        const { key, display } = generateRecoveryKey();
        const messy = display.toLowerCase().replace(/-/g, " ");
        const parsed = parseRecoveryKey(messy);
        expect(parsed).not.toBeNull();
        expect(Array.from(parsed!)).toEqual(Array.from(key));
    });

    test("rejects garbage input", () => {
        expect(parseRecoveryKey("not a valid recovery key!!")).toBeNull();
        expect(parseRecoveryKey("")).toBeNull();
    });

    test("rejects a truncated key (wrong length)", () => {
        const { display } = generateRecoveryKey();
        // Drop the last group - still valid base32 alphabet, wrong length.
        const truncated = display.split("-").slice(0, -1).join("-");
        expect(parseRecoveryKey(truncated)).toBeNull();
    });
});

describe("sealToPublicKey / unseal", () => {
    test("round-trips arbitrary bytes to the matching identity", () => {
        const identity = generateIdentity();
        const payload = generateConversationKey();
        const sealed = sealToPublicKey(payload, identity.publicKey);
        const opened = unseal(sealed, identity.publicKey, identity.privateKey);
        expect(opened).not.toBeNull();
        expect(Array.from(opened!)).toEqual(Array.from(payload));
    });

    test("fails closed when opened with the wrong identity", () => {
        const recipient = generateIdentity();
        const impostor = generateIdentity();
        const sealed = sealToPublicKey(generateConversationKey(), recipient.publicKey);
        expect(unseal(sealed, impostor.publicKey, impostor.privateKey)).toBeNull();
    });
});

describe("encryptMessage / decryptMessage", () => {
    test("round-trips plaintext under the conversation key", () => {
        const key = generateConversationKey();
        const { ciphertext, nonce } = encryptMessage("hey, found a great spot today", key);
        expect(decryptMessage(ciphertext, nonce, key)).toBe("hey, found a great spot today");
    });

    test("round-trips unicode and empty strings", () => {
        const key = generateConversationKey();
        for (const plaintext of ["", "🏚️ abandoned mall", "line1\nline2\ttab"]) {
            const { ciphertext, nonce } = encryptMessage(plaintext, key);
            expect(decryptMessage(ciphertext, nonce, key)).toBe(plaintext);
        }
    });

    test("fails closed under the wrong conversation key", () => {
        const { ciphertext, nonce } = encryptMessage("secret", generateConversationKey());
        expect(decryptMessage(ciphertext, nonce, generateConversationKey())).toBeNull();
    });

    test("fails closed when the nonce doesn't match the ciphertext", () => {
        const key = generateConversationKey();
        const a = encryptMessage("first message", key);
        const b = encryptMessage("second message", key);
        expect(decryptMessage(a.ciphertext, b.nonce, key)).toBeNull();
    });

    test("each encryption uses a fresh nonce even for identical plaintext", () => {
        const key = generateConversationKey();
        const a = encryptMessage("same text", key);
        const b = encryptMessage("same text", key);
        expect(a.nonce).not.toEqual(b.nonce);
        expect(a.ciphertext).not.toEqual(b.ciphertext);
    });
});
