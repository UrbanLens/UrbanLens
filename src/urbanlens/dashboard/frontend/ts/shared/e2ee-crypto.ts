/**
 * Pure crypto primitives for direct-message end-to-end encryption.
 *
 * Every operation here happens in the browser; the server only ever stores
 * the base64 blobs these functions emit. The scheme (see docs/e2ee.md):
 *
 * - X25519 identity keypair per user (crypto_box).
 * - Private key stored server-side only wrapped: under an Argon2id key
 *   derived from the login password (password accounts), and under a random
 *   32-byte recovery key (all accounts).
 * - The login credential itself is a *separately salted* Argon2id derivation
 *   of the same password ("authKey"), so the server never sees the raw
 *   password and cannot derive the wrapping key from what it does see.
 * - Per-conversation random symmetric key, sealed to each participant's
 *   public key (crypto_box_seal); message bodies encrypted with
 *   crypto_secretbox (XSalsa20-Poly1305) under it.
 *
 * All base64 uses the standard alphabet with padding, matching Python's
 * base64.b64decode(validate=True) on the server and PyNaCl in the tests.
 */
import sodium from "libsodium-wrappers-sumo";

/** Argon2id parameters - libsodium "interactive" limits, pinned to match the
 * server-side defaults in models/e2ee/key_bundle.py. */
export const KDF_OPSLIMIT = 2;
export const KDF_MEMLIMIT = 67_108_864; // 64 MiB

const KEY_BYTES = 32;

/** Resolve once libsodium's WASM is initialized; call before anything else. */
export async function cryptoReady(): Promise<void> {
    await sodium.ready;
}

function toB64(bytes: Uint8Array): string {
    return sodium.to_base64(bytes, sodium.base64_variants.ORIGINAL);
}

function fromB64(value: string): Uint8Array {
    return sodium.from_base64(value, sodium.base64_variants.ORIGINAL);
}

/** Generate a random Argon2id salt, base64-encoded. */
export function randomSalt(): string {
    return toB64(sodium.randombytes_buf(sodium.crypto_pwhash_SALTBYTES));
}

/**
 * Derive one 32-byte key from a password with Argon2id.
 *
 * @param password - The raw password (never transmitted).
 * @param saltB64 - Base64 16-byte salt.
 * @param opslimit - Argon2id operations limit (server-pinned per bundle).
 * @param memlimit - Argon2id memory limit in bytes (server-pinned per bundle).
 * @returns The derived key bytes.
 */
export function deriveKey(password: string, saltB64: string, opslimit: number = KDF_OPSLIMIT, memlimit: number = KDF_MEMLIMIT): Uint8Array {
    return sodium.crypto_pwhash(KEY_BYTES, password, fromB64(saltB64), opslimit, memlimit, sodium.crypto_pwhash_ALG_ARGON2ID13);
}

/** The two independent keys a password account derives at login. */
export interface LoginKeys {
    /** Base64 credential sent to the server in place of the password. */
    authKey: string;
    /** Key-wrapping key; never leaves the browser. */
    wrapKey: Uint8Array;
}

/**
 * Derive the login credential and the key-wrapping key from one password.
 *
 * The two salts MUST be independent - that is the domain separation keeping
 * the server (which learns authKey) unable to compute wrapKey.
 *
 * @param password - The raw password.
 * @param authSaltB64 - Salt for the login credential (from AccountKdf).
 * @param wrapSaltB64 - Salt for the wrapping key (from the key bundle).
 * @param opslimit - Argon2id operations limit.
 * @param memlimit - Argon2id memory limit in bytes.
 * @returns Both derived keys.
 */
export function deriveLoginKeys(password: string, authSaltB64: string, wrapSaltB64: string, opslimit: number = KDF_OPSLIMIT, memlimit: number = KDF_MEMLIMIT): LoginKeys {
    return {
        authKey: toB64(deriveKey(password, authSaltB64, opslimit, memlimit)),
        wrapKey: deriveKey(password, wrapSaltB64, opslimit, memlimit),
    };
}

/** A freshly generated X25519 identity keypair. */
export interface Identity {
    publicKey: string;
    privateKey: Uint8Array;
}

/** Generate a new X25519 identity keypair. */
export function generateIdentity(): Identity {
    const pair = sodium.crypto_box_keypair();
    return { publicKey: toB64(pair.publicKey), privateKey: pair.privateKey };
}

/**
 * Encrypt the private key under a wrapping key (password-derived or recovery).
 *
 * @param privateKey - The identity private key.
 * @param wrapKey - 32-byte symmetric wrapping key.
 * @returns One base64 blob: nonce || secretbox ciphertext.
 */
export function wrapSecretKey(privateKey: Uint8Array, wrapKey: Uint8Array): string {
    const nonce = sodium.randombytes_buf(sodium.crypto_secretbox_NONCEBYTES);
    const boxed = sodium.crypto_secretbox_easy(privateKey, nonce, wrapKey);
    const blob = new Uint8Array(nonce.length + boxed.length);
    blob.set(nonce);
    blob.set(boxed, nonce.length);
    return toB64(blob);
}

/**
 * Decrypt a wrapped private-key blob.
 *
 * @param blobB64 - Base64 nonce || secretbox blob from wrapSecretKey.
 * @param wrapKey - The 32-byte wrapping key.
 * @returns The private key, or null when the key is wrong / blob corrupt.
 */
