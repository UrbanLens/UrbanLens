/**
 * The sign-in pages' bundle. Classic and loaded right after e2ee.js, below the form: a deferred module would leave
 * a window in which a quick submit sends the raw password.
 */
import { installAuthPages } from "../shared/auth-pages";

installAuthPages(window.UrbanLensE2EE, window.UrbanLensWebAuthn);
