// src/urbanlens/dashboard/frontend/ts/shared/csrf.ts
function getCsrfToken() {
  return window.csrftoken ?? "";
}

export { getCsrfToken };
