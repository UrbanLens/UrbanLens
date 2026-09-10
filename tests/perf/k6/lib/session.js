/**
 * Signing in, and the two Django behaviours that make it more than one POST.
 *
 * **CSRF is two halves that must match.** Django compares a cookie against a
 * form field, so the token has to be fetched from the login page before it can
 * be posted back. k6 keeps a cookie jar per VU, so the cookie half is automatic
 * once the GET has happened; the field half is read out of that jar here.
 *
 * **Over HTTPS, Django also checks `Referer`.** A run against
 * `https://…dev.urbanlens.org` with no `Referer` header is refused before the
 * view executes, and the refusal renders as a 403 page at the URL that was
 * posted to - indistinguishable from a wrong password unless something looks at
 * the body. Both headers are sent on every unsafe request here, and the failure
 * message says which of the three causes it was.
 *
 * These accounts must come from `provision_integration_env`, which clears
 * `AccountKdf`. With a salt present the browser derives the credential before
 * posting it, and the plaintext in the manifest stops being what the form
 * sends - so a raw POST like this one would fail for an account that a human
 * has since logged into through a browser.
 *
 * **Sign in once, then adopt.** Password verification is PBKDF2 and costs
 * hundreds of milliseconds of CPU by design, so a VU pool that each signed
 * itself in would spend its ramp-up mounting a CPU attack on the box it is
 * trying to measure. Measured on a dev stack: one login took 1.0s and every
 * other request under 0.25s, but sixty VUs each doing their own login produced
 * zero completed iterations in fifty seconds. `setup` signs in once per role
 * and every VU adopts the resulting cookies - which is also the more honest
 * model, since the neighbour is meant to be a user who already has a session,
 * not one who logs in five times a second.
 *
 * **Adopted cookies go in an explicit jar, not the VU's.** k6 resets a VU's
 * own cookie jar at the start of every iteration, so cookies installed once
 * survive exactly one request. What that produced was not an error: the second
 * iteration onwards was redirected to the sign-in page, k6 followed the 302
 * and recorded a fast 200, and the run reported a healthy p95 for the login
 * page while believing it had measured the map. A jar constructed here lives
 * as long as the VU does and is passed explicitly on every request.
 */

import http from "k6/http";
import { fail } from "k6";

const LOGIN_PATH = "/accounts/login/";

/**
 * Sign in as one account, leaving the session cookie in this VU's jar.
 *
 * @param {string} baseUrl Origin under test, no trailing slash.
 * @param {{username: string, password: string, role: string}} account
 * @returns {{baseUrl: string, role: string, csrfToken: string}} A handle to
 *     pass to the request helpers below.
 */
export function signIn(baseUrl, account) {
    const loginUrl = `${baseUrl}${LOGIN_PATH}`;
    const form = http.get(loginUrl, { tags: { endpoint: "login_form", phase: "setup" }, responseType: "text" });
    if (form.status !== 200) {
        fail(`GET ${loginUrl} answered ${form.status}; the target is not serving the sign-in page.`);
    }

    const token = csrfToken(baseUrl);
    if (!token) {
        fail(`No csrftoken cookie after GET ${loginUrl}. Django sets it on that page, so this means something in front of the app is stripping Set-Cookie.`);
    }

    const response = http.post(
        loginUrl,
        { csrfmiddlewaretoken: token, username: account.username, password: account.password },
        {
            headers: unsafeHeaders(baseUrl, loginUrl),
            redirects: 5,
            tags: { endpoint: "login", phase: "setup" },
            // Overrides the run's `discardResponseBodies`. Without the body
            // there is no way to tell a successful sign-in from the form
            // re-rendering with an error, since both are a 200 at this URL -
            // so every failed login would be adopted as a working session and
            // the whole run would measure the login page.
            responseType: "text",
        },
    );

    // A successful sign-in redirects away from the login page. A failed one
    // renders the form again with an error, at 200, at the same URL - so status
    // alone cannot tell them apart, and neither can the URL on its own.
    const stillOnLoginForm = response.body && response.body.includes('id="password-login-form"');
    if (response.status >= 400 || stillOnLoginForm) {
        fail(`Sign-in as "${account.username}" failed (${response.status}). ${diagnose(response)}`);
    }

    // The token is rotated on login, so the pre-login one is stale for every
    // POST after this.
    return { baseUrl, role: account.role, cookies: cookiesFor(baseUrl) };
}

/**
 * Adopt cookies minted by `signIn` into *this* VU's jar.
 *
 * Each k6 VU is its own runtime with its own cookie jar, so a session
 * established in `setup` does not reach them by itself. Passing the cookies
 * through `setup`'s return value and installing them here means one password
 * verification for the whole run instead of one per VU.
 *
 * @param {string} baseUrl Origin under test, no trailing slash.
 * @param {string} role Which account these cookies belong to.
 * @param {{sessionid: string, csrftoken: string}} cookies From `signIn`.
 * @returns {{baseUrl: string, role: string}} A handle for the request helpers.
 */
