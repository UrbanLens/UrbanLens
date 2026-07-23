(() => {
  var __defProp = Object.defineProperty;
  var __getOwnPropNames = Object.getOwnPropertyNames;
  var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
  var __hasOwnProp = Object.prototype.hasOwnProperty;
  function __accessProp(key) {
    return this[key];
  }
  var __toCommonJS = (from) => {
    var entry = (__moduleCache ??= new WeakMap).get(from), desc;
    if (entry)
      return entry;
    entry = __defProp({}, "__esModule", { value: true });
    if (from && typeof from === "object" || typeof from === "function") {
      for (var key of __getOwnPropNames(from))
        if (!__hasOwnProp.call(entry, key))
          __defProp(entry, key, {
            get: __accessProp.bind(from, key),
            enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable
          });
    }
    __moduleCache.set(from, entry);
    return entry;
  };
  var __moduleCache;
  var __commonJS = (cb, mod) => () => (mod || cb((mod = { exports: {} }).exports, mod), mod.exports);
  var __returnValue = (v) => v;
  function __exportSetter(name, newValue) {
    this[name] = __returnValue.bind(null, newValue);
  }
  var __export = (target, all) => {
    for (var name in all)
      __defProp(target, name, {
        get: all[name],
        enumerable: true,
        configurable: true,
        set: __exportSetter.bind(all, name)
      });
  };
  var __esm = (fn, res) => () => (fn && (res = fn(fn = 0)), res);
  var __require = /* @__PURE__ */ ((x) => typeof require !== "undefined" ? require : typeof Proxy !== "undefined" ? new Proxy(x, {
    get: (a, b) => (typeof require !== "undefined" ? require : a)[b]
  }) : x)(function(x) {
    if (typeof require !== "undefined")
      return require.apply(this, arguments);
    throw Error('Dynamic require of "' + x + '" is not supported');
  });

  // src/urbanlens/dashboard/frontend/ts/shared/permissions-client.ts
  async function queryState(name) {
    if (!navigator.permissions?.query)
      return "unsupported";
    try {
      const status = await navigator.permissions.query({ name });
      return status.state;
    } catch {
      return "unsupported";
    }
  }
  function getLocationPermissionState() {
    if (!navigator.geolocation)
      return Promise.resolve("unsupported");
    return queryState("geolocation");
  }
  function getNotificationPermissionState() {
    if (!("Notification" in window))
      return Promise.resolve("unsupported");
    return Promise.resolve(Notification.permission === "default" ? "prompt" : Notification.permission);
  }
  function requestLocationPermission() {
    return new Promise((resolve) => {
      if (!navigator.geolocation) {
        resolve("unsupported");
        return;
      }
      navigator.geolocation.getCurrentPosition(() => resolve("granted"), (error) => resolve(error.code === error.PERMISSION_DENIED ? "denied" : "prompt"), { timeout: 1e4 });
    });
  }
  async function requestNotificationPermission() {
    if (!("Notification" in window))
      return "unsupported";
    const result = await Notification.requestPermission();
    return result === "default" ? "prompt" : result;
  }

  // src/urbanlens/dashboard/frontend/ts/entries-classic/permissions.ts
  var api = {
    getLocationPermissionState,
    getNotificationPermissionState,
    requestLocationPermission,
    requestNotificationPermission
  };
  window.UrbanLensPermissions = api;
})();
