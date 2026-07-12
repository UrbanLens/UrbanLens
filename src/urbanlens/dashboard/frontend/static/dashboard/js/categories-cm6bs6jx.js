var __require = /* @__PURE__ */ ((x) => typeof require !== "undefined" ? require : typeof Proxy !== "undefined" ? new Proxy(x, {
  get: (a, b) => (typeof require !== "undefined" ? require : a)[b]
}) : x)(function(x) {
  if (typeof require !== "undefined")
    return require.apply(this, arguments);
  throw Error('Dynamic require of "' + x + '" is not supported');
});

// src/urbanlens/dashboard/frontend/ts/shared/dialogs.ts
async function confirmAction(options) {
  if (window.confirmDialog) {
    return window.confirmDialog(options);
  }
  return window.confirm(options.message ?? "Are you sure?");
}
var toast = {
  success(message) {
    window.toastr.success(message);
  },
  error(message) {
    window.toastr.error(message);
  },
  warning(message) {
    window.toastr.warning(message);
  },
  info(message) {
    window.toastr.info(message);
  }
};
function htmxProcess(element) {
  window.htmx?.process(element);
}

// src/urbanlens/dashboard/frontend/ts/shared/csrf.ts
function getCsrfToken() {
  return window.csrftoken ?? "";
}

export { __require, confirmAction, toast, htmxProcess, getCsrfToken };