export function adopt(baseUrl, role, cookies) {
    const jar = new http.CookieJar();
    for (const name of SESSION_COOKIES) {
        if (cookies[name]) {
            jar.set(`${baseUrl}/`, name, cookies[name], { path: "/" });
        }
    }
    return { baseUrl, role, jar };
}

/** The two cookies a signed-in request needs. */
const SESSION_COOKIES = ["sessionid", "csrftoken"];

/** Both session cookies as a plain object, for handing to another VU. */
export function cookiesFor(baseUrl) {
    const jar = http.cookieJar().cookiesForURL(`${baseUrl}/`);
    const cookies = {};
    for (const name of SESSION_COOKIES) {
        if (jar[name]) {
            cookies[name] = jar[name][0];
        }
    }
    return cookies;
}

/**
 * The current CSRF token for a jar.
 *
 * @param {string} baseUrl Origin under test, no trailing slash.
 * @param {object} jar The jar to read. Defaults to the VU's own, which is
 *     correct inside `setup` and wrong everywhere else - see the note on
 *     `adopt` about k6 resetting it between iterations.
 */
export function csrfToken(baseUrl, jar) {
    const cookies = (jar || http.cookieJar()).cookiesForURL(`${baseUrl}/`);
    return cookies.csrftoken ? cookies.csrftoken[0] : "";
}

/** Headers Django requires on any unsafe method, given where the request claims to come from. */
export function unsafeHeaders(baseUrl, referer) {
    return {
        Origin: baseUrl,
        Referer: referer || `${baseUrl}/`,
        "X-Requested-With": "XMLHttpRequest",
    };
}

/** GET, tagged so the summary can slice by endpoint and by what the actor was doing. */
export function get(session, path, tags, extra) {
    return http.get(
        `${session.baseUrl}${path}`,
        Object.assign({ tags: Object.assign({ role: session.role }, tags) }, jarParam(session), extra),
    );
}

/** The session's own jar as request params, or nothing when it has none. */
function jarParam(session) {
    return session.jar ? { jar: session.jar } : {};
}

/**
 * POST a form, refreshing the CSRF token from the jar each time.
 *
 * Re-read rather than cached because Django rotates the token on login and may
 * rotate it again; a cached token turns into a 403 halfway through a run, which
 * reads as the endpoint failing rather than as the harness failing.
 *
 * @param {object} extra Merged into k6's request params. `timeout` above all:
 *     its default of 60s is shorter than several of the things measured here,
 *     and a request the harness cut off is recorded as an error the server
 *     never made.
 */
export function postForm(session, path, body, tags, extra) {
    const url = `${session.baseUrl}${path}`;
    const payload = Object.assign({ csrfmiddlewaretoken: csrfToken(session.baseUrl, session.jar) }, body);
    return http.post(
        url,
        payload,
        Object.assign(
            { headers: unsafeHeaders(session.baseUrl, url), tags: Object.assign({ role: session.role }, tags) },
            jarParam(session),
            extra,
        ),
    );
}

/** POST a JSON document, for the endpoints that take one. */
export function postJson(session, path, document, tags, extra) {
    const url = `${session.baseUrl}${path}`;
    const headers = Object.assign({ "Content-Type": "application/json" }, unsafeHeaders(session.baseUrl, url));
    headers["X-CSRFToken"] = csrfToken(session.baseUrl, session.jar);
    return http.post(
        url,
        JSON.stringify(document),
        Object.assign({ headers, tags: Object.assign({ role: session.role }, tags) }, jarParam(session), extra),
    );
}

/** Whatever the response can say about why sign-in did not happen. */
function diagnose(response) {
    const body = response.body || "";
    if (/CSRF verification failed|CSRF cookie not set/i.test(body)) {
        return "Django refused the POST for CSRF. That is almost always the target's UL_SITE_URL not matching the --url this ran against: Django only trusts origins it was configured for.";
    }
    if (/hasn't been verified|has not been verified/i.test(body)) {
        return "The account exists but its email is unverified. Re-run provision_integration_env on the target.";
    }
    if (/too many|locked/i.test(body)) {
        return "The account is rate-limited or locked out from earlier failed attempts.";
    }
    if (/two-factor|2fa/i.test(body)) {
        return "The account has a second factor, which this cannot answer. Re-run provision_integration_env to clear it.";
    }
    return "The form came back without a recognised error; the credentials in the manifest are probably not the current ones.";
}
