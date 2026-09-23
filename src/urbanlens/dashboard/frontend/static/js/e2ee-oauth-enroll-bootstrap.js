// Passwordless (OAuth) account with no message-encryption keys yet: enroll transparently in the
// background. The recovery key stays viewable in Settings > Direct Messages while this device
// holds the decrypted key, so nothing is lost if the toast goes unnoticed.
//
// e2ee.js loads immediately above this file (themes/base.html) and defines the UrbanLensE2EE
// global this reads; #e2ee-urls is the json_script island holding this request's reversed URLs.
(function () {
    var urls = JSON.parse(document.getElementById('e2ee-urls').textContent);
    UrbanLensE2EE.init({
        selfSlug: null,
        urls: urls,
    });
    UrbanLensE2EE.enrollOauthIfNeeded().catch(function (error) {
        console.error("E2EE OAuth enrollment failed", error);
    });
}());
