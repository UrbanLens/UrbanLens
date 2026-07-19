// src/urbanlens/dashboard/frontend/ts/shared/csrf.ts
function getCsrfToken() {
  return window.csrftoken ?? "";
}

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

export { getCsrfToken, confirmAction, toast, htmxProcess };