export function unwrapSecretKey(blobB64: string, wrapKey: Uint8Array): Uint8Array | null {
    try {
        const blob = fromB64(blobB64);
        const nonce = blob.slice(0, sodium.crypto_secretbox_NONCEBYTES);
        const boxed = blob.slice(sodium.crypto_secretbox_NONCEBYTES);
        return sodium.crypto_secretbox_open_easy(boxed, nonce, wrapKey);
    } catch {
        return null;
    }
}

/** A generated recovery key: raw bytes plus its human display form. */
export interface RecoveryKey {
    key: Uint8Array;
    /** Grouped base32, e.g. "R3K7-Q2ND-…" - what the user saves. */
    display: string;
}

const BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

function base32Encode(bytes: Uint8Array): string {
    let bits = 0;
    let value = 0;
    let output = "";
    for (const byte of bytes) {
        value = (value << 8) | byte;
        bits += 8;
        while (bits >= 5) {
            output += BASE32_ALPHABET[(value >>> (bits - 5)) & 31];
            bits -= 5;
        }
    }
    if (bits > 0) {
        output += BASE32_ALPHABET[(value << (5 - bits)) & 31];
    }
    return output;
}

function base32Decode(text: string): Uint8Array | null {
    let bits = 0;
    let value = 0;
    const output: number[] = [];
    for (const char of text) {
        const index = BASE32_ALPHABET.indexOf(char);
        if (index < 0) {
            return null;
        }
        value = (value << 5) | index;
        bits += 5;
        if (bits >= 8) {
            output.push((value >>> (bits - 8)) & 255);
            bits -= 8;
        }
    }
    return new Uint8Array(output);
}

/** Generate a full-entropy 32-byte recovery key with its display encoding. */
export function generateRecoveryKey(): RecoveryKey {
    const key = sodium.randombytes_buf(KEY_BYTES);
    return { key, display: formatRecoveryDisplay(base32Encode(key)) };
}

function formatRecoveryDisplay(encoded: string): string {
    return (encoded.match(/.{1,4}/g) ?? []).join("-");
}

/**
 * Parse a user-typed recovery key back to bytes (forgiving about case,
 * spaces, and dashes).
 *
 * @param display - Whatever the user typed or pasted.
 * @returns The 32-byte key, or null when it doesn't parse.
 */
export function parseRecoveryKey(display: string): Uint8Array | null {
    const cleaned = display.toUpperCase().replace(/[^A-Z2-7]/g, "");
    const bytes = base32Decode(cleaned);
    return bytes !== null && bytes.length === KEY_BYTES ? bytes.slice(0, KEY_BYTES) : null;
}

/**
 * Seal bytes to a public key (anonymous sender - crypto_box_seal).
 *
 * @param data - The bytes to seal (a conversation key).
 * @param publicKeyB64 - The recipient's identity public key.
 * @returns Base64 sealed blob only the matching private key can open.
 */
export function sealToPublicKey(data: Uint8Array, publicKeyB64: string): string {
    return toB64(sodium.crypto_box_seal(data, fromB64(publicKeyB64)));
}

/**
 * Open a sealed blob with our identity keypair.
 *
 * @param blobB64 - Base64 crypto_box_seal output.
 * @param publicKeyB64 - Our identity public key.
 * @param privateKey - Our identity private key.
 * @returns The sealed bytes, or null on failure.
 */
export function unseal(blobB64: string, publicKeyB64: string, privateKey: Uint8Array): Uint8Array | null {
    try {
        return sodium.crypto_box_seal_open(fromB64(blobB64), fromB64(publicKeyB64), privateKey);
    } catch {
        return null;
    }
}

/** Generate a random 32-byte conversation key. */
export function generateConversationKey(): Uint8Array {
    return sodium.randombytes_buf(KEY_BYTES);
}

/** One encrypted message body ready to send. */
export interface EncryptedMessage {
    ciphertext: string;
    nonce: string;
}

/**
 * Encrypt one message body under a conversation key.
 *
 * @param plaintext - The message text.
 * @param conversationKey - The 32-byte conversation key.
 * @returns Base64 ciphertext and nonce, stored separately server-side.
 */
export function encryptMessage(plaintext: string, conversationKey: Uint8Array): EncryptedMessage {
    const nonce = sodium.randombytes_buf(sodium.crypto_secretbox_NONCEBYTES);
    const ciphertext = sodium.crypto_secretbox_easy(sodium.from_string(plaintext), nonce, conversationKey);
    return { ciphertext: toB64(ciphertext), nonce: toB64(nonce) };
}

/**
 * Decrypt one message body.
 *
 * @param ciphertextB64 - Base64 secretbox ciphertext.
 * @param nonceB64 - Base64 nonce stored with the message.
 * @param conversationKey - The 32-byte conversation key.
 * @returns The plaintext, or null when the key/nonce/blob don't match.
 */
export function decryptMessage(ciphertextB64: string, nonceB64: string, conversationKey: Uint8Array): string | null {
    try {
        const opened = sodium.crypto_secretbox_open_easy(fromB64(ciphertextB64), fromB64(nonceB64), conversationKey);
        return sodium.to_string(opened);
    } catch {
        return null;
    }
}
