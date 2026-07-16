import {
  confirmAction,
  getCsrfToken,
  htmxProcess,
  toast
} from "./map-annotations-9jdqkrz7.js";

// src/urbanlens/dashboard/frontend/ts/shared/icon-picker.ts
var MATERIAL_ICON_NAME = /^[a-z_]+$/;
var IconPicker = {
  toggle(id) {
    const panel = document.getElementById(`icon-panel-${id}`);
    if (!panel)
      return;
    const isHidden = panel.hasAttribute("hidden");
    document.querySelectorAll(".icon-picker-panel").forEach((p) => p.setAttribute("hidden", ""));
    if (isHidden) {
      panel.removeAttribute("hidden");
      const search = panel.querySelector(".icon-picker-search-input");
      if (search) {
        search.value = "";
        search.focus();
      }
      IconPicker.setTabSilent(id, "");
    }
  },
  setTabSilent(id, cat) {
    const panel = document.getElementById(`icon-panel-${id}`);
    if (!panel)
      return;
    panel.querySelectorAll(".icon-tab").forEach((b) => b.classList.toggle("active", b.dataset.cat === cat));
    const grid = document.getElementById(`icon-grid-${id}`);
    if (!grid)
      return;
    grid.querySelectorAll(".icon-picker-item").forEach((item) => {
      item.style.display = !cat || item.dataset.cat === cat || !item.dataset.cat ? "" : "none";
    });
  },
  setTab(id, cat, btn) {
    const panel = document.getElementById(`icon-panel-${id}`);
    if (!panel)
      return;
    panel.querySelectorAll(".icon-tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const search = panel.querySelector(".icon-picker-search-input");
    if (search)
      search.value = "";
    const grid = document.getElementById(`icon-grid-${id}`);
    if (!grid)
      return;
    grid.querySelectorAll(".icon-picker-item").forEach((item) => {
      item.style.display = !cat || item.dataset.cat === cat || !item.dataset.cat ? "" : "none";
    });
  },
  search(id, query) {
    const q = query.toLowerCase().trim();
    const panel = document.getElementById(`icon-panel-${id}`);
    if (!panel)
      return;
    panel.querySelectorAll(".icon-tab").forEach((b) => b.classList.toggle("active", b.dataset.cat === ""));
    const grid = document.getElementById(`icon-grid-${id}`);
    if (!grid)
      return;
    grid.querySelectorAll(".icon-picker-item").forEach((item) => {
      if (!q) {
        item.style.display = "";
        return;
      }
      const label = item.dataset.label ?? "";
      const icon = item.dataset.icon ?? "";
      const keywords = item.dataset.keywords ?? "";
      item.style.display = label.includes(q) || icon === q || keywords.includes(q) ? "" : "none";
    });
  },
  pick(id, icon, btn) {
    const input = document.getElementById(`icon-value-${id}`);
    if (input)
      input.value = icon;
    const current = document.getElementById(`icon-current-${id}`);
    if (current) {
      current.innerHTML = renderIconGlyphHtml(icon);
    }
    const grid = document.getElementById(`icon-grid-${id}`);
    if (grid) {
      grid.querySelectorAll(".icon-picker-item").forEach((b) => b.classList.remove("selected"));
      btn?.classList.add("selected");
    }
    const panel = document.getElementById(`icon-panel-${id}`);
    if (panel)
      panel.setAttribute("hidden", "");
  }
};
function renderIconGlyphHtml(icon) {
  if (!icon)
    return '<span class="icon-picker-none-label">No icon</span>';
  return MATERIAL_ICON_NAME.test(icon) ? `<i class="material-icons icon-picker-current-mi">${icon}</i>` : `<span class="icon-picker-current-glyph">${icon}</span>`;
}
function resetIconPicker(pickerId) {
  const input = document.getElementById(`icon-value-${pickerId}`);
  if (input)
    input.value = "";
  const current = document.getElementById(`icon-current-${pickerId}`);
  if (current)
    current.innerHTML = '<span class="icon-picker-none-label">No icon</span>';
  const grid = document.getElementById(`icon-grid-${pickerId}`);
  if (grid) {
    grid.querySelectorAll(".icon-picker-item").forEach((b) => b.classList.remove("selected"));
    grid.querySelector(".icon-picker-none")?.classList.add("selected");
  }
}
document.addEventListener("click", (e) => {
  if (!e.target.closest(".icon-picker-dropdown")) {
    document.querySelectorAll(".icon-picker-panel").forEach((p) => p.setAttribute("hidden", ""));
  }
});

// src/urbanlens/dashboard/frontend/ts/shared/organize-icon-picker.ts
var bulkStateUpdaters = new Map;
function registerBulkStateUpdater(nsPrefix, updater) {
  bulkStateUpdaters.set(nsPrefix, updater);
}
var OrganizeIconPicker = {
  ...IconPicker,
  pick(id, icon, btn) {
    IconPicker.pick(id, icon, btn);
    const clearFlag = document.getElementById(`edit-clear-custom-${id}`);
    if (clearFlag)
      clearFlag.value = "1";
    const uploadInput = document.getElementById(`icon-upload-input-${id}`);
    if (icon && uploadInput)
      uploadInput.value = "";
    if (id.endsWith("-bulk-edit")) {
      const ns = id.slice(0, -"-bulk-edit".length);
      const nochange = document.getElementById(`${ns}-bulk-icon-nochange`);
      if (nochange)
        nochange.checked = false;
      bulkStateUpdaters.get(ns)?.();
    }
  },
  _handleUpload(id, input) {
    const file = input.files?.[0];
    if (!file)
      return;
    const clearFlag = document.getElementById(`edit-clear-custom-${id}`);
    if (clearFlag)
      clearFlag.value = "";
    const iconVal = document.getElementById(`icon-value-${id}`);
    if (iconVal)
      iconVal.value = "";
    document.getElementById(`icon-grid-${id}`)?.querySelectorAll(".icon-picker-item").forEach((b) => b.classList.remove("selected"));
    const reader = new FileReader;
    reader.onload = (e) => {
      const current = document.getElementById(`icon-current-${id}`);
      if (current)
        current.innerHTML = `<img src="${e.target?.result}" class="icon-picker-custom-preview" alt="Custom icon">`;
    };
    reader.readAsDataURL(file);
    document.getElementById(`icon-panel-${id}`)?.setAttribute("hidden", "");
  }
};
function installGlobalOrganizeIconPicker() {
  window.IconPicker = OrganizeIconPicker;
}

// src/urbanlens/dashboard/frontend/ts/shared/color-picker.ts
function pickColor(pickerId, valueId, colorHex, btn) {
  const picker = document.getElementById(pickerId);
  picker?.querySelectorAll(".color-swatch").forEach((b) => b.classList.remove("selected"));
  btn.classList.add("selected");
  const value = document.getElementById(valueId);
  if (value)
    value.value = colorHex;
}
function resetColorPicker(pickerId, valueId) {
  document.getElementById(pickerId)?.querySelectorAll(".color-swatch").forEach((b) => b.classList.remove("selected"));
  const value = document.getElementById(valueId);
  if (value)
    value.value = "";
}
function installGlobalColorPicker() {
  window.pickColor = pickColor;
}

// node_modules/sortablejs/modular/sortable.esm.js
function _defineProperty(e, r, t) {
  return (r = _toPropertyKey(r)) in e ? Object.defineProperty(e, r, {
    value: t,
    enumerable: true,
    configurable: true,
    writable: true
  }) : e[r] = t, e;
}
function _extends() {
  return _extends = Object.assign ? Object.assign.bind() : function(n) {
    for (var e = 1;e < arguments.length; e++) {
      var t = arguments[e];
      for (var r in t)
        ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]);
    }
    return n;
  }, _extends.apply(null, arguments);
}
function ownKeys(e, r) {
  var t = Object.keys(e);
  if (Object.getOwnPropertySymbols) {
    var o = Object.getOwnPropertySymbols(e);
    r && (o = o.filter(function(r2) {
      return Object.getOwnPropertyDescriptor(e, r2).enumerable;
    })), t.push.apply(t, o);
  }
  return t;
}
function _objectSpread2(e) {
  for (var r = 1;r < arguments.length; r++) {
    var t = arguments[r] != null ? arguments[r] : {};
    r % 2 ? ownKeys(Object(t), true).forEach(function(r2) {
      _defineProperty(e, r2, t[r2]);
    }) : Object.getOwnPropertyDescriptors ? Object.defineProperties(e, Object.getOwnPropertyDescriptors(t)) : ownKeys(Object(t)).forEach(function(r2) {
      Object.defineProperty(e, r2, Object.getOwnPropertyDescriptor(t, r2));
    });
  }
  return e;
}
function _objectWithoutProperties(e, t) {
  if (e == null)
    return {};
  var o, r, i = _objectWithoutPropertiesLoose(e, t);
  if (Object.getOwnPropertySymbols) {
    var n = Object.getOwnPropertySymbols(e);
    for (r = 0;r < n.length; r++)
      o = n[r], t.indexOf(o) === -1 && {}.propertyIsEnumerable.call(e, o) && (i[o] = e[o]);
  }
  return i;
}
function _objectWithoutPropertiesLoose(r, e) {
  if (r == null)
    return {};
  var t = {};
  for (var n in r)
    if ({}.hasOwnProperty.call(r, n)) {
      if (e.indexOf(n) !== -1)
        continue;
      t[n] = r[n];
    }
  return t;
}
function _toPrimitive(t, r) {
  if (typeof t != "object" || !t)
    return t;
  var e = t[Symbol.toPrimitive];
  if (e !== undefined) {
    var i = e.call(t, r || "default");
    if (typeof i != "object")
      return i;
    throw new TypeError("@@toPrimitive must return a primitive value.");
  }
  return (r === "string" ? String : Number)(t);
}
function _toPropertyKey(t) {
  var i = _toPrimitive(t, "string");
  return typeof i == "symbol" ? i : i + "";
}
function _typeof(o) {
  "@babel/helpers - typeof";
  return _typeof = typeof Symbol == "function" && typeof Symbol.iterator == "symbol" ? function(o2) {
    return typeof o2;
  } : function(o2) {
    return o2 && typeof Symbol == "function" && o2.constructor === Symbol && o2 !== Symbol.prototype ? "symbol" : typeof o2;
  }, _typeof(o);
}
var version = "1.15.7";
function userAgent(pattern) {
  if (typeof window !== "undefined" && window.navigator) {
    return !!/* @__PURE__ */ navigator.userAgent.match(pattern);
  }
}
var IE11OrLess = userAgent(/(?:Trident.*rv[ :]?11\.|msie|iemobile|Windows Phone)/i);
var Edge = userAgent(/Edge/i);
var FireFox = userAgent(/firefox/i);
var Safari = userAgent(/safari/i) && !userAgent(/chrome/i) && !userAgent(/android/i);
var IOS = userAgent(/iP(ad|od|hone)/i);
var ChromeForAndroid = userAgent(/chrome/i) && userAgent(/android/i);
var captureMode = {
  capture: false,
  passive: false
};
function on(el, event, fn) {
  el.addEventListener(event, fn, !IE11OrLess && captureMode);
}
function off(el, event, fn) {
  el.removeEventListener(event, fn, !IE11OrLess && captureMode);
}
function matches(el, selector) {
  if (!selector)
    return;
  selector[0] === ">" && (selector = selector.substring(1));
  if (el) {
    try {
      if (el.matches) {
        return el.matches(selector);
      } else if (el.msMatchesSelector) {
        return el.msMatchesSelector(selector);
      } else if (el.webkitMatchesSelector) {
        return el.webkitMatchesSelector(selector);
      }
    } catch (_) {
      return false;
    }
  }
  return false;
}
function getParentOrHost(el) {
  return el.host && el !== document && el.host.nodeType && el.host !== el ? el.host : el.parentNode;
}
function closest(el, selector, ctx, includeCTX) {
  if (el) {
    ctx = ctx || document;
    do {
      if (selector != null && (selector[0] === ">" ? el.parentNode === ctx && matches(el, selector) : matches(el, selector)) || includeCTX && el === ctx) {
        return el;
      }
      if (el === ctx)
        break;
    } while (el = getParentOrHost(el));
  }
  return null;
}
var R_SPACE = /\s+/g;
function toggleClass(el, name, state) {
  if (el && name) {
    if (el.classList) {
      el.classList[state ? "add" : "remove"](name);
    } else {
      var className = (" " + el.className + " ").replace(R_SPACE, " ").replace(" " + name + " ", " ");
      el.className = (className + (state ? " " + name : "")).replace(R_SPACE, " ");
    }
  }
}
function css(el, prop, val) {
  var style = el && el.style;
  if (style) {
    if (val === undefined) {
      if (document.defaultView && document.defaultView.getComputedStyle) {
        val = document.defaultView.getComputedStyle(el, "");
      } else if (el.currentStyle) {
        val = el.currentStyle;
      }
      return prop === undefined ? val : val[prop];
    } else {
      if (!(prop in style) && prop.indexOf("webkit") === -1) {
        prop = "-webkit-" + prop;
      }
      style[prop] = val + (typeof val === "string" ? "" : "px");
    }
  }
}
function matrix(el, selfOnly) {
  var appliedTransforms = "";
  if (typeof el === "string") {
    appliedTransforms = el;
  } else {
    do {
      var transform = css(el, "transform");
      if (transform && transform !== "none") {
        appliedTransforms = transform + " " + appliedTransforms;
      }
    } while (!selfOnly && (el = el.parentNode));
  }
  var matrixFn = window.DOMMatrix || window.WebKitCSSMatrix || window.CSSMatrix || window.MSCSSMatrix;
  return matrixFn && new matrixFn(appliedTransforms);
}
function find(ctx, tagName, iterator) {
  if (ctx) {
    var list = ctx.getElementsByTagName(tagName), i = 0, n = list.length;
    if (iterator) {
      for (;i < n; i++) {
        iterator(list[i], i);
      }
    }
    return list;
  }
  return [];
}
function getWindowScrollingElement() {
  var scrollingElement = document.scrollingElement;
  if (scrollingElement) {
    return scrollingElement;
  } else {
    return document.documentElement;
  }
}
function getRect(el, relativeToContainingBlock, relativeToNonStaticParent, undoScale, container) {
  if (!el.getBoundingClientRect && el !== window)
    return;
  var elRect, top, left, bottom, right, height, width;
  if (el !== window && el.parentNode && el !== getWindowScrollingElement()) {
    elRect = el.getBoundingClientRect();
    top = elRect.top;
    left = elRect.left;
    bottom = elRect.bottom;
    right = elRect.right;
    height = elRect.height;
    width = elRect.width;
  } else {
    top = 0;
    left = 0;
    bottom = window.innerHeight;
    right = window.innerWidth;
    height = window.innerHeight;
    width = window.innerWidth;
  }
  if ((relativeToContainingBlock || relativeToNonStaticParent) && el !== window) {
    container = container || el.parentNode;
    if (!IE11OrLess) {
      do {
        if (container && container.getBoundingClientRect && (css(container, "transform") !== "none" || relativeToNonStaticParent && css(container, "position") !== "static")) {
          var containerRect = container.getBoundingClientRect();
          top -= containerRect.top + parseInt(css(container, "border-top-width"));
          left -= containerRect.left + parseInt(css(container, "border-left-width"));
          bottom = top + elRect.height;
          right = left + elRect.width;
          break;
        }
      } while (container = container.parentNode);
    }
  }
  if (undoScale && el !== window) {
    var elMatrix = matrix(container || el), scaleX = elMatrix && elMatrix.a, scaleY = elMatrix && elMatrix.d;
    if (elMatrix) {
      top /= scaleY;
      left /= scaleX;
      width /= scaleX;
      height /= scaleY;
      bottom = top + height;
      right = left + width;
    }
  }
  return {
    top,
    left,
    bottom,
    right,
    width,
    height
  };
}
function isScrolledPast(el, elSide, parentSide) {
  var parent = getParentAutoScrollElement(el, true), elSideVal = getRect(el)[elSide];
  while (parent) {
    var parentSideVal = getRect(parent)[parentSide], visible = undefined;
    if (parentSide === "top" || parentSide === "left") {
      visible = elSideVal >= parentSideVal;
    } else {
      visible = elSideVal <= parentSideVal;
    }
    if (!visible)
      return parent;
    if (parent === getWindowScrollingElement())
      break;
    parent = getParentAutoScrollElement(parent, false);
  }
  return false;
}
function getChild(el, childNum, options, includeDragEl) {
  var currentChild = 0, i = 0, children = el.children;
  while (i < children.length) {
    if (children[i].style.display !== "none" && children[i] !== Sortable.ghost && (includeDragEl || children[i] !== Sortable.dragged) && closest(children[i], options.draggable, el, false)) {
      if (currentChild === childNum) {
        return children[i];
      }
      currentChild++;
    }
    i++;
  }
  return null;
}
function lastChild(el, selector) {
  var last = el.lastElementChild;
  while (last && (last === Sortable.ghost || css(last, "display") === "none" || selector && !matches(last, selector))) {
    last = last.previousElementSibling;
  }
  return last || null;
}
function index(el, selector) {
  var index2 = 0;
  if (!el || !el.parentNode) {
    return -1;
  }
  while (el = el.previousElementSibling) {
    if (el.nodeName.toUpperCase() !== "TEMPLATE" && el !== Sortable.clone && (!selector || matches(el, selector))) {
      index2++;
    }
  }
  return index2;
}
function getRelativeScrollOffset(el) {
  var offsetLeft = 0, offsetTop = 0, winScroller = getWindowScrollingElement();
  if (el) {
    do {
      var elMatrix = matrix(el), scaleX = elMatrix.a, scaleY = elMatrix.d;
      offsetLeft += el.scrollLeft * scaleX;
      offsetTop += el.scrollTop * scaleY;
    } while (el !== winScroller && (el = el.parentNode));
  }
  return [offsetLeft, offsetTop];
}
function indexOfObject(arr, obj) {
  for (var i in arr) {
    if (!arr.hasOwnProperty(i))
      continue;
    for (var key in obj) {
      if (obj.hasOwnProperty(key) && obj[key] === arr[i][key])
        return Number(i);
    }
  }
  return -1;
}
function getParentAutoScrollElement(el, includeSelf) {
  if (!el || !el.getBoundingClientRect)
    return getWindowScrollingElement();
  var elem = el;
  var gotSelf = false;
  do {
    if (elem.clientWidth < elem.scrollWidth || elem.clientHeight < elem.scrollHeight) {
      var elemCSS = css(elem);
      if (elem.clientWidth < elem.scrollWidth && (elemCSS.overflowX == "auto" || elemCSS.overflowX == "scroll") || elem.clientHeight < elem.scrollHeight && (elemCSS.overflowY == "auto" || elemCSS.overflowY == "scroll")) {
        if (!elem.getBoundingClientRect || elem === document.body)
          return getWindowScrollingElement();
        if (gotSelf || includeSelf)
          return elem;
        gotSelf = true;
      }
    }
  } while (elem = elem.parentNode);
  return getWindowScrollingElement();
}
function extend(dst, src) {
  if (dst && src) {
    for (var key in src) {
      if (src.hasOwnProperty(key)) {
        dst[key] = src[key];
      }
    }
  }
  return dst;
}
function isRectEqual(rect1, rect2) {
  return Math.round(rect1.top) === Math.round(rect2.top) && Math.round(rect1.left) === Math.round(rect2.left) && Math.round(rect1.height) === Math.round(rect2.height) && Math.round(rect1.width) === Math.round(rect2.width);
}
var _throttleTimeout;
function throttle(callback, ms) {
  return function() {
    if (!_throttleTimeout) {
      var args = arguments, _this = this;
      if (args.length === 1) {
        callback.call(_this, args[0]);
      } else {
        callback.apply(_this, args);
      }
      _throttleTimeout = setTimeout(function() {
        _throttleTimeout = undefined;
      }, ms);
    }
  };
}
function cancelThrottle() {
  clearTimeout(_throttleTimeout);
  _throttleTimeout = undefined;
}
function scrollBy(el, x, y) {
  el.scrollLeft += x;
  el.scrollTop += y;
}
function clone(el) {
  var Polymer = window.Polymer;
  var $ = window.jQuery || window.Zepto;
  if (Polymer && Polymer.dom) {
    return Polymer.dom(el).cloneNode(true);
  } else if ($) {
    return $(el).clone(true)[0];
  } else {
    return el.cloneNode(true);
  }
}
function getChildContainingRectFromElement(container, options, ghostEl) {
  var rect = {};
  Array.from(container.children).forEach(function(child) {
    var _rect$left, _rect$top, _rect$right, _rect$bottom;
    if (!closest(child, options.draggable, container, false) || child.animated || child === ghostEl)
      return;
    var childRect = getRect(child);
    rect.left = Math.min((_rect$left = rect.left) !== null && _rect$left !== undefined ? _rect$left : Infinity, childRect.left);
    rect.top = Math.min((_rect$top = rect.top) !== null && _rect$top !== undefined ? _rect$top : Infinity, childRect.top);
    rect.right = Math.max((_rect$right = rect.right) !== null && _rect$right !== undefined ? _rect$right : -Infinity, childRect.right);
    rect.bottom = Math.max((_rect$bottom = rect.bottom) !== null && _rect$bottom !== undefined ? _rect$bottom : -Infinity, childRect.bottom);
  });
  rect.width = rect.right - rect.left;
  rect.height = rect.bottom - rect.top;
  rect.x = rect.left;
  rect.y = rect.top;
  return rect;
}
var expando = "Sortable" + new Date().getTime();
function AnimationStateManager() {
  var animationStates = [], animationCallbackId;
  return {
    captureAnimationState: function captureAnimationState() {
      animationStates = [];
      if (!this.options.animation)
        return;
      var children = [].slice.call(this.el.children);
      children.forEach(function(child) {
        if (css(child, "display") === "none" || child === Sortable.ghost)
          return;
        animationStates.push({
          target: child,
          rect: getRect(child)
        });
        var fromRect = _objectSpread2({}, animationStates[animationStates.length - 1].rect);
        if (child.thisAnimationDuration) {
          var childMatrix = matrix(child, true);
          if (childMatrix) {
            fromRect.top -= childMatrix.f;
            fromRect.left -= childMatrix.e;
          }
        }
        child.fromRect = fromRect;
      });
    },
    addAnimationState: function addAnimationState(state) {
      animationStates.push(state);
    },
    removeAnimationState: function removeAnimationState(target) {
      animationStates.splice(indexOfObject(animationStates, {
        target
      }), 1);
    },
    animateAll: function animateAll(callback) {
      var _this = this;
      if (!this.options.animation) {
        clearTimeout(animationCallbackId);
        if (typeof callback === "function")
          callback();
        return;
      }
      var animating = false, animationTime = 0;
      animationStates.forEach(function(state) {
        var time = 0, target = state.target, fromRect = target.fromRect, toRect = getRect(target), prevFromRect = target.prevFromRect, prevToRect = target.prevToRect, animatingRect = state.rect, targetMatrix = matrix(target, true);
        if (targetMatrix) {
          toRect.top -= targetMatrix.f;
          toRect.left -= targetMatrix.e;
        }
        target.toRect = toRect;
        if (target.thisAnimationDuration) {
          if (isRectEqual(prevFromRect, toRect) && !isRectEqual(fromRect, toRect) && (animatingRect.top - toRect.top) / (animatingRect.left - toRect.left) === (fromRect.top - toRect.top) / (fromRect.left - toRect.left)) {
            time = calculateRealTime(animatingRect, prevFromRect, prevToRect, _this.options);
          }
        }
        if (!isRectEqual(toRect, fromRect)) {
          target.prevFromRect = fromRect;
          target.prevToRect = toRect;
          if (!time) {
            time = _this.options.animation;
          }
          _this.animate(target, animatingRect, toRect, time);
        }
        if (time) {
          animating = true;
          animationTime = Math.max(animationTime, time);
          clearTimeout(target.animationResetTimer);
          target.animationResetTimer = setTimeout(function() {
            target.animationTime = 0;
            target.prevFromRect = null;
            target.fromRect = null;
            target.prevToRect = null;
            target.thisAnimationDuration = null;
          }, time);
          target.thisAnimationDuration = time;
        }
      });
      clearTimeout(animationCallbackId);
      if (!animating) {
        if (typeof callback === "function")
          callback();
      } else {
        animationCallbackId = setTimeout(function() {
          if (typeof callback === "function")
            callback();
        }, animationTime);
      }
      animationStates = [];
    },
    animate: function animate(target, currentRect, toRect, duration) {
      if (duration) {
        css(target, "transition", "");
        css(target, "transform", "");
        var elMatrix = matrix(this.el), scaleX = elMatrix && elMatrix.a, scaleY = elMatrix && elMatrix.d, translateX = (currentRect.left - toRect.left) / (scaleX || 1), translateY = (currentRect.top - toRect.top) / (scaleY || 1);
        target.animatingX = !!translateX;
        target.animatingY = !!translateY;
        css(target, "transform", "translate3d(" + translateX + "px," + translateY + "px,0)");
        this.forRepaintDummy = repaint(target);
        css(target, "transition", "transform " + duration + "ms" + (this.options.easing ? " " + this.options.easing : ""));
        css(target, "transform", "translate3d(0,0,0)");
        typeof target.animated === "number" && clearTimeout(target.animated);
        target.animated = setTimeout(function() {
          css(target, "transition", "");
          css(target, "transform", "");
          target.animated = false;
          target.animatingX = false;
          target.animatingY = false;
        }, duration);
      }
    }
  };
}
function repaint(target) {
  return target.offsetWidth;
}
function calculateRealTime(animatingRect, fromRect, toRect, options) {
  return Math.sqrt(Math.pow(fromRect.top - animatingRect.top, 2) + Math.pow(fromRect.left - animatingRect.left, 2)) / Math.sqrt(Math.pow(fromRect.top - toRect.top, 2) + Math.pow(fromRect.left - toRect.left, 2)) * options.animation;
}
var plugins = [];
var defaults = {
  initializeByDefault: true
};
var PluginManager = {
  mount: function mount(plugin) {
    for (var option in defaults) {
      if (defaults.hasOwnProperty(option) && !(option in plugin)) {
        plugin[option] = defaults[option];
      }
    }
    plugins.forEach(function(p) {
      if (p.pluginName === plugin.pluginName) {
        throw "Sortable: Cannot mount plugin ".concat(plugin.pluginName, " more than once");
      }
    });
    plugins.push(plugin);
  },
  pluginEvent: function pluginEvent(eventName, sortable, evt) {
    var _this = this;
    this.eventCanceled = false;
    evt.cancel = function() {
      _this.eventCanceled = true;
    };
    var eventNameGlobal = eventName + "Global";
    plugins.forEach(function(plugin) {
      if (!sortable[plugin.pluginName])
        return;
      if (sortable[plugin.pluginName][eventNameGlobal]) {
        sortable[plugin.pluginName][eventNameGlobal](_objectSpread2({
          sortable
        }, evt));
      }
      if (sortable.options[plugin.pluginName] && sortable[plugin.pluginName][eventName]) {
        sortable[plugin.pluginName][eventName](_objectSpread2({
          sortable
        }, evt));
      }
    });
  },
  initializePlugins: function initializePlugins(sortable, el, defaults2, options) {
    plugins.forEach(function(plugin) {
      var pluginName = plugin.pluginName;
      if (!sortable.options[pluginName] && !plugin.initializeByDefault)
        return;
      var initialized = new plugin(sortable, el, sortable.options);
      initialized.sortable = sortable;
      initialized.options = sortable.options;
      sortable[pluginName] = initialized;
      _extends(defaults2, initialized.defaults);
    });
    for (var option in sortable.options) {
      if (!sortable.options.hasOwnProperty(option))
        continue;
      var modified = this.modifyOption(sortable, option, sortable.options[option]);
      if (typeof modified !== "undefined") {
        sortable.options[option] = modified;
      }
    }
  },
  getEventProperties: function getEventProperties(name, sortable) {
    var eventProperties = {};
    plugins.forEach(function(plugin) {
      if (typeof plugin.eventProperties !== "function")
        return;
      _extends(eventProperties, plugin.eventProperties.call(sortable[plugin.pluginName], name));
    });
    return eventProperties;
  },
  modifyOption: function modifyOption(sortable, name, value) {
    var modifiedValue;
    plugins.forEach(function(plugin) {
      if (!sortable[plugin.pluginName])
        return;
      if (plugin.optionListeners && typeof plugin.optionListeners[name] === "function") {
        modifiedValue = plugin.optionListeners[name].call(sortable[plugin.pluginName], value);
      }
    });
    return modifiedValue;
  }
};
function dispatchEvent(_ref) {
  var { sortable, rootEl, name, targetEl, cloneEl, toEl, fromEl, oldIndex, newIndex, oldDraggableIndex, newDraggableIndex, originalEvent, putSortable, extraEventProperties } = _ref;
  sortable = sortable || rootEl && rootEl[expando];
  if (!sortable)
    return;
  var evt, options = sortable.options, onName = "on" + name.charAt(0).toUpperCase() + name.substr(1);
  if (window.CustomEvent && !IE11OrLess && !Edge) {
    evt = new CustomEvent(name, {
      bubbles: true,
      cancelable: true
    });
  } else {
    evt = document.createEvent("Event");
    evt.initEvent(name, true, true);
  }
  evt.to = toEl || rootEl;
  evt.from = fromEl || rootEl;
  evt.item = targetEl || rootEl;
  evt.clone = cloneEl;
  evt.oldIndex = oldIndex;
  evt.newIndex = newIndex;
  evt.oldDraggableIndex = oldDraggableIndex;
  evt.newDraggableIndex = newDraggableIndex;
  evt.originalEvent = originalEvent;
  evt.pullMode = putSortable ? putSortable.lastPutMode : undefined;
  var allEventProperties = _objectSpread2(_objectSpread2({}, extraEventProperties), PluginManager.getEventProperties(name, sortable));
  for (var option in allEventProperties) {
    evt[option] = allEventProperties[option];
  }
  if (rootEl) {
    rootEl.dispatchEvent(evt);
  }
  if (options[onName]) {
    options[onName].call(sortable, evt);
  }
}
var _excluded = ["evt"];
var pluginEvent2 = function pluginEvent3(eventName, sortable) {
  var _ref = arguments.length > 2 && arguments[2] !== undefined ? arguments[2] : {}, originalEvent = _ref.evt, data = _objectWithoutProperties(_ref, _excluded);
  PluginManager.pluginEvent.bind(Sortable)(eventName, sortable, _objectSpread2({
    dragEl,
    parentEl,
    ghostEl,
    rootEl,
    nextEl,
    lastDownEl,
    cloneEl,
    cloneHidden,
    dragStarted: moved,
    putSortable,
    activeSortable: Sortable.active,
    originalEvent,
    oldIndex,
    oldDraggableIndex,
    newIndex,
    newDraggableIndex,
    hideGhostForTarget: _hideGhostForTarget,
    unhideGhostForTarget: _unhideGhostForTarget,
    cloneNowHidden: function cloneNowHidden() {
      cloneHidden = true;
    },
    cloneNowShown: function cloneNowShown() {
      cloneHidden = false;
    },
    dispatchSortableEvent: function dispatchSortableEvent(name) {
      _dispatchEvent({
        sortable,
        name,
        originalEvent
      });
    }
  }, data));
};
function _dispatchEvent(info) {
  dispatchEvent(_objectSpread2({
    putSortable,
    cloneEl,
    targetEl: dragEl,
    rootEl,
    oldIndex,
    oldDraggableIndex,
    newIndex,
    newDraggableIndex
  }, info));
}
var dragEl;
var parentEl;
var ghostEl;
var rootEl;
var nextEl;
var lastDownEl;
var cloneEl;
var cloneHidden;
var oldIndex;
var newIndex;
var oldDraggableIndex;
var newDraggableIndex;
var activeGroup;
var putSortable;
var awaitingDragStarted = false;
var ignoreNextClick = false;
var sortables = [];
var tapEvt;
var touchEvt;
var lastDx;
var lastDy;
var tapDistanceLeft;
var tapDistanceTop;
var moved;
var lastTarget;
var lastDirection;
var pastFirstInvertThresh = false;
var isCircumstantialInvert = false;
var targetMoveDistance;
var ghostRelativeParent;
var ghostRelativeParentInitialScroll = [];
var _silent = false;
var savedInputChecked = [];
var documentExists = typeof document !== "undefined";
var PositionGhostAbsolutely = IOS;
var CSSFloatProperty = Edge || IE11OrLess ? "cssFloat" : "float";
var supportDraggable = documentExists && !ChromeForAndroid && !IOS && "draggable" in document.createElement("div");
var supportCssPointerEvents = function() {
  if (!documentExists)
    return;
  if (IE11OrLess) {
    return false;
  }
  var el = document.createElement("x");
  el.style.cssText = "pointer-events:auto";
  return el.style.pointerEvents === "auto";
}();
var _detectDirection = function _detectDirection2(el, options) {
  var elCSS = css(el), elWidth = parseInt(elCSS.width) - parseInt(elCSS.paddingLeft) - parseInt(elCSS.paddingRight) - parseInt(elCSS.borderLeftWidth) - parseInt(elCSS.borderRightWidth), child1 = getChild(el, 0, options), child2 = getChild(el, 1, options), firstChildCSS = child1 && css(child1), secondChildCSS = child2 && css(child2), firstChildWidth = firstChildCSS && parseInt(firstChildCSS.marginLeft) + parseInt(firstChildCSS.marginRight) + getRect(child1).width, secondChildWidth = secondChildCSS && parseInt(secondChildCSS.marginLeft) + parseInt(secondChildCSS.marginRight) + getRect(child2).width;
  if (elCSS.display === "flex") {
    return elCSS.flexDirection === "column" || elCSS.flexDirection === "column-reverse" ? "vertical" : "horizontal";
  }
  if (elCSS.display === "grid") {
    return elCSS.gridTemplateColumns.split(" ").length <= 1 ? "vertical" : "horizontal";
  }
  if (child1 && firstChildCSS["float"] && firstChildCSS["float"] !== "none") {
    var touchingSideChild2 = firstChildCSS["float"] === "left" ? "left" : "right";
    return child2 && (secondChildCSS.clear === "both" || secondChildCSS.clear === touchingSideChild2) ? "vertical" : "horizontal";
  }
  return child1 && (firstChildCSS.display === "block" || firstChildCSS.display === "flex" || firstChildCSS.display === "table" || firstChildCSS.display === "grid" || firstChildWidth >= elWidth && elCSS[CSSFloatProperty] === "none" || child2 && elCSS[CSSFloatProperty] === "none" && firstChildWidth + secondChildWidth > elWidth) ? "vertical" : "horizontal";
};
var _dragElInRowColumn = function _dragElInRowColumn2(dragRect, targetRect, vertical) {
  var dragElS1Opp = vertical ? dragRect.left : dragRect.top, dragElS2Opp = vertical ? dragRect.right : dragRect.bottom, dragElOppLength = vertical ? dragRect.width : dragRect.height, targetS1Opp = vertical ? targetRect.left : targetRect.top, targetS2Opp = vertical ? targetRect.right : targetRect.bottom, targetOppLength = vertical ? targetRect.width : targetRect.height;
  return dragElS1Opp === targetS1Opp || dragElS2Opp === targetS2Opp || dragElS1Opp + dragElOppLength / 2 === targetS1Opp + targetOppLength / 2;
};
var _detectNearestEmptySortable = function _detectNearestEmptySortable2(x, y) {
  var ret;
  sortables.some(function(sortable) {
    var threshold = sortable[expando].options.emptyInsertThreshold;
    if (!threshold || lastChild(sortable))
      return;
    var rect = getRect(sortable), insideHorizontally = x >= rect.left - threshold && x <= rect.right + threshold, insideVertically = y >= rect.top - threshold && y <= rect.bottom + threshold;
    if (insideHorizontally && insideVertically) {
      return ret = sortable;
    }
  });
  return ret;
};
var _prepareGroup = function _prepareGroup2(options) {
  function toFn(value, pull) {
    return function(to, from, dragEl2, evt) {
      var sameGroup = to.options.group.name && from.options.group.name && to.options.group.name === from.options.group.name;
      if (value == null && (pull || sameGroup)) {
        return true;
      } else if (value == null || value === false) {
        return false;
      } else if (pull && value === "clone") {
        return value;
      } else if (typeof value === "function") {
        return toFn(value(to, from, dragEl2, evt), pull)(to, from, dragEl2, evt);
      } else {
        var otherGroup = (pull ? to : from).options.group.name;
        return value === true || typeof value === "string" && value === otherGroup || value.join && value.indexOf(otherGroup) > -1;
      }
    };
  }
  var group = {};
  var originalGroup = options.group;
  if (!originalGroup || _typeof(originalGroup) != "object") {
    originalGroup = {
      name: originalGroup
    };
  }
  group.name = originalGroup.name;
  group.checkPull = toFn(originalGroup.pull, true);
  group.checkPut = toFn(originalGroup.put);
  group.revertClone = originalGroup.revertClone;
  options.group = group;
};
var _hideGhostForTarget = function _hideGhostForTarget2() {
  if (!supportCssPointerEvents && ghostEl) {
    css(ghostEl, "display", "none");
  }
};
var _unhideGhostForTarget = function _unhideGhostForTarget2() {
  if (!supportCssPointerEvents && ghostEl) {
    css(ghostEl, "display", "");
  }
};
if (documentExists && !ChromeForAndroid) {
  document.addEventListener("click", function(evt) {
    if (ignoreNextClick) {
      evt.preventDefault();
      evt.stopPropagation && evt.stopPropagation();
      evt.stopImmediatePropagation && evt.stopImmediatePropagation();
      ignoreNextClick = false;
      return false;
    }
  }, true);
}
var nearestEmptyInsertDetectEvent = function nearestEmptyInsertDetectEvent2(evt) {
  if (dragEl) {
    evt = evt.touches ? evt.touches[0] : evt;
    var nearest = _detectNearestEmptySortable(evt.clientX, evt.clientY);
    if (nearest) {
      var event = {};
      for (var i in evt) {
        if (evt.hasOwnProperty(i)) {
          event[i] = evt[i];
        }
      }
      event.target = event.rootEl = nearest;
      event.preventDefault = undefined;
      event.stopPropagation = undefined;
      nearest[expando]._onDragOver(event);
    }
  }
};
var _checkOutsideTargetEl = function _checkOutsideTargetEl2(evt) {
  if (dragEl) {
    dragEl.parentNode[expando]._isOutsideThisEl(evt.target);
  }
};
function Sortable(el, options) {
  if (!(el && el.nodeType && el.nodeType === 1)) {
    throw "Sortable: `el` must be an HTMLElement, not ".concat({}.toString.call(el));
  }
  this.el = el;
  this.options = options = _extends({}, options);
  el[expando] = this;
  var defaults2 = {
    group: null,
    sort: true,
    disabled: false,
    store: null,
    handle: null,
    draggable: /^[uo]l$/i.test(el.nodeName) ? ">li" : ">*",
    swapThreshold: 1,
    invertSwap: false,
    invertedSwapThreshold: null,
    removeCloneOnHide: true,
    direction: function direction() {
      return _detectDirection(el, this.options);
    },
    ghostClass: "sortable-ghost",
    chosenClass: "sortable-chosen",
    dragClass: "sortable-drag",
    ignore: "a, img",
    filter: null,
    preventOnFilter: true,
    animation: 0,
    easing: null,
    setData: function setData(dataTransfer, dragEl2) {
      dataTransfer.setData("Text", dragEl2.textContent);
    },
    dropBubble: false,
    dragoverBubble: false,
    dataIdAttr: "data-id",
    delay: 0,
    delayOnTouchOnly: false,
    touchStartThreshold: (Number.parseInt ? Number : window).parseInt(window.devicePixelRatio, 10) || 1,
    forceFallback: false,
    fallbackClass: "sortable-fallback",
    fallbackOnBody: false,
    fallbackTolerance: 0,
    fallbackOffset: {
      x: 0,
      y: 0
    },
    supportPointer: Sortable.supportPointer !== false && "PointerEvent" in window && (!Safari || IOS),
    emptyInsertThreshold: 5
  };
  PluginManager.initializePlugins(this, el, defaults2);
  for (var name in defaults2) {
    !(name in options) && (options[name] = defaults2[name]);
  }
  _prepareGroup(options);
  for (var fn in this) {
    if (fn.charAt(0) === "_" && typeof this[fn] === "function") {
      this[fn] = this[fn].bind(this);
    }
  }
  this.nativeDraggable = options.forceFallback ? false : supportDraggable;
  if (this.nativeDraggable) {
    this.options.touchStartThreshold = 1;
  }
  if (options.supportPointer) {
    on(el, "pointerdown", this._onTapStart);
  } else {
    on(el, "mousedown", this._onTapStart);
    on(el, "touchstart", this._onTapStart);
  }
  if (this.nativeDraggable) {
    on(el, "dragover", this);
    on(el, "dragenter", this);
  }
  sortables.push(this.el);
  options.store && options.store.get && this.sort(options.store.get(this) || []);
  _extends(this, AnimationStateManager());
}
Sortable.prototype = {
  constructor: Sortable,
  _isOutsideThisEl: function _isOutsideThisEl(target) {
    if (!this.el.contains(target) && target !== this.el) {
      lastTarget = null;
    }
  },
  _getDirection: function _getDirection(evt, target) {
    return typeof this.options.direction === "function" ? this.options.direction.call(this, evt, target, dragEl) : this.options.direction;
  },
  _onTapStart: function _onTapStart(evt) {
    if (!evt.cancelable)
      return;
    var _this = this, el = this.el, options = this.options, preventOnFilter = options.preventOnFilter, type = evt.type, touch = evt.touches && evt.touches[0] || evt.pointerType && evt.pointerType === "touch" && evt, target = (touch || evt).target, originalTarget = evt.target.shadowRoot && (evt.path && evt.path[0] || evt.composedPath && evt.composedPath()[0]) || target, filter = options.filter;
    _saveInputCheckedState(el);
    if (dragEl) {
      return;
    }
    if (/mousedown|pointerdown/.test(type) && evt.button !== 0 || options.disabled) {
      return;
    }
    if (originalTarget.isContentEditable) {
      return;
    }
    if (!this.nativeDraggable && Safari && target && target.tagName.toUpperCase() === "SELECT") {
      return;
    }
    target = closest(target, options.draggable, el, false);
    if (target && target.animated) {
      return;
    }
    if (lastDownEl === target) {
      return;
    }
    oldIndex = index(target);
    oldDraggableIndex = index(target, options.draggable);
    if (typeof filter === "function") {
      if (filter.call(this, evt, target, this)) {
        _dispatchEvent({
          sortable: _this,
          rootEl: originalTarget,
          name: "filter",
          targetEl: target,
          toEl: el,
          fromEl: el
        });
        pluginEvent2("filter", _this, {
          evt
        });
        preventOnFilter && evt.preventDefault();
        return;
      }
    } else if (filter) {
      filter = filter.split(",").some(function(criteria) {
        criteria = closest(originalTarget, criteria.trim(), el, false);
        if (criteria) {
          _dispatchEvent({
            sortable: _this,
            rootEl: criteria,
            name: "filter",
            targetEl: target,
            fromEl: el,
            toEl: el
          });
          pluginEvent2("filter", _this, {
            evt
          });
          return true;
        }
      });
      if (filter) {
        preventOnFilter && evt.preventDefault();
        return;
      }
    }
    if (options.handle && !closest(originalTarget, options.handle, el, false)) {
      return;
    }
    this._prepareDragStart(evt, touch, target);
  },
  _prepareDragStart: function _prepareDragStart(evt, touch, target) {
    var _this = this, el = _this.el, options = _this.options, ownerDocument = el.ownerDocument, dragStartFn;
    if (target && !dragEl && target.parentNode === el) {
      var dragRect = getRect(target);
      rootEl = el;
      dragEl = target;
      parentEl = dragEl.parentNode;
      nextEl = dragEl.nextSibling;
      lastDownEl = target;
      activeGroup = options.group;
      Sortable.dragged = dragEl;
      tapEvt = {
        target: dragEl,
        clientX: (touch || evt).clientX,
        clientY: (touch || evt).clientY
      };
      tapDistanceLeft = tapEvt.clientX - dragRect.left;
      tapDistanceTop = tapEvt.clientY - dragRect.top;
      this._lastX = (touch || evt).clientX;
      this._lastY = (touch || evt).clientY;
      dragEl.style["will-change"] = "all";
      dragStartFn = function dragStartFn2() {
        pluginEvent2("delayEnded", _this, {
          evt
        });
        if (Sortable.eventCanceled) {
          _this._onDrop();
          return;
        }
        _this._disableDelayedDragEvents();
        if (!FireFox && _this.nativeDraggable) {
          dragEl.draggable = true;
        }
        _this._triggerDragStart(evt, touch);
        _dispatchEvent({
          sortable: _this,
          name: "choose",
          originalEvent: evt
        });
        toggleClass(dragEl, options.chosenClass, true);
      };
      options.ignore.split(",").forEach(function(criteria) {
        find(dragEl, criteria.trim(), _disableDraggable);
      });
      on(ownerDocument, "dragover", nearestEmptyInsertDetectEvent);
      on(ownerDocument, "mousemove", nearestEmptyInsertDetectEvent);
      on(ownerDocument, "touchmove", nearestEmptyInsertDetectEvent);
      if (options.supportPointer) {
        on(ownerDocument, "pointerup", _this._onDrop);
        !this.nativeDraggable && on(ownerDocument, "pointercancel", _this._onDrop);
      } else {
        on(ownerDocument, "mouseup", _this._onDrop);
        on(ownerDocument, "touchend", _this._onDrop);
        on(ownerDocument, "touchcancel", _this._onDrop);
      }
      if (FireFox && this.nativeDraggable) {
        this.options.touchStartThreshold = 4;
        dragEl.draggable = true;
      }
      pluginEvent2("delayStart", this, {
        evt
      });
      if (options.delay && (!options.delayOnTouchOnly || touch) && (!this.nativeDraggable || !(Edge || IE11OrLess))) {
        if (Sortable.eventCanceled) {
          this._onDrop();
          return;
        }
        if (options.supportPointer) {
          on(ownerDocument, "pointerup", _this._disableDelayedDrag);
          on(ownerDocument, "pointercancel", _this._disableDelayedDrag);
        } else {
          on(ownerDocument, "mouseup", _this._disableDelayedDrag);
          on(ownerDocument, "touchend", _this._disableDelayedDrag);
          on(ownerDocument, "touchcancel", _this._disableDelayedDrag);
        }
        on(ownerDocument, "mousemove", _this._delayedDragTouchMoveHandler);
        on(ownerDocument, "touchmove", _this._delayedDragTouchMoveHandler);
        options.supportPointer && on(ownerDocument, "pointermove", _this._delayedDragTouchMoveHandler);
        _this._dragStartTimer = setTimeout(dragStartFn, options.delay);
      } else {
        dragStartFn();
      }
    }
  },
  _delayedDragTouchMoveHandler: function _delayedDragTouchMoveHandler(e) {
    var touch = e.touches ? e.touches[0] : e;
    if (Math.max(Math.abs(touch.clientX - this._lastX), Math.abs(touch.clientY - this._lastY)) >= Math.floor(this.options.touchStartThreshold / (this.nativeDraggable && window.devicePixelRatio || 1))) {
      this._disableDelayedDrag();
    }
  },
  _disableDelayedDrag: function _disableDelayedDrag() {
    dragEl && _disableDraggable(dragEl);
    clearTimeout(this._dragStartTimer);
    this._disableDelayedDragEvents();
  },
  _disableDelayedDragEvents: function _disableDelayedDragEvents() {
    var ownerDocument = this.el.ownerDocument;
    off(ownerDocument, "mouseup", this._disableDelayedDrag);
    off(ownerDocument, "touchend", this._disableDelayedDrag);
    off(ownerDocument, "touchcancel", this._disableDelayedDrag);
    off(ownerDocument, "pointerup", this._disableDelayedDrag);
    off(ownerDocument, "pointercancel", this._disableDelayedDrag);
    off(ownerDocument, "mousemove", this._delayedDragTouchMoveHandler);
    off(ownerDocument, "touchmove", this._delayedDragTouchMoveHandler);
    off(ownerDocument, "pointermove", this._delayedDragTouchMoveHandler);
  },
  _triggerDragStart: function _triggerDragStart(evt, touch) {
    touch = touch || evt.pointerType == "touch" && evt;
    if (!this.nativeDraggable || touch) {
      if (this.options.supportPointer) {
        on(document, "pointermove", this._onTouchMove);
      } else if (touch) {
        on(document, "touchmove", this._onTouchMove);
      } else {
        on(document, "mousemove", this._onTouchMove);
      }
    } else {
      on(dragEl, "dragend", this);
      on(rootEl, "dragstart", this._onDragStart);
    }
    try {
      if (document.selection) {
        _nextTick(function() {
          document.selection.empty();
        });
      } else {
        window.getSelection().removeAllRanges();
      }
    } catch (err) {}
  },
  _dragStarted: function _dragStarted(fallback, evt) {
    awaitingDragStarted = false;
    if (rootEl && dragEl) {
      pluginEvent2("dragStarted", this, {
        evt
      });
      if (this.nativeDraggable) {
        on(document, "dragover", _checkOutsideTargetEl);
      }
      var options = this.options;
      !fallback && toggleClass(dragEl, options.dragClass, false);
      toggleClass(dragEl, options.ghostClass, true);
      Sortable.active = this;
      fallback && this._appendGhost();
      _dispatchEvent({
        sortable: this,
        name: "start",
        originalEvent: evt
      });
    } else {
      this._nulling();
    }
  },
  _emulateDragOver: function _emulateDragOver() {
    if (touchEvt) {
      this._lastX = touchEvt.clientX;
      this._lastY = touchEvt.clientY;
      _hideGhostForTarget();
      var target = document.elementFromPoint(touchEvt.clientX, touchEvt.clientY);
      var parent = target;
      while (target && target.shadowRoot) {
        target = target.shadowRoot.elementFromPoint(touchEvt.clientX, touchEvt.clientY);
        if (target === parent)
          break;
        parent = target;
      }
      dragEl.parentNode[expando]._isOutsideThisEl(target);
      if (parent) {
        do {
          if (parent[expando]) {
            var inserted = undefined;
            inserted = parent[expando]._onDragOver({
              clientX: touchEvt.clientX,
              clientY: touchEvt.clientY,
              target,
              rootEl: parent
            });
            if (inserted && !this.options.dragoverBubble) {
              break;
            }
          }
          target = parent;
        } while (parent = getParentOrHost(parent));
      }
      _unhideGhostForTarget();
    }
  },
  _onTouchMove: function _onTouchMove(evt) {
    if (tapEvt) {
      var options = this.options, fallbackTolerance = options.fallbackTolerance, fallbackOffset = options.fallbackOffset, touch = evt.touches ? evt.touches[0] : evt, ghostMatrix = ghostEl && matrix(ghostEl, true), scaleX = ghostEl && ghostMatrix && ghostMatrix.a, scaleY = ghostEl && ghostMatrix && ghostMatrix.d, relativeScrollOffset = PositionGhostAbsolutely && ghostRelativeParent && getRelativeScrollOffset(ghostRelativeParent), dx = (touch.clientX - tapEvt.clientX + fallbackOffset.x) / (scaleX || 1) + (relativeScrollOffset ? relativeScrollOffset[0] - ghostRelativeParentInitialScroll[0] : 0) / (scaleX || 1), dy = (touch.clientY - tapEvt.clientY + fallbackOffset.y) / (scaleY || 1) + (relativeScrollOffset ? relativeScrollOffset[1] - ghostRelativeParentInitialScroll[1] : 0) / (scaleY || 1);
      if (!Sortable.active && !awaitingDragStarted) {
        if (fallbackTolerance && Math.max(Math.abs(touch.clientX - this._lastX), Math.abs(touch.clientY - this._lastY)) < fallbackTolerance) {
          return;
        }
        this._onDragStart(evt, true);
      }
      if (ghostEl) {
        if (ghostMatrix) {
          ghostMatrix.e += dx - (lastDx || 0);
          ghostMatrix.f += dy - (lastDy || 0);
        } else {
          ghostMatrix = {
            a: 1,
            b: 0,
            c: 0,
            d: 1,
            e: dx,
            f: dy
          };
        }
        var cssMatrix = "matrix(".concat(ghostMatrix.a, ",").concat(ghostMatrix.b, ",").concat(ghostMatrix.c, ",").concat(ghostMatrix.d, ",").concat(ghostMatrix.e, ",").concat(ghostMatrix.f, ")");
        css(ghostEl, "webkitTransform", cssMatrix);
        css(ghostEl, "mozTransform", cssMatrix);
        css(ghostEl, "msTransform", cssMatrix);
        css(ghostEl, "transform", cssMatrix);
        lastDx = dx;
        lastDy = dy;
        touchEvt = touch;
      }
      evt.cancelable && evt.preventDefault();
    }
  },
  _appendGhost: function _appendGhost() {
    if (!ghostEl) {
      var container = this.options.fallbackOnBody ? document.body : rootEl, rect = getRect(dragEl, true, PositionGhostAbsolutely, true, container), options = this.options;
      if (PositionGhostAbsolutely) {
        ghostRelativeParent = container;
        while (css(ghostRelativeParent, "position") === "static" && css(ghostRelativeParent, "transform") === "none" && ghostRelativeParent !== document) {
          ghostRelativeParent = ghostRelativeParent.parentNode;
        }
        if (ghostRelativeParent !== document.body && ghostRelativeParent !== document.documentElement) {
          if (ghostRelativeParent === document)
            ghostRelativeParent = getWindowScrollingElement();
          rect.top += ghostRelativeParent.scrollTop;
          rect.left += ghostRelativeParent.scrollLeft;
        } else {
          ghostRelativeParent = getWindowScrollingElement();
        }
        ghostRelativeParentInitialScroll = getRelativeScrollOffset(ghostRelativeParent);
      }
      ghostEl = dragEl.cloneNode(true);
      toggleClass(ghostEl, options.ghostClass, false);
      toggleClass(ghostEl, options.fallbackClass, true);
      toggleClass(ghostEl, options.dragClass, true);
      css(ghostEl, "transition", "");
      css(ghostEl, "transform", "");
      css(ghostEl, "box-sizing", "border-box");
      css(ghostEl, "margin", 0);
      css(ghostEl, "top", rect.top);
      css(ghostEl, "left", rect.left);
      css(ghostEl, "width", rect.width);
      css(ghostEl, "height", rect.height);
      css(ghostEl, "opacity", "0.8");
      css(ghostEl, "position", PositionGhostAbsolutely ? "absolute" : "fixed");
      css(ghostEl, "zIndex", "100000");
      css(ghostEl, "pointerEvents", "none");
      Sortable.ghost = ghostEl;
      container.appendChild(ghostEl);
      css(ghostEl, "transform-origin", tapDistanceLeft / parseInt(ghostEl.style.width) * 100 + "% " + tapDistanceTop / parseInt(ghostEl.style.height) * 100 + "%");
    }
  },
  _onDragStart: function _onDragStart(evt, fallback) {
    var _this = this;
    var dataTransfer = evt.dataTransfer;
    var options = _this.options;
    pluginEvent2("dragStart", this, {
      evt
    });
    if (Sortable.eventCanceled) {
      this._onDrop();
      return;
    }
    pluginEvent2("setupClone", this);
    if (!Sortable.eventCanceled) {
      cloneEl = clone(dragEl);
      cloneEl.removeAttribute("id");
      cloneEl.draggable = false;
      cloneEl.style["will-change"] = "";
      this._hideClone();
      toggleClass(cloneEl, this.options.chosenClass, false);
      Sortable.clone = cloneEl;
    }
    _this.cloneId = _nextTick(function() {
      pluginEvent2("clone", _this);
      if (Sortable.eventCanceled)
        return;
      if (!_this.options.removeCloneOnHide) {
        rootEl.insertBefore(cloneEl, dragEl);
      }
      _this._hideClone();
      _dispatchEvent({
        sortable: _this,
        name: "clone"
      });
    });
    !fallback && toggleClass(dragEl, options.dragClass, true);
    if (fallback) {
      ignoreNextClick = true;
      _this._loopId = setInterval(_this._emulateDragOver, 50);
    } else {
      off(document, "mouseup", _this._onDrop);
      off(document, "touchend", _this._onDrop);
      off(document, "touchcancel", _this._onDrop);
      if (dataTransfer) {
        dataTransfer.effectAllowed = "move";
        options.setData && options.setData.call(_this, dataTransfer, dragEl);
      }
      on(document, "drop", _this);
      css(dragEl, "transform", "translateZ(0)");
    }
    awaitingDragStarted = true;
    _this._dragStartId = _nextTick(_this._dragStarted.bind(_this, fallback, evt));
    on(document, "selectstart", _this);
    moved = true;
    window.getSelection().removeAllRanges();
    if (Safari) {
      css(document.body, "user-select", "none");
    }
  },
  _onDragOver: function _onDragOver(evt) {
    var el = this.el, target = evt.target, dragRect, targetRect, revert, options = this.options, group = options.group, activeSortable = Sortable.active, isOwner = activeGroup === group, canSort = options.sort, fromSortable = putSortable || activeSortable, vertical, _this = this, completedFired = false;
    if (_silent)
      return;
    function dragOverEvent(name, extra) {
      pluginEvent2(name, _this, _objectSpread2({
        evt,
        isOwner,
        axis: vertical ? "vertical" : "horizontal",
        revert,
        dragRect,
        targetRect,
        canSort,
        fromSortable,
        target,
        completed,
        onMove: function onMove(target2, after2) {
          return _onMove(rootEl, el, dragEl, dragRect, target2, getRect(target2), evt, after2);
        },
        changed
      }, extra));
    }
    function capture() {
      dragOverEvent("dragOverAnimationCapture");
      _this.captureAnimationState();
      if (_this !== fromSortable) {
        fromSortable.captureAnimationState();
      }
    }
    function completed(insertion) {
      dragOverEvent("dragOverCompleted", {
        insertion
      });
      if (insertion) {
        if (isOwner) {
          activeSortable._hideClone();
        } else {
          activeSortable._showClone(_this);
        }
        if (_this !== fromSortable) {
          toggleClass(dragEl, putSortable ? putSortable.options.ghostClass : activeSortable.options.ghostClass, false);
          toggleClass(dragEl, options.ghostClass, true);
        }
        if (putSortable !== _this && _this !== Sortable.active) {
          putSortable = _this;
        } else if (_this === Sortable.active && putSortable) {
          putSortable = null;
        }
        if (fromSortable === _this) {
          _this._ignoreWhileAnimating = target;
        }
        _this.animateAll(function() {
          dragOverEvent("dragOverAnimationComplete");
          _this._ignoreWhileAnimating = null;
        });
        if (_this !== fromSortable) {
          fromSortable.animateAll();
          fromSortable._ignoreWhileAnimating = null;
        }
      }
      if (target === dragEl && !dragEl.animated || target === el && !target.animated) {
        lastTarget = null;
      }
      if (!options.dragoverBubble && !evt.rootEl && target !== document) {
        dragEl.parentNode[expando]._isOutsideThisEl(evt.target);
        !insertion && nearestEmptyInsertDetectEvent(evt);
      }
      !options.dragoverBubble && evt.stopPropagation && evt.stopPropagation();
      return completedFired = true;
    }
    function changed() {
      newIndex = index(dragEl);
      newDraggableIndex = index(dragEl, options.draggable);
      _dispatchEvent({
        sortable: _this,
        name: "change",
        toEl: el,
        newIndex,
        newDraggableIndex,
        originalEvent: evt
      });
    }
    if (evt.preventDefault !== undefined) {
      evt.cancelable && evt.preventDefault();
    }
    target = closest(target, options.draggable, el, true);
    dragOverEvent("dragOver");
    if (Sortable.eventCanceled)
      return completedFired;
    if (dragEl.contains(evt.target) || target.animated && target.animatingX && target.animatingY || _this._ignoreWhileAnimating === target) {
      return completed(false);
    }
    ignoreNextClick = false;
    if (activeSortable && !options.disabled && (isOwner ? canSort || (revert = parentEl !== rootEl) : putSortable === this || (this.lastPutMode = activeGroup.checkPull(this, activeSortable, dragEl, evt)) && group.checkPut(this, activeSortable, dragEl, evt))) {
      vertical = this._getDirection(evt, target) === "vertical";
      dragRect = getRect(dragEl);
      dragOverEvent("dragOverValid");
      if (Sortable.eventCanceled)
        return completedFired;
      if (revert) {
        parentEl = rootEl;
        capture();
        this._hideClone();
        dragOverEvent("revert");
        if (!Sortable.eventCanceled) {
          if (nextEl) {
            rootEl.insertBefore(dragEl, nextEl);
          } else {
            rootEl.appendChild(dragEl);
          }
        }
        return completed(true);
      }
      var elLastChild = lastChild(el, options.draggable);
      if (!elLastChild || _ghostIsLast(evt, vertical, this) && !elLastChild.animated) {
        if (elLastChild === dragEl) {
          return completed(false);
        }
        if (elLastChild && el === evt.target) {
          target = elLastChild;
        }
        if (target) {
          targetRect = getRect(target);
        }
        if (_onMove(rootEl, el, dragEl, dragRect, target, targetRect, evt, !!target) !== false) {
          capture();
          if (elLastChild && elLastChild.nextSibling) {
            el.insertBefore(dragEl, elLastChild.nextSibling);
          } else {
            el.appendChild(dragEl);
          }
          parentEl = el;
          changed();
          return completed(true);
        }
      } else if (elLastChild && _ghostIsFirst(evt, vertical, this)) {
        var firstChild = getChild(el, 0, options, true);
        if (firstChild === dragEl) {
          return completed(false);
        }
        target = firstChild;
        targetRect = getRect(target);
        if (_onMove(rootEl, el, dragEl, dragRect, target, targetRect, evt, false) !== false) {
          capture();
          el.insertBefore(dragEl, firstChild);
          parentEl = el;
          changed();
          return completed(true);
        }
      } else if (target.parentNode === el) {
        targetRect = getRect(target);
        var direction = 0, targetBeforeFirstSwap, differentLevel = dragEl.parentNode !== el, differentRowCol = !_dragElInRowColumn(dragEl.animated && dragEl.toRect || dragRect, target.animated && target.toRect || targetRect, vertical), side1 = vertical ? "top" : "left", scrolledPastTop = isScrolledPast(target, "top", "top") || isScrolledPast(dragEl, "top", "top"), scrollBefore = scrolledPastTop ? scrolledPastTop.scrollTop : undefined;
        if (lastTarget !== target) {
          targetBeforeFirstSwap = targetRect[side1];
          pastFirstInvertThresh = false;
          isCircumstantialInvert = !differentRowCol && options.invertSwap || differentLevel;
        }
        direction = _getSwapDirection(evt, target, targetRect, vertical, differentRowCol ? 1 : options.swapThreshold, options.invertedSwapThreshold == null ? options.swapThreshold : options.invertedSwapThreshold, isCircumstantialInvert, lastTarget === target);
        var sibling;
        if (direction !== 0) {
          var dragIndex = index(dragEl);
          do {
            dragIndex -= direction;
            sibling = parentEl.children[dragIndex];
          } while (sibling && (css(sibling, "display") === "none" || sibling === ghostEl));
        }
        if (direction === 0 || sibling === target) {
          return completed(false);
        }
        lastTarget = target;
        lastDirection = direction;
        var nextSibling = target.nextElementSibling, after = false;
        after = direction === 1;
        var moveVector = _onMove(rootEl, el, dragEl, dragRect, target, targetRect, evt, after);
        if (moveVector !== false) {
          if (moveVector === 1 || moveVector === -1) {
            after = moveVector === 1;
          }
          _silent = true;
          setTimeout(_unsilent, 30);
          capture();
          if (after && !nextSibling) {
            el.appendChild(dragEl);
          } else {
            target.parentNode.insertBefore(dragEl, after ? nextSibling : target);
          }
          if (scrolledPastTop) {
            scrollBy(scrolledPastTop, 0, scrollBefore - scrolledPastTop.scrollTop);
          }
          parentEl = dragEl.parentNode;
          if (targetBeforeFirstSwap !== undefined && !isCircumstantialInvert) {
            targetMoveDistance = Math.abs(targetBeforeFirstSwap - getRect(target)[side1]);
          }
          changed();
          return completed(true);
        }
      }
      if (el.contains(dragEl)) {
        return completed(false);
      }
    }
    return false;
  },
  _ignoreWhileAnimating: null,
  _offMoveEvents: function _offMoveEvents() {
    off(document, "mousemove", this._onTouchMove);
    off(document, "touchmove", this._onTouchMove);
    off(document, "pointermove", this._onTouchMove);
    off(document, "dragover", nearestEmptyInsertDetectEvent);
    off(document, "mousemove", nearestEmptyInsertDetectEvent);
    off(document, "touchmove", nearestEmptyInsertDetectEvent);
  },
  _offUpEvents: function _offUpEvents() {
    var ownerDocument = this.el.ownerDocument;
    off(ownerDocument, "mouseup", this._onDrop);
    off(ownerDocument, "touchend", this._onDrop);
    off(ownerDocument, "pointerup", this._onDrop);
    off(ownerDocument, "pointercancel", this._onDrop);
    off(ownerDocument, "touchcancel", this._onDrop);
    off(document, "selectstart", this);
  },
  _onDrop: function _onDrop(evt) {
    var el = this.el, options = this.options;
    newIndex = index(dragEl);
    newDraggableIndex = index(dragEl, options.draggable);
    pluginEvent2("drop", this, {
      evt
    });
    parentEl = dragEl && dragEl.parentNode;
    newIndex = index(dragEl);
    newDraggableIndex = index(dragEl, options.draggable);
    if (Sortable.eventCanceled) {
      this._nulling();
      return;
    }
    awaitingDragStarted = false;
    isCircumstantialInvert = false;
    pastFirstInvertThresh = false;
    clearInterval(this._loopId);
    clearTimeout(this._dragStartTimer);
    _cancelNextTick(this.cloneId);
    _cancelNextTick(this._dragStartId);
    if (this.nativeDraggable) {
      off(document, "drop", this);
      off(el, "dragstart", this._onDragStart);
    }
    this._offMoveEvents();
    this._offUpEvents();
    if (Safari) {
      css(document.body, "user-select", "");
    }
    css(dragEl, "transform", "");
    if (evt) {
      if (moved) {
        evt.cancelable && evt.preventDefault();
        !options.dropBubble && evt.stopPropagation();
      }
      ghostEl && ghostEl.parentNode && ghostEl.parentNode.removeChild(ghostEl);
      if (rootEl === parentEl || putSortable && putSortable.lastPutMode !== "clone") {
        cloneEl && cloneEl.parentNode && cloneEl.parentNode.removeChild(cloneEl);
      }
      if (dragEl) {
        if (this.nativeDraggable) {
          off(dragEl, "dragend", this);
        }
        _disableDraggable(dragEl);
        dragEl.style["will-change"] = "";
        if (moved && !awaitingDragStarted) {
          toggleClass(dragEl, putSortable ? putSortable.options.ghostClass : this.options.ghostClass, false);
        }
        toggleClass(dragEl, this.options.chosenClass, false);
        _dispatchEvent({
          sortable: this,
          name: "unchoose",
          toEl: parentEl,
          newIndex: null,
          newDraggableIndex: null,
          originalEvent: evt
        });
        if (rootEl !== parentEl) {
          if (newIndex >= 0) {
            _dispatchEvent({
              rootEl: parentEl,
              name: "add",
              toEl: parentEl,
              fromEl: rootEl,
              originalEvent: evt
            });
            _dispatchEvent({
              sortable: this,
              name: "remove",
              toEl: parentEl,
              originalEvent: evt
            });
            _dispatchEvent({
              rootEl: parentEl,
              name: "sort",
              toEl: parentEl,
              fromEl: rootEl,
              originalEvent: evt
            });
            _dispatchEvent({
              sortable: this,
              name: "sort",
              toEl: parentEl,
              originalEvent: evt
            });
          }
          putSortable && putSortable.save();
        } else {
          if (newIndex !== oldIndex) {
            if (newIndex >= 0) {
              _dispatchEvent({
                sortable: this,
                name: "update",
                toEl: parentEl,
                originalEvent: evt
              });
              _dispatchEvent({
                sortable: this,
                name: "sort",
                toEl: parentEl,
                originalEvent: evt
              });
            }
          }
        }
        if (Sortable.active) {
          if (newIndex == null || newIndex === -1) {
            newIndex = oldIndex;
            newDraggableIndex = oldDraggableIndex;
          }
          _dispatchEvent({
            sortable: this,
            name: "end",
            toEl: parentEl,
            originalEvent: evt
          });
          this.save();
        }
      }
    }
    this._nulling();
  },
  _nulling: function _nulling() {
    pluginEvent2("nulling", this);
    rootEl = dragEl = parentEl = ghostEl = nextEl = cloneEl = lastDownEl = cloneHidden = tapEvt = touchEvt = moved = newIndex = newDraggableIndex = oldIndex = oldDraggableIndex = lastTarget = lastDirection = putSortable = activeGroup = Sortable.dragged = Sortable.ghost = Sortable.clone = Sortable.active = null;
    var el = this.el;
    savedInputChecked.forEach(function(checkEl) {
      if (el.contains(checkEl)) {
        checkEl.checked = true;
      }
    });
    savedInputChecked.length = lastDx = lastDy = 0;
  },
  handleEvent: function handleEvent(evt) {
    switch (evt.type) {
      case "drop":
      case "dragend":
        this._onDrop(evt);
        break;
      case "dragenter":
      case "dragover":
        if (dragEl) {
          this._onDragOver(evt);
          _globalDragOver(evt);
        }
        break;
      case "selectstart":
        evt.preventDefault();
        break;
    }
  },
  toArray: function toArray() {
    var order = [], el, children = this.el.children, i = 0, n = children.length, options = this.options;
    for (;i < n; i++) {
      el = children[i];
      if (closest(el, options.draggable, this.el, false)) {
        order.push(el.getAttribute(options.dataIdAttr) || _generateId(el));
      }
    }
    return order;
  },
  sort: function sort(order, useAnimation) {
    var items = {}, rootEl2 = this.el;
    this.toArray().forEach(function(id, i) {
      var el = rootEl2.children[i];
      if (closest(el, this.options.draggable, rootEl2, false)) {
        items[id] = el;
      }
    }, this);
    useAnimation && this.captureAnimationState();
    order.forEach(function(id) {
      if (items[id]) {
        rootEl2.removeChild(items[id]);
        rootEl2.appendChild(items[id]);
      }
    });
    useAnimation && this.animateAll();
  },
  save: function save() {
    var store = this.options.store;
    store && store.set && store.set(this);
  },
  closest: function closest$1(el, selector) {
    return closest(el, selector || this.options.draggable, this.el, false);
  },
  option: function option(name, value) {
    var options = this.options;
    if (value === undefined) {
      return options[name];
    } else {
      var modifiedValue = PluginManager.modifyOption(this, name, value);
      if (typeof modifiedValue !== "undefined") {
        options[name] = modifiedValue;
      } else {
        options[name] = value;
      }
      if (name === "group") {
        _prepareGroup(options);
      }
    }
  },
  destroy: function destroy() {
    pluginEvent2("destroy", this);
    var el = this.el;
    el[expando] = null;
    off(el, "mousedown", this._onTapStart);
    off(el, "touchstart", this._onTapStart);
    off(el, "pointerdown", this._onTapStart);
    if (this.nativeDraggable) {
      off(el, "dragover", this);
      off(el, "dragenter", this);
    }
    Array.prototype.forEach.call(el.querySelectorAll("[draggable]"), function(el2) {
      el2.removeAttribute("draggable");
    });
    this._onDrop();
    this._disableDelayedDragEvents();
    sortables.splice(sortables.indexOf(this.el), 1);
    this.el = el = null;
  },
  _hideClone: function _hideClone() {
    if (!cloneHidden) {
      pluginEvent2("hideClone", this);
      if (Sortable.eventCanceled)
        return;
      css(cloneEl, "display", "none");
      if (this.options.removeCloneOnHide && cloneEl.parentNode) {
        cloneEl.parentNode.removeChild(cloneEl);
      }
      cloneHidden = true;
    }
  },
  _showClone: function _showClone(putSortable2) {
    if (putSortable2.lastPutMode !== "clone") {
      this._hideClone();
      return;
    }
    if (cloneHidden) {
      pluginEvent2("showClone", this);
      if (Sortable.eventCanceled)
        return;
      if (dragEl.parentNode == rootEl && !this.options.group.revertClone) {
        rootEl.insertBefore(cloneEl, dragEl);
      } else if (nextEl) {
        rootEl.insertBefore(cloneEl, nextEl);
      } else {
        rootEl.appendChild(cloneEl);
      }
      if (this.options.group.revertClone) {
        this.animate(dragEl, cloneEl);
      }
      css(cloneEl, "display", "");
      cloneHidden = false;
    }
  }
};
function _globalDragOver(evt) {
  if (evt.dataTransfer) {
    evt.dataTransfer.dropEffect = "move";
  }
  evt.cancelable && evt.preventDefault();
}
function _onMove(fromEl, toEl, dragEl2, dragRect, targetEl, targetRect, originalEvent, willInsertAfter) {
  var evt, sortable = fromEl[expando], onMoveFn = sortable.options.onMove, retVal;
  if (window.CustomEvent && !IE11OrLess && !Edge) {
    evt = new CustomEvent("move", {
      bubbles: true,
      cancelable: true
    });
  } else {
    evt = document.createEvent("Event");
    evt.initEvent("move", true, true);
  }
  evt.to = toEl;
  evt.from = fromEl;
  evt.dragged = dragEl2;
  evt.draggedRect = dragRect;
  evt.related = targetEl || toEl;
  evt.relatedRect = targetRect || getRect(toEl);
  evt.willInsertAfter = willInsertAfter;
  evt.originalEvent = originalEvent;
  fromEl.dispatchEvent(evt);
  if (onMoveFn) {
    retVal = onMoveFn.call(sortable, evt, originalEvent);
  }
  return retVal;
}
function _disableDraggable(el) {
  el.draggable = false;
}
function _unsilent() {
  _silent = false;
}
function _ghostIsFirst(evt, vertical, sortable) {
  var firstElRect = getRect(getChild(sortable.el, 0, sortable.options, true));
  var childContainingRect = getChildContainingRectFromElement(sortable.el, sortable.options, ghostEl);
  var spacer = 10;
  return vertical ? evt.clientX < childContainingRect.left - spacer || evt.clientY < firstElRect.top && evt.clientX < firstElRect.right : evt.clientY < childContainingRect.top - spacer || evt.clientY < firstElRect.bottom && evt.clientX < firstElRect.left;
}
function _ghostIsLast(evt, vertical, sortable) {
  var lastElRect = getRect(lastChild(sortable.el, sortable.options.draggable));
  var childContainingRect = getChildContainingRectFromElement(sortable.el, sortable.options, ghostEl);
  var spacer = 10;
  return vertical ? evt.clientX > childContainingRect.right + spacer || evt.clientY > lastElRect.bottom && evt.clientX > lastElRect.left : evt.clientY > childContainingRect.bottom + spacer || evt.clientX > lastElRect.right && evt.clientY > lastElRect.top;
}
function _getSwapDirection(evt, target, targetRect, vertical, swapThreshold, invertedSwapThreshold, invertSwap, isLastTarget) {
  var mouseOnAxis = vertical ? evt.clientY : evt.clientX, targetLength = vertical ? targetRect.height : targetRect.width, targetS1 = vertical ? targetRect.top : targetRect.left, targetS2 = vertical ? targetRect.bottom : targetRect.right, invert = false;
  if (!invertSwap) {
    if (isLastTarget && targetMoveDistance < targetLength * swapThreshold) {
      if (!pastFirstInvertThresh && (lastDirection === 1 ? mouseOnAxis > targetS1 + targetLength * invertedSwapThreshold / 2 : mouseOnAxis < targetS2 - targetLength * invertedSwapThreshold / 2)) {
        pastFirstInvertThresh = true;
      }
      if (!pastFirstInvertThresh) {
        if (lastDirection === 1 ? mouseOnAxis < targetS1 + targetMoveDistance : mouseOnAxis > targetS2 - targetMoveDistance) {
          return -lastDirection;
        }
      } else {
        invert = true;
      }
    } else {
      if (mouseOnAxis > targetS1 + targetLength * (1 - swapThreshold) / 2 && mouseOnAxis < targetS2 - targetLength * (1 - swapThreshold) / 2) {
        return _getInsertDirection(target);
      }
    }
  }
  invert = invert || invertSwap;
  if (invert) {
    if (mouseOnAxis < targetS1 + targetLength * invertedSwapThreshold / 2 || mouseOnAxis > targetS2 - targetLength * invertedSwapThreshold / 2) {
      return mouseOnAxis > targetS1 + targetLength / 2 ? 1 : -1;
    }
  }
  return 0;
}
function _getInsertDirection(target) {
  if (index(dragEl) < index(target)) {
    return 1;
  } else {
    return -1;
  }
}
function _generateId(el) {
  var str = el.tagName + el.className + el.src + el.href + el.textContent, i = str.length, sum = 0;
  while (i--) {
    sum += str.charCodeAt(i);
  }
  return sum.toString(36);
}
function _saveInputCheckedState(root) {
  savedInputChecked.length = 0;
  var inputs = root.getElementsByTagName("input");
  var idx = inputs.length;
  while (idx--) {
    var el = inputs[idx];
    el.checked && savedInputChecked.push(el);
  }
}
function _nextTick(fn) {
  return setTimeout(fn, 0);
}
function _cancelNextTick(id) {
  return clearTimeout(id);
}
if (documentExists) {
  on(document, "touchmove", function(evt) {
    if ((Sortable.active || awaitingDragStarted) && evt.cancelable) {
      evt.preventDefault();
    }
  });
}
Sortable.utils = {
  on,
  off,
  css,
  find,
  is: function is(el, selector) {
    return !!closest(el, selector, el, false);
  },
  extend,
  throttle,
  closest,
  toggleClass,
  clone,
  index,
  nextTick: _nextTick,
  cancelNextTick: _cancelNextTick,
  detectDirection: _detectDirection,
  getChild,
  expando
};
Sortable.get = function(element) {
  return element[expando];
};
Sortable.mount = function() {
  for (var _len = arguments.length, plugins2 = new Array(_len), _key = 0;_key < _len; _key++) {
    plugins2[_key] = arguments[_key];
  }
  if (plugins2[0].constructor === Array)
    plugins2 = plugins2[0];
  plugins2.forEach(function(plugin) {
    if (!plugin.prototype || !plugin.prototype.constructor) {
      throw "Sortable: Mounted plugin must be a constructor function, not ".concat({}.toString.call(plugin));
    }
    if (plugin.utils)
      Sortable.utils = _objectSpread2(_objectSpread2({}, Sortable.utils), plugin.utils);
    PluginManager.mount(plugin);
  });
};
Sortable.create = function(el, options) {
  return new Sortable(el, options);
};
Sortable.version = version;
var autoScrolls = [];
var scrollEl;
var scrollRootEl;
var scrolling = false;
var lastAutoScrollX;
var lastAutoScrollY;
var touchEvt$1;
var pointerElemChangedInterval;
function AutoScrollPlugin() {
  function AutoScroll() {
    this.defaults = {
      scroll: true,
      forceAutoScrollFallback: false,
      scrollSensitivity: 30,
      scrollSpeed: 10,
      bubbleScroll: true
    };
    for (var fn in this) {
      if (fn.charAt(0) === "_" && typeof this[fn] === "function") {
        this[fn] = this[fn].bind(this);
      }
    }
  }
  AutoScroll.prototype = {
    dragStarted: function dragStarted(_ref) {
      var originalEvent = _ref.originalEvent;
      if (this.sortable.nativeDraggable) {
        on(document, "dragover", this._handleAutoScroll);
      } else {
        if (this.options.supportPointer) {
          on(document, "pointermove", this._handleFallbackAutoScroll);
        } else if (originalEvent.touches) {
          on(document, "touchmove", this._handleFallbackAutoScroll);
        } else {
          on(document, "mousemove", this._handleFallbackAutoScroll);
        }
      }
    },
    dragOverCompleted: function dragOverCompleted(_ref2) {
      var originalEvent = _ref2.originalEvent;
      if (!this.options.dragOverBubble && !originalEvent.rootEl) {
        this._handleAutoScroll(originalEvent);
      }
    },
    drop: function drop() {
      if (this.sortable.nativeDraggable) {
        off(document, "dragover", this._handleAutoScroll);
      } else {
        off(document, "pointermove", this._handleFallbackAutoScroll);
        off(document, "touchmove", this._handleFallbackAutoScroll);
        off(document, "mousemove", this._handleFallbackAutoScroll);
      }
      clearPointerElemChangedInterval();
      clearAutoScrolls();
      cancelThrottle();
    },
    nulling: function nulling() {
      touchEvt$1 = scrollRootEl = scrollEl = scrolling = pointerElemChangedInterval = lastAutoScrollX = lastAutoScrollY = null;
      autoScrolls.length = 0;
    },
    _handleFallbackAutoScroll: function _handleFallbackAutoScroll(evt) {
      this._handleAutoScroll(evt, true);
    },
    _handleAutoScroll: function _handleAutoScroll(evt, fallback) {
      var _this = this;
      var x = (evt.touches ? evt.touches[0] : evt).clientX, y = (evt.touches ? evt.touches[0] : evt).clientY, elem = document.elementFromPoint(x, y);
      touchEvt$1 = evt;
      if (fallback || this.options.forceAutoScrollFallback || Edge || IE11OrLess || Safari) {
        autoScroll(evt, this.options, elem, fallback);
        var ogElemScroller = getParentAutoScrollElement(elem, true);
        if (scrolling && (!pointerElemChangedInterval || x !== lastAutoScrollX || y !== lastAutoScrollY)) {
          pointerElemChangedInterval && clearPointerElemChangedInterval();
          pointerElemChangedInterval = setInterval(function() {
            var newElem = getParentAutoScrollElement(document.elementFromPoint(x, y), true);
            if (newElem !== ogElemScroller) {
              ogElemScroller = newElem;
              clearAutoScrolls();
            }
            autoScroll(evt, _this.options, newElem, fallback);
          }, 10);
          lastAutoScrollX = x;
          lastAutoScrollY = y;
        }
      } else {
        if (!this.options.bubbleScroll || getParentAutoScrollElement(elem, true) === getWindowScrollingElement()) {
          clearAutoScrolls();
          return;
        }
        autoScroll(evt, this.options, getParentAutoScrollElement(elem, false), false);
      }
    }
  };
  return _extends(AutoScroll, {
    pluginName: "scroll",
    initializeByDefault: true
  });
}
function clearAutoScrolls() {
  autoScrolls.forEach(function(autoScroll) {
    clearInterval(autoScroll.pid);
  });
  autoScrolls = [];
}
function clearPointerElemChangedInterval() {
  clearInterval(pointerElemChangedInterval);
}
var autoScroll = throttle(function(evt, options, rootEl2, isFallback) {
  if (!options.scroll)
    return;
  var x = (evt.touches ? evt.touches[0] : evt).clientX, y = (evt.touches ? evt.touches[0] : evt).clientY, sens = options.scrollSensitivity, speed = options.scrollSpeed, winScroller = getWindowScrollingElement();
  var scrollThisInstance = false, scrollCustomFn;
  if (scrollRootEl !== rootEl2) {
    scrollRootEl = rootEl2;
    clearAutoScrolls();
    scrollEl = options.scroll;
    scrollCustomFn = options.scrollFn;
    if (scrollEl === true) {
      scrollEl = getParentAutoScrollElement(rootEl2, true);
    }
  }
  var layersOut = 0;
  var currentParent = scrollEl;
  do {
    var el = currentParent, rect = getRect(el), top = rect.top, bottom = rect.bottom, left = rect.left, right = rect.right, width = rect.width, height = rect.height, canScrollX = undefined, canScrollY = undefined, scrollWidth = el.scrollWidth, scrollHeight = el.scrollHeight, elCSS = css(el), scrollPosX = el.scrollLeft, scrollPosY = el.scrollTop;
    if (el === winScroller) {
      canScrollX = width < scrollWidth && (elCSS.overflowX === "auto" || elCSS.overflowX === "scroll" || elCSS.overflowX === "visible");
      canScrollY = height < scrollHeight && (elCSS.overflowY === "auto" || elCSS.overflowY === "scroll" || elCSS.overflowY === "visible");
    } else {
      canScrollX = width < scrollWidth && (elCSS.overflowX === "auto" || elCSS.overflowX === "scroll");
      canScrollY = height < scrollHeight && (elCSS.overflowY === "auto" || elCSS.overflowY === "scroll");
    }
    var vx = canScrollX && (Math.abs(right - x) <= sens && scrollPosX + width < scrollWidth) - (Math.abs(left - x) <= sens && !!scrollPosX);
    var vy = canScrollY && (Math.abs(bottom - y) <= sens && scrollPosY + height < scrollHeight) - (Math.abs(top - y) <= sens && !!scrollPosY);
    if (!autoScrolls[layersOut]) {
      for (var i = 0;i <= layersOut; i++) {
        if (!autoScrolls[i]) {
          autoScrolls[i] = {};
        }
      }
    }
    if (autoScrolls[layersOut].vx != vx || autoScrolls[layersOut].vy != vy || autoScrolls[layersOut].el !== el) {
      autoScrolls[layersOut].el = el;
      autoScrolls[layersOut].vx = vx;
      autoScrolls[layersOut].vy = vy;
      clearInterval(autoScrolls[layersOut].pid);
      if (vx != 0 || vy != 0) {
        scrollThisInstance = true;
        autoScrolls[layersOut].pid = setInterval(function() {
          if (isFallback && this.layer === 0) {
            Sortable.active._onTouchMove(touchEvt$1);
          }
          var scrollOffsetY = autoScrolls[this.layer].vy ? autoScrolls[this.layer].vy * speed : 0;
          var scrollOffsetX = autoScrolls[this.layer].vx ? autoScrolls[this.layer].vx * speed : 0;
          if (typeof scrollCustomFn === "function") {
            if (scrollCustomFn.call(Sortable.dragged.parentNode[expando], scrollOffsetX, scrollOffsetY, evt, touchEvt$1, autoScrolls[this.layer].el) !== "continue") {
              return;
            }
          }
          scrollBy(autoScrolls[this.layer].el, scrollOffsetX, scrollOffsetY);
        }.bind({
          layer: layersOut
        }), 24);
      }
    }
    layersOut++;
  } while (options.bubbleScroll && currentParent !== winScroller && (currentParent = getParentAutoScrollElement(currentParent, false)));
  scrolling = scrollThisInstance;
}, 30);
var drop = function drop2(_ref) {
  var { originalEvent, putSortable: putSortable2, dragEl: dragEl2, activeSortable, dispatchSortableEvent, hideGhostForTarget, unhideGhostForTarget } = _ref;
  if (!originalEvent)
    return;
  var toSortable = putSortable2 || activeSortable;
  hideGhostForTarget();
  var touch = originalEvent.changedTouches && originalEvent.changedTouches.length ? originalEvent.changedTouches[0] : originalEvent;
  var target = document.elementFromPoint(touch.clientX, touch.clientY);
  unhideGhostForTarget();
  if (toSortable && !toSortable.el.contains(target)) {
    dispatchSortableEvent("spill");
    this.onSpill({
      dragEl: dragEl2,
      putSortable: putSortable2
    });
  }
};
function Revert() {}
Revert.prototype = {
  startIndex: null,
  dragStart: function dragStart(_ref2) {
    var oldDraggableIndex2 = _ref2.oldDraggableIndex;
    this.startIndex = oldDraggableIndex2;
  },
  onSpill: function onSpill(_ref3) {
    var { dragEl: dragEl2, putSortable: putSortable2 } = _ref3;
    this.sortable.captureAnimationState();
    if (putSortable2) {
      putSortable2.captureAnimationState();
    }
    var nextSibling = getChild(this.sortable.el, this.startIndex, this.options);
    if (nextSibling) {
      this.sortable.el.insertBefore(dragEl2, nextSibling);
    } else {
      this.sortable.el.appendChild(dragEl2);
    }
    this.sortable.animateAll();
    if (putSortable2) {
      putSortable2.animateAll();
    }
  },
  drop
};
_extends(Revert, {
  pluginName: "revertOnSpill"
});
function Remove() {}
Remove.prototype = {
  onSpill: function onSpill2(_ref4) {
    var { dragEl: dragEl2, putSortable: putSortable2 } = _ref4;
    var parentSortable = putSortable2 || this.sortable;
    parentSortable.captureAnimationState();
    dragEl2.parentNode && dragEl2.parentNode.removeChild(dragEl2);
    parentSortable.animateAll();
  },
  drop
};
_extends(Remove, {
  pluginName: "removeOnSpill"
});
Sortable.mount(new AutoScrollPlugin);
Sortable.mount(Remove, Revert);
var sortable_esm_default = Sortable;

// src/urbanlens/dashboard/frontend/ts/shared/label-rel-picker.ts
var LabelRelPicker = {
  toggle(instanceId, relType, _triggerBtn) {
    const popup = document.getElementById(`${instanceId}-popup-${relType}`);
    if (!popup)
      return;
    const wasHidden = popup.hidden;
    document.querySelectorAll(".label-rel-popup").forEach((p) => {
      p.hidden = true;
    });
    if (!wasHidden)
      return;
    popup.hidden = false;
    const search = popup.querySelector(".label-rel-search");
    if (search) {
      search.value = "";
      search.focus();
    }
  },
  select(instanceId, relType, btn) {
    if (btn.classList.contains("label-rel-suggestion--hidden"))
      return;
    const group = document.getElementById(`${instanceId}-sel-${relType}`);
    if (!group)
      return;
    const id = btn.dataset.id;
    if (!id || group.querySelector(`.label-rel-chip[data-id="${id}"]`))
      return;
    const picker = document.querySelector(`[data-picker-id="${instanceId}"]`);
    const pill = document.createElement("span");
    pill.className = "tag-chip";
    const color = btn.style.getPropertyValue("--tag-color");
    if (color)
      pill.style.setProperty("--tag-color", color);
    pill.innerHTML = btn.innerHTML;
    pill.querySelector(".label-kind-chip")?.remove();
    if (picker?.dataset.mode === "replace") {
      const hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = `${relType}_ids`;
      hidden.value = id;
      pill.appendChild(hidden);
    }
    const chip = document.createElement("span");
    chip.className = "label-rel-chip";
    chip.dataset.id = id;
    chip.appendChild(pill);
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "tag-chip-remove";
    removeBtn.title = "Remove";
    removeBtn.innerHTML = "&times;";
    removeBtn.onclick = () => LabelRelPicker.remove(instanceId, chip);
    chip.appendChild(removeBtn);
    group.appendChild(chip);
    LabelRelPicker._hideSuggestion(instanceId, id);
    LabelRelPicker._updateEmptyHints(instanceId);
  },
  remove(instanceId, chipEl) {
    if (!chipEl)
      return;
    const id = chipEl.dataset.id;
    chipEl.remove();
    if (id)
      LabelRelPicker._showSuggestion(instanceId, id);
    LabelRelPicker._updateEmptyHints(instanceId);
  },
  _hideSuggestion(instanceId, id) {
    ["parent", "child"].forEach((relType) => {
      const container = document.getElementById(`${instanceId}-suggestions-${relType}`);
      container?.querySelector(`.label-rel-suggestion[data-id="${id}"]`)?.classList.add("label-rel-suggestion--hidden");
    });
  },
  _showSuggestion(instanceId, id) {
    ["parent", "child"].forEach((relType) => {
      const container = document.getElementById(`${instanceId}-suggestions-${relType}`);
      const btn = container?.querySelector(`.label-rel-suggestion[data-id="${id}"]`);
      if (btn) {
        btn.classList.remove("label-rel-suggestion--hidden");
        LabelRelPicker._applyFilters(instanceId, relType);
      }
    });
  },
  setTab(instanceId, relType, kind, btn) {
    const popup = document.getElementById(`${instanceId}-popup-${relType}`);
    if (!popup)
      return;
    popup.querySelectorAll(".label-rel-tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const container = document.getElementById(`${instanceId}-suggestions-${relType}`);
    if (container)
      container.dataset.activeTab = kind;
    LabelRelPicker._applyFilters(instanceId, relType);
  },
  filter(instanceId, relType, query) {
    const popup = document.getElementById(`${instanceId}-popup-${relType}`);
    if (popup)
      popup.dataset.searchQuery = query.toLowerCase().trim();
    LabelRelPicker._applyFilters(instanceId, relType);
  },
  _applyFilters(instanceId, relType) {
    const popup = document.getElementById(`${instanceId}-popup-${relType}`);
    const container = document.getElementById(`${instanceId}-suggestions-${relType}`);
    if (!popup || !container)
      return;
    const q = popup.dataset.searchQuery ?? "";
    const tab = container.dataset.activeTab ?? "";
    container.querySelectorAll(".label-rel-suggestion").forEach((btn) => {
      const matchesTab = !tab || btn.dataset.kind === tab;
      const matchesSearch = !q || (btn.dataset.name ?? "").indexOf(q) !== -1;
      btn.style.display = matchesTab && matchesSearch ? "" : "none";
    });
  },
  _updateEmptyHints(instanceId) {
    ["parent", "child"].forEach((relType) => {
      const group = document.getElementById(`${instanceId}-sel-${relType}`);
      const hint = group?.parentElement?.querySelector(".label-rel-empty-hint");
      if (hint)
        hint.hidden = (group?.children.length ?? 0) > 0;
    });
  },
  getSelectedIds(instanceId, relType) {
    const group = document.getElementById(`${instanceId}-sel-${relType}`);
    if (!group)
      return [];
    return Array.from(group.querySelectorAll(".label-rel-chip")).map((c) => Number.parseInt(c.dataset.id ?? "0", 10));
  },
  reset(instanceId) {
    ["parent", "child"].forEach((relType) => {
      const group = document.getElementById(`${instanceId}-sel-${relType}`);
      if (!group)
        return;
      Array.from(group.querySelectorAll(".label-rel-chip")).forEach((chip) => LabelRelPicker.remove(instanceId, chip));
    });
  },
  _makeSortable(instanceId) {
    const groupName = `${instanceId}-rel`;
    const parentList = document.getElementById(`${instanceId}-sel-parent`);
    const childList = document.getElementById(`${instanceId}-sel-child`);
    const trash = document.getElementById(`${instanceId}-trash`);
    if (!parentList || !childList)
      return;
    const showTrash = () => trash?.classList.add("is-active");
    const hideTrash = () => trash?.classList.remove("is-active");
    const onEnd = () => {
      hideTrash();
      LabelRelPicker._updateEmptyHints(instanceId);
    };
    const makeOnAdd = (relType) => (evt) => {
      const hidden = evt.item.querySelector('input[type="hidden"]');
      if (hidden)
        hidden.name = `${relType}_ids`;
    };
    new sortable_esm_default(parentList, {
      group: groupName,
      animation: 150,
      filter: ".tag-chip-remove",
      preventOnFilter: false,
      onStart: showTrash,
      onEnd,
      onAdd: makeOnAdd("parent")
    });
    new sortable_esm_default(childList, {
      group: groupName,
      animation: 150,
      filter: ".tag-chip-remove",
      preventOnFilter: false,
      onStart: showTrash,
      onEnd,
      onAdd: makeOnAdd("child")
    });
    if (trash) {
      new sortable_esm_default(trash, {
        group: { name: groupName, put: true, pull: false },
        animation: 150,
        onAdd: (evt) => LabelRelPicker.remove(instanceId, evt.item)
      });
    }
  },
  _initAll(root) {
    (root ?? document).querySelectorAll(".label-rel-picker").forEach((picker) => {
      if (picker.dataset.relInit === "1")
        return;
      picker.dataset.relInit = "1";
      if (picker.dataset.pickerId)
        LabelRelPicker._makeSortable(picker.dataset.pickerId);
    });
  }
};
function installGlobalLabelRelPicker() {
  window.LabelRelPicker = LabelRelPicker;
  LabelRelPicker._initAll();
  document.body.addEventListener("htmx:afterSettle", () => LabelRelPicker._initAll());
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".label-rel-add-dropdown")) {
      document.querySelectorAll(".label-rel-popup").forEach((p) => {
        p.hidden = true;
      });
    }
  });
}

// src/urbanlens/dashboard/frontend/ts/shared/organize-filter-engine.ts
var ORG_FILTER_NAMESPACES = ["tag", "cat", "status", "people"];
var NS_LABELS = { tag: "tags", cat: "categories", status: "statuses", people: "people" };
var NS_CONFIG = {
  tag: { rowsId: "tag-rows", cardSel: ".tag-card[data-tag-id]", idKey: "tagId", nameKey: "tagName", iconKey: "tagIcon", customIconKey: "tagCustomIcon", colorKey: "tagColor", parentsKey: "tagParents" },
  cat: { rowsId: "category-rows", cardSel: ".tag-card[data-category-id]", idKey: "categoryId", nameKey: "categoryName", iconKey: "categoryIcon", customIconKey: "categoryCustomIcon", colorKey: "categoryColor", parentsKey: "categoryParents" },
  status: { rowsId: "status-rows", cardSel: ".tag-card[data-status-id]", idKey: "statusId", nameKey: "statusName", iconKey: "statusIcon", customIconKey: "statusCustomIcon", colorKey: "statusColor", parentsKey: "statusParents" },
  people: { rowsId: "people-label-rows", cardSel: ".tag-card[data-people-id]", idKey: "peopleId", nameKey: "peopleName", iconKey: "peopleIcon", colorKey: "peopleColor", parentsKey: "peopleParents" }
};
function loadSharedFilter() {
  const params = new URLSearchParams(window.location.search);
  return {
    search: params.get("filter_search") ?? "",
    chips: new Set((params.get("filter_chips") ?? "").split(",").filter(Boolean)),
    color: (params.get("filter_color") ?? "").toLowerCase()
  };
}
var sharedFilter = loadSharedFilter();
var filterBarOpen = false;
function saveSharedFilter() {
  const params = new URLSearchParams(window.location.search);
  if (sharedFilter.search)
    params.set("filter_search", sharedFilter.search);
  else
    params.delete("filter_search");
  const chips = Array.from(sharedFilter.chips);
  if (chips.length > 0)
    params.set("filter_chips", chips.join(","));
  else
    params.delete("filter_chips");
  if (sharedFilter.color)
    params.set("filter_color", sharedFilter.color);
  else
    params.delete("filter_color");
  const newUrl = window.location.pathname + (params.toString() ? `?${params.toString()}` : "");
  window.history.replaceState({}, "", newUrl);
}
function captureFilterFromBar(ns) {
  const bar = document.getElementById(`${ns}-filter-bar`);
  const si = document.getElementById(`${ns}-filter-search`);
  sharedFilter.search = si ? si.value : "";
  sharedFilter.chips = new Set;
  sharedFilter.color = "";
  if (bar) {
    bar.querySelectorAll(".org-filter-chip.active").forEach((c) => {
      if (c.dataset.filter)
        sharedFilter.chips.add(c.dataset.filter);
    });
    const colorDot = bar.querySelector(".org-filter-color-dot.active");
    if (colorDot)
      sharedFilter.color = (colorDot.dataset.filter ?? "").replace("color:", "").toLowerCase();
  }
  saveSharedFilter();
}
function syncOrgFilterUI() {
  ORG_FILTER_NAMESPACES.forEach((ns) => {
    const bar = document.getElementById(`${ns}-filter-bar`);
    const si = document.getElementById(`${ns}-filter-search`);
    if (si)
      si.value = sharedFilter.search;
    bar?.querySelectorAll(".org-filter-chip, .org-filter-color-dot").forEach((el) => {
      const f = el.dataset.filter;
      if (!f)
        return;
      if (f.startsWith("color:"))
        el.classList.toggle("active", f.replace("color:", "").toLowerCase() === sharedFilter.color);
      else
        el.classList.toggle("active", sharedFilter.chips.has(f));
    });
  });
}
function updateFilterBtn() {
  const filterBtn = document.getElementById("org-header-filter-btn");
  if (!filterBtn)
    return;
  const hasActive = sharedFilter.search.trim() || sharedFilter.chips.size > 0 || sharedFilter.color;
  filterBtn.classList.toggle("has-filter", !!hasActive);
}
function syncFilterBtnActive() {
  document.getElementById("org-header-filter-btn")?.classList.toggle("btn--active", !!document.querySelector(".org-filter-bar.open"));
}
function getOrgVisibleCards(rows, cardSel) {
  if (!rows)
    return [];
  const inTreeView = rows.classList.contains("tag-view--tree");
  const cards = Array.from(rows.querySelectorAll(inTreeView ? `.tag-tree-root ${cardSel}` : cardSel));
  return cards.filter((c) => {
    if (inTreeView) {
      const treeItem = c.closest(".tag-tree-item");
      return !treeItem || treeItem.style.display !== "none";
    }
    return c.style.display !== "none";
  });
}
function applyFilterForNs(ns) {
  const cfg = NS_CONFIG[ns];
  const rows = document.getElementById(cfg.rowsId);
  if (!rows)
    return;
  const search = sharedFilter.search.toLowerCase().trim();
  const activeChips = sharedFilter.chips;
  const activeColor = sharedFilter.color;
  const inTreeView = rows.classList.contains("tag-view--tree");
  const allCards = Array.from(rows.querySelectorAll(inTreeView ? `.tag-tree-root ${cfg.cardSel}` : cfg.cardSel));
  const hasChildrenSet = new Set;
  if (activeChips.has("has-children")) {
    const childSourceCards = inTreeView ? Array.from(rows.querySelectorAll(cfg.cardSel)) : allCards;
    childSourceCards.forEach((c) => {
      (c.dataset[cfg.parentsKey] ?? "").split(",").map((s) => s.trim()).filter(Boolean).forEach((pid) => hasChildrenSet.add(pid));
    });
  }
  allCards.forEach((card) => {
    const idVal = card.dataset[cfg.idKey];
    const name = (card.dataset[cfg.nameKey] ?? "").toLowerCase();
    const icon = card.dataset[cfg.iconKey] ?? "";
    const customIcon = cfg.customIconKey ? card.dataset[cfg.customIconKey] ?? "" : "";
    const anyIcon = icon || customIcon;
    const color = (card.dataset[cfg.colorKey] ?? "").toLowerCase();
    const parents = card.dataset[cfg.parentsKey] ?? "";
    const hasParents = parents.split(",").some((p) => p.trim() !== "");
    let show = true;
    if (search && !name.includes(search))
      show = false;
    if (activeChips.has("has-icon") && !anyIcon)
      show = false;
    if (activeChips.has("no-icon") && anyIcon)
      show = false;
    if (activeChips.has("has-color") && !color)
      show = false;
    if (activeChips.has("no-color") && color)
      show = false;
    if (activeChips.has("has-children") && !hasChildrenSet.has(String(idVal)))
      show = false;
    if (activeChips.has("has-parents") && !hasParents)
      show = false;
    if (activeColor && color !== activeColor)
      show = false;
    if (inTreeView) {
      const treeItem = card.closest(".tag-tree-item");
      (treeItem ?? card).style.display = show ? "" : "none";
    } else {
      card.style.display = show ? "" : "none";
    }
  });
  document.dispatchEvent(new CustomEvent("org:filter-applied", { detail: { ns } }));
}
function applyOrgFilter(ns) {
  applyAllOrgFilters(ns);
}
function applyAllOrgFilters(triggerNs) {
  if (triggerNs) {
    const si = document.getElementById(`${triggerNs}-filter-search`);
    if (si) {
      sharedFilter.search = si.value;
      saveSharedFilter();
      syncOrgFilterUI();
    }
  }
  ORG_FILTER_NAMESPACES.forEach((ns) => applyFilterForNs(ns));
  updateFilterBtn();
}
function hasAnyOrgFilter() {
  return !!(sharedFilter.search || sharedFilter.chips.size > 0 || sharedFilter.color);
}
function countVisibleCards(ns) {
  const cfg = NS_CONFIG[ns];
  const rows = document.getElementById(cfg.rowsId);
  if (!rows)
    return 0;
  const inTreeView = rows.classList.contains("tag-view--tree");
  const scope = inTreeView ? `.tag-tree-root ${cfg.cardSel}` : cfg.cardSel;
  return Array.from(rows.querySelectorAll(scope)).filter((c) => c.style.display !== "none").length;
}
function updateCrossTabCounts() {
  if (!hasAnyOrgFilter()) {
    ORG_FILTER_NAMESPACES.forEach((ns) => {
      const countEl = document.getElementById(`org-tab-count-${ns}`);
      if (countEl)
        countEl.hidden = true;
      const footer = document.getElementById(`org-cross-tab-${ns}`);
      if (footer)
        footer.hidden = true;
    });
    return;
  }
  const counts = { tag: 0, cat: 0, status: 0, people: 0 };
  ORG_FILTER_NAMESPACES.forEach((ns) => {
    counts[ns] = countVisibleCards(ns);
  });
  const activeTabEl = document.querySelector(".organize-tab.active[data-filter-ns]");
  const activeNs = activeTabEl?.dataset.filterNs ?? null;
  ORG_FILTER_NAMESPACES.forEach((ns) => {
    const countEl = document.getElementById(`org-tab-count-${ns}`);
    if (!countEl)
      return;
    if (ns === activeNs) {
      countEl.hidden = true;
    } else {
      countEl.textContent = String(counts[ns]);
      countEl.hidden = false;
    }
  });
  ORG_FILTER_NAMESPACES.forEach((ns) => {
    const footer = document.getElementById(`org-cross-tab-${ns}`);
    if (!footer)
      return;
    if (ns !== activeNs) {
      footer.hidden = true;
      return;
    }
    const otherParts = ORG_FILTER_NAMESPACES.filter((otherNs) => otherNs !== ns && counts[otherNs] > 0).map((otherNs) => ({
      ns: otherNs,
      n: counts[otherNs],
      label: NS_LABELS[otherNs]
    }));
    if (otherParts.length === 0) {
      footer.hidden = true;
      return;
    }
    const selfCount = counts[ns];
    const prefix = selfCount === 0 ? `No ${NS_LABELS[ns]} match, but ` : "";
    const parts = otherParts.map((p) => {
      const tabKey = p.ns === "cat" ? "categories" : p.ns === "tag" ? "tags" : p.ns;
      const tabBtn = document.querySelector(`.organize-tab[data-tab="${tabKey}"]`);
      return tabBtn ? `<button class="org-cross-tab-link" type="button" data-org-tab="${tabKey}">${p.n} ${p.label}</button>` : `${p.n} ${p.label}`;
    });
    let partsHtml;
    if (parts.length === 1)
      partsHtml = parts[0];
    else if (parts.length === 2)
      partsHtml = `${parts[0]} and ${parts[1]}`;
    else
      partsHtml = `${parts.slice(0, -1).join(", ")}, and ${parts[parts.length - 1]}`;
    footer.innerHTML = `<i class="material-symbols-outlined">info</i><span>${prefix}${partsHtml} also match this search.</span>`;
    footer.hidden = false;
  });
}
function syncOrgFilterBarVisibility(activeNs) {
  ORG_FILTER_NAMESPACES.forEach((ns) => document.getElementById(`${ns}-filter-bar`)?.classList.remove("open"));
  if (activeNs && (filterBarOpen || hasAnyOrgFilter())) {
    const activeBar = document.getElementById(`${activeNs}-filter-bar`);
    if (activeBar) {
      activeBar.classList.add("open");
      filterBarOpen = true;
    }
  }
  syncFilterBtnActive();
}
function toggleOrgFilter(ns) {
  const bar = document.getElementById(`${ns}-filter-bar`);
  if (!bar)
    return;
  const willOpen = !bar.classList.contains("open");
  ORG_FILTER_NAMESPACES.forEach((otherNs) => document.getElementById(`${otherNs}-filter-bar`)?.classList.remove("open"));
  if (willOpen) {
    bar.classList.add("open");
    filterBarOpen = true;
  } else {
    filterBarOpen = false;
    clearOrgFilter(ns);
  }
  syncFilterBtnActive();
}
function toggleOrgChip(btn, ns) {
  btn.classList.toggle("active");
  const mutex = btn.dataset.mutex;
  if (mutex && btn.classList.contains("active")) {
    document.getElementById(`${ns}-filter-bar`)?.querySelectorAll(`[data-filter="${mutex}"]`).forEach((m) => m.classList.remove("active"));
  }
  if (btn.dataset.filter?.startsWith("color:") && btn.classList.contains("active")) {
    document.getElementById(`${ns}-filter-bar`)?.querySelectorAll(".org-filter-color-dot.active").forEach((d) => {
      if (d !== btn)
        d.classList.remove("active");
    });
  }
  captureFilterFromBar(ns);
  syncOrgFilterUI();
  applyAllOrgFilters();
}
function clearOrgFilter(_ns) {
  sharedFilter.search = "";
  sharedFilter.chips = new Set;
  sharedFilter.color = "";
  saveSharedFilter();
  syncOrgFilterUI();
  applyAllOrgFilters();
}
function installOrgFilterEngine() {
  let crossTabPendingId = null;
  document.addEventListener("org:filter-applied", () => {
    if (crossTabPendingId)
      clearTimeout(crossTabPendingId);
    crossTabPendingId = setTimeout(updateCrossTabCounts, 0);
  });
  document.addEventListener("org:tab-changed", () => {
    if (hasAnyOrgFilter())
      updateCrossTabCounts();
  });
  document.addEventListener("click", (e) => {
    const target = e.target;
    const crossTabLink = target.closest(".org-cross-tab-link[data-org-tab]");
    if (crossTabLink) {
      document.querySelector(`.organize-tab[data-tab="${crossTabLink.dataset.orgTab}"]`)?.click();
      return;
    }
    const chip = target.closest(".org-filter-chip, .org-filter-color-dot");
    if (chip && !chip.classList.contains("org-filter-clear") && !chip.classList.contains("org-filter-close")) {
      const bar = chip.closest(".org-filter-bar");
      const ns = bar?.dataset.filterNs;
      if (ns)
        toggleOrgChip(chip, ns);
      return;
    }
    const clearBtn = target.closest(".org-filter-clear");
    if (clearBtn) {
      const bar = clearBtn.closest(".org-filter-bar");
      const ns = bar?.dataset.filterNs;
      if (ns)
        clearOrgFilter(ns);
      return;
    }
    const closeBtn = target.closest(".org-filter-close");
    if (closeBtn) {
      const bar = closeBtn.closest(".org-filter-bar");
      const ns = bar?.dataset.filterNs;
      if (ns)
        toggleOrgFilter(ns);
    }
  });
  document.addEventListener("input", (e) => {
    const target = e.target;
    if (!target.classList.contains("org-filter-search"))
      return;
    const bar = target.closest(".org-filter-bar");
    const ns = bar?.dataset.filterNs;
    if (ns)
      applyOrgFilter(ns);
  });
}

// src/urbanlens/dashboard/frontend/ts/shared/organize-header.ts
var TAB_FILTER_NS = { categories: "cat", tags: "tag", status: "status", people: "people" };

class OrganizeHeader {
  tabs = new Map;
  activeTab;
  sharedView;
  actionsEl = null;
  headerActionsEl = null;
  filterBtn = null;
  createBtn = null;
  viewToggle = null;
  wired = false;
  constructor(initialTab) {
    this.activeTab = initialTab;
    this.sharedView = this.loadSharedView();
  }
  loadSharedView() {
    return localStorage.getItem("organize_view") ?? localStorage.getItem("tag_view") ?? localStorage.getItem("category_view") ?? localStorage.getItem("status_view") ?? localStorage.getItem("people_view") ?? "list";
  }
  register(tabKey, cfg) {
    this.tabs.set(tabKey, cfg);
  }
  getFilterNs() {
    return TAB_FILTER_NS[this.activeTab] ?? null;
  }
  getSharedView() {
    return this.sharedView;
  }
  setSharedView(view) {
    this.sharedView = view;
    localStorage.setItem("organize_view", view);
    this.syncViewButtons(view);
    this.tabs.forEach((cfg) => cfg.applyView());
    applyAllOrgFilters();
  }
  syncViewButtons(view) {
    document.querySelectorAll(".org-header-view-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === view);
    });
  }
  syncCreateButton(cfg) {
    if (!this.createBtn)
      return;
    this.createBtn.className = "btn btn--primary";
    this.createBtn.style.padding = ".4rem .55rem";
    this.createBtn.style.minWidth = "0";
    this.createBtn.title = cfg.createTitle;
    this.createBtn.innerHTML = cfg.createHtml;
  }
  setTab(tabKey) {
    this.activeTab = tabKey;
    this.headerActionsEl ??= document.querySelector(".organize-page-header-actions");
    this.actionsEl ??= document.getElementById("org-header-actions");
    const cfg = this.tabs.get(tabKey);
    if (this.headerActionsEl)
      this.headerActionsEl.hidden = tabKey === "priority";
    if (this.actionsEl)
      this.actionsEl.hidden = !cfg;
    if (!cfg)
      return;
    if (this.viewToggle)
      this.viewToggle.setAttribute("aria-label", cfg.viewAriaLabel);
    if (this.filterBtn)
      this.filterBtn.title = cfg.filterTitle;
    this.syncCreateButton(cfg);
    this.syncViewButtons(this.sharedView);
    cfg.updateSelAllBtn();
  }
  wireButtons() {
    if (this.wired)
      return;
    this.wired = true;
    this.actionsEl = document.getElementById("org-header-actions");
    this.filterBtn = document.getElementById("org-header-filter-btn");
    const selAllBtn = document.getElementById("org-header-sel-all");
    this.createBtn = document.getElementById("org-header-create-btn");
    this.viewToggle = document.getElementById("org-view-toggle");
    document.querySelectorAll(".org-header-view-btn").forEach((btn) => {
      btn.addEventListener("click", () => this.setSharedView(btn.dataset.view ?? "list"));
    });
    selAllBtn?.addEventListener("click", () => this.tabs.get(this.activeTab)?.onSelAll());
    this.filterBtn?.addEventListener("click", () => {
      const ns = this.getFilterNs();
      if (ns)
        toggleOrgFilter(ns);
    });
    this.createBtn?.addEventListener("click", () => this.tabs.get(this.activeTab)?.onCreate());
  }
  enforceMobileGalleryFallback() {
    if (!window.matchMedia("(max-width: 767px)").matches)
      return;
    if (this.sharedView === "gallery")
      this.setSharedView("list");
  }
  init() {
    this.wireButtons();
    this.enforceMobileGalleryFallback();
    this.setTab(this.activeTab);
    syncOrgFilterUI();
    applyAllOrgFilters();
    syncOrgFilterBarVisibility(this.getFilterNs());
    window.addEventListener("resize", () => this.enforceMobileGalleryFallback());
  }
}
var orgHeader;
function createOrganizeHeader(initialTab) {
  orgHeader = new OrganizeHeader(initialTab);
  return orgHeader;
}
function resetOrgBulk() {
  return { deselect: null, edit: null, merge: null, del: null };
}
function installOrgBulkToolbar() {
  window._orgBulk = resetOrgBulk();
  window._orgSelectionClearers = window._orgSelectionClearers ?? [];
  window._orgBulkEditByIds = window._orgBulkEditByIds ?? {};
  window._orgRegisterSelectionClearer = (fn) => {
    window._orgSelectionClearers.push(fn);
  };
  window._orgBulkClear = () => {
    document.getElementById("org-bulk-bar")?.classList.remove("visible");
    document.querySelector(".organize-page")?.classList.remove("org-page--has-selection");
    window._orgBulk = resetOrgBulk();
  };
  window._orgClearAllSelections = () => {
    window._orgSelectionClearers.forEach((fn) => fn());
    window._orgBulkClear();
  };
  window._orgBulkSync = (n, opts) => {
    const bar = document.getElementById("org-bulk-bar");
    const countEl = document.getElementById("org-bulk-count");
    const editBtn = document.getElementById("org-bulk-edit-btn");
    const mergeBtn = document.getElementById("org-bulk-merge-btn");
    const deleteBtn = document.getElementById("org-bulk-delete-btn");
    if (!bar)
      return;
    bar.classList.toggle("visible", n > 0);
    document.querySelector(".organize-page")?.classList.toggle("org-page--has-selection", n > 0);
    if (n > 0 && countEl)
      countEl.textContent = n === 1 ? "1 selected" : `${n} selected`;
    if (editBtn)
      editBtn.hidden = !opts.hasEdit;
    if (mergeBtn) {
      mergeBtn.hidden = !opts.hasMerge;
      mergeBtn.disabled = n < 2;
    }
    if (deleteBtn)
      deleteBtn.hidden = !opts.hasDel;
  };
  window._orgOpenSingleEdit = (dataAttr, id) => {
    const card = document.querySelector(`[${dataAttr}="${id}"]`);
    const btn = card?.querySelector('.tag-card-actions .btn--icon[title="Edit"]');
    if (!btn)
      return false;
    btn.click();
    return true;
  };
}
function installOrgTabSwitching() {
  const tabs = document.querySelectorAll(".organize-tab");
  const panels = document.querySelectorAll(".organize-panel");
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const target = tab.dataset.tab;
      if (!target)
        return;
      tabs.forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      panels.forEach((p) => {
        p.hidden = true;
      });
      const panel = document.getElementById(`panel-${target}`);
      if (panel)
        panel.hidden = false;
      orgHeader.setTab(target);
      localStorage.setItem("organize_tab", target);
      const url = new URL(window.location.href);
      url.searchParams.set("tab", target);
      window.history.replaceState({}, "", url.toString());
      if (target === "priority")
        window._initPrioritySortable?.();
      document.dispatchEvent(new CustomEvent("org:tab-changed", { detail: { tab: target } }));
      window._orgClearAllSelections?.();
      syncOrgFilterBarVisibility(tab.dataset.filterNs ?? null);
    });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape")
      return;
    const anyOpen = document.querySelector(".org-filter-bar.open");
    if (anyOpen) {
      document.querySelectorAll(".org-filter-bar.open").forEach((bar) => bar.classList.remove("open"));
      const activeTabEl = document.querySelector(".organize-tab.active[data-filter-ns]");
      const activeNs = activeTabEl?.dataset.filterNs ?? "tag";
      clearOrgFilter(activeNs);
      return;
    }
    window._orgBulk?.deselect?.();
  });
}
var ORG_SECTION_HERO = {
  labels: { icon: "tune", title: "Organize", subtitle: "Manage the tags, categories, statuses, and people labels used to organize your data." },
  lists: { icon: "bookmarks", title: "Lists", subtitle: "Group your pins into curated collections you can browse, share, and filter by." },
  filters: { icon: "filter_alt", title: "Filters", subtitle: "Save reusable filter criteria to quickly narrow down pins on the map and elsewhere." }
};
function updateOrgSectionHero(section) {
  const hero = ORG_SECTION_HERO[section];
  if (!hero)
    return;
  const titleEl = document.querySelector(".ul-page-hero__title");
  const iconEl = titleEl?.querySelector(".material-symbols-outlined");
  const subtitleEl = document.querySelector(".ul-page-hero__subtitle");
  if (iconEl)
    iconEl.textContent = hero.icon;
  if (titleEl) {
    const textNode = Array.from(titleEl.childNodes).find((n) => n.nodeType === Node.TEXT_NODE && !!n.textContent?.trim());
    if (textNode)
      textNode.textContent = ` ${hero.title} `;
  }
  if (subtitleEl)
    subtitleEl.textContent = hero.subtitle;
}
function installOrgSectionSwitching() {
  const tabs = document.querySelectorAll(".organize-section-tab");
  const panels = document.querySelectorAll(".organize-section-panel");
  if (!tabs.length)
    return;
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const section = tab.dataset.section;
      if (!section)
        return;
      tabs.forEach((t) => t.classList.toggle("is-active", t === tab));
      panels.forEach((p) => {
        p.hidden = p.id !== `panel-${section}`;
      });
      updateOrgSectionHero(section);
      const url = new URL(window.location.href);
      const tabParam = section === "labels" ? localStorage.getItem("organize_tab") ?? "tags" : section;
      url.searchParams.set("tab", tabParam);
      window.history.replaceState({}, "", url.toString());
    });
  });
}

// src/urbanlens/dashboard/frontend/ts/shared/tree-view.ts
var DEFAULT_TREE_ROOT_CLASS = "tag-tree-root";
function renderTreeView(rows, config) {
  const treeRootClass = config.treeRootClass ?? DEFAULT_TREE_ROOT_CLASS;
  rows.querySelector(`.${treeRootClass}`)?.remove();
  const cards = Array.from(rows.querySelectorAll(config.cardSelector));
  const cardMap = new Map;
  const parentMap = new Map;
  cards.forEach((card) => {
    const id = card.dataset[config.idKey];
    if (!id)
      return;
    cardMap.set(id, card);
    const parents = card.dataset[config.parentsKey] ?? "";
    parentMap.set(id, parents.split(",").map((s) => s.trim()).filter(Boolean));
    card.style.display = "none";
  });
  const childrenMap = new Map;
  parentMap.forEach((parents, id) => {
    parents.forEach((pid) => {
      const siblings = childrenMap.get(pid) ?? [];
      siblings.push(id);
      childrenMap.set(pid, siblings);
    });
  });
  const cardIds = new Set(cardMap.keys());
  const rootIds = Array.from(cardMap.keys()).filter((id) => {
    const parents = parentMap.get(id) ?? [];
    return parents.length === 0 || parents.every((pid) => !cardIds.has(pid));
  });
  rootIds.sort((a, b) => cards.indexOf(cardMap.get(a)) - cards.indexOf(cardMap.get(b)));
  const treeRoot = document.createElement("div");
  treeRoot.className = treeRootClass;
  const appearedInTree = new Set;
  function buildNode(id, depth, ancestorPath) {
    if (ancestorPath.has(id))
      return null;
    const card = cardMap.get(id);
    if (!card)
      return null;
    appearedInTree.add(id);
    const item = document.createElement("div");
    item.className = "tag-tree-item";
    item.dataset.depth = String(depth);
    item.style.setProperty("--tree-depth", String(depth));
    const clone2 = card.cloneNode(true);
    clone2.style.display = "";
    clone2.id = `tree-node-${id}-d${depth}-${Math.random().toString(36).slice(2, 6)}`;
    item.appendChild(clone2);
    const newPath = new Set(ancestorPath);
    newPath.add(id);
    const children = childrenMap.get(id) ?? [];
    if (children.length > 0) {
      const childrenContainer = document.createElement("div");
      childrenContainer.className = "tag-tree-children";
      children.forEach((cid) => {
        const childNode = buildNode(cid, depth + 1, newPath);
        if (childNode)
          childrenContainer.appendChild(childNode);
      });
      item.appendChild(childrenContainer);
    }
    return item;
  }
  rootIds.forEach((id) => {
    const node = buildNode(id, 0, new Set);
    if (node)
      treeRoot.appendChild(node);
  });
  cardMap.forEach((_card, id) => {
    if (!appearedInTree.has(id)) {
      const node = buildNode(id, 0, new Set);
      if (node)
        treeRoot.appendChild(node);
    }
  });
  rows.appendChild(treeRoot);
  htmxProcess(treeRoot);
}

// src/urbanlens/dashboard/frontend/ts/shared/organize-tab-manager.ts
var MATERIAL_ICON_NAME2 = /^[a-z_]+$/;
function escHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

class OrgTabManager {
  cfg;
  selected = new Set;
  lastClickedIdx = -1;
  mergeTargetId = null;
  convertTarget = null;
  constructor(cfg) {
    this.cfg = cfg;
  }
  init() {
    this.wireSelection();
    this.wireRowEditIntercept();
    this.wireBulkEdit();
    this.wireMerge();
    this.wireHtmxHooks();
    registerBulkStateUpdater(this.cfg.ns, () => this.updateBulkState());
    const globalWindow = window;
    if (this.cfg.convertTargets.length > 0) {
      globalWindow[`_set${this.cfg.nsCapitalized}BulkConvert`] = (target) => this.setConvertTarget(target);
    }
    globalWindow[`_update${this.cfg.nsCapitalized}BulkState`] = () => this.updateBulkState();
    window._orgBulkEditByIds[this.cfg.ns] = (ids) => {
      this.selected = new Set(ids.map(String));
      this.syncSelectionUi();
      this.openBulkEditDialog();
    };
    window._orgRegisterSelectionClearer(() => {
      this.selected.clear();
      this.lastClickedIdx = -1;
      this.syncSelectionUi();
    });
    orgHeader.register(this.tabKey(), {
      filterTitle: `Filter ${this.cfg.entityPluralLower}`,
      viewAriaLabel: `${this.cfg.entitySingular} view mode`,
      createTitle: `New ${this.cfg.entitySingular}`,
      createHtml: '<i class="material-icons" style="font-size:1.2rem;">add</i>',
      applyView: () => this.applyView(),
      onSelAll: () => this.onSelectAll(),
      updateSelAllBtn: () => this.updateSelAllBtn(),
      onCreate: () => this.onCreate()
    });
    this.applyView();
  }
  tabKey() {
    return { tag: "tags", cat: "categories", status: "status", people: "people" }[this.cfg.ns] ?? this.cfg.ns;
  }
  get rows() {
    return document.getElementById(this.cfg.rowsId);
  }
  applyView() {
    const rows = this.rows;
    if (!rows)
      return;
    const view = orgHeader.getSharedView();
    rows.classList.remove("tag-view--list", "tag-view--gallery", "tag-view--tree");
    rows.classList.add(`tag-view--${view}`);
    orgHeader.syncViewButtons(view);
    if (view === "tree") {
      renderTreeView(rows, { cardSelector: this.cfg.cardSelector, idKey: this.cfg.idKey, parentsKey: this.cfg.parentsKey });
    } else {
      rows.querySelector(".tag-tree-root")?.remove();
      rows.querySelectorAll(".tag-card").forEach((c) => {
        c.style.display = "";
      });
    }
  }
  visibleCards() {
    return getOrgVisibleCards(this.rows, this.cfg.cardSelector);
  }
  getVisibleIds() {
    return this.visibleCards().filter((c) => !this.cfg.isProtected?.(c.dataset[this.cfg.idKey] ?? "")).map((c) => c.dataset[this.cfg.idKey] ?? "").filter(Boolean);
  }
  syncSelectionUi() {
    this.rows?.querySelectorAll(this.cfg.cardSelector).forEach((card) => {
      const id = card.dataset[this.cfg.idKey] ?? "";
      card.classList.toggle("tag-card--selected", this.selected.has(id));
      const cb = card.querySelector(this.cfg.checkboxSelector);
      if (cb)
        cb.checked = this.selected.has(id);
    });
    this.updateSelectionBar();
  }
  updateSelAllBtn() {
    const btn = document.getElementById("org-header-sel-all");
    if (!btn)
      return;
    const visIds = this.getVisibleIds();
    const allSel = visIds.length > 0 && visIds.every((id) => this.selected.has(id));
    btn.classList.toggle("deselect-mode", allSel);
    btn.title = allSel ? "Deselect all" : "Select all";
    btn.innerHTML = allSel ? '<i class="material-symbols-outlined">remove_done</i>' : '<i class="material-symbols-outlined">checklist</i>';
  }
  onSelectAll() {
    const visIds = this.getVisibleIds();
    const allSel = visIds.length > 0 && visIds.every((id) => this.selected.has(id));
    visIds.forEach((id) => {
      if (allSel)
        this.selected.delete(id);
      else
        this.selected.add(id);
    });
    this.lastClickedIdx = -1;
    this.syncSelectionUi();
  }
  updateSelectionBar() {
    const n = this.selected.size;
    window._orgBulk.deselect = () => {
      this.selected.clear();
      this.syncSelectionUi();
    };
    window._orgBulk.edit = () => {
      if (!this.selected.size)
        return;
      if (this.selected.size === 1 && window._orgOpenSingleEdit(`data-${this.datasetAttr(this.cfg.idKey)}`, Array.from(this.selected)[0]))
        return;
      this.openBulkEditDialog();
    };
    window._orgBulk.merge = () => {
      if (this.selected.size < 2)
        return;
      this.mergeTargetId = Array.from(this.selected)[0];
      this.renderMergeDialog();
      document.getElementById(this.cfg.mergeDialog.dialogId).showModal();
    };
    window._orgBulk.del = () => this.bulkDelete();
    window._orgBulkSync(n, { hasEdit: true, hasMerge: true, hasDel: true });
    this.updateSelAllBtn();
  }
  wireSelection() {
    this.rows?.addEventListener("click", (e) => {
      const target = e.target;
      const card = target.closest(this.cfg.cardSelector);
      if (!card)
        return;
      const cb = target.closest(this.cfg.checkboxSelector);
      if (cb) {
        e.preventDefault();
      } else if (target.closest("a,button,input,select,textarea")) {
        return;
      }
      const cards = this.visibleCards();
      const idx = cards.indexOf(card);
      const id = card.dataset[this.cfg.idKey] ?? "";
      const isProtected = this.cfg.isProtected?.(id) ?? false;
      if (e.shiftKey && this.lastClickedIdx >= 0) {
        const lastCard = cards[this.lastClickedIdx];
        const lastIdx = lastCard ? cards.indexOf(lastCard) : -1;
        const lo = lastIdx >= 0 ? Math.min(idx, lastIdx) : idx;
        const hi = lastIdx >= 0 ? Math.max(idx, lastIdx) : idx;
        const targetState = !this.selected.has(id);
        for (let i = lo;i <= hi; i++) {
          const cid = cards[i]?.dataset[this.cfg.idKey];
          if (!cid)
            continue;
          if (this.cfg.isProtected?.(cid))
            continue;
          if (targetState)
            this.selected.add(cid);
          else
            this.selected.delete(cid);
        }
        if (isProtected) {
          if (targetState)
            this.selected.add(id);
          else
            this.selected.delete(id);
        }
      } else {
        if (this.selected.has(id))
          this.selected.delete(id);
        else
          this.selected.add(id);
        this.lastClickedIdx = idx;
      }
      this.syncSelectionUi();
    });
  }
  wireRowEditIntercept() {
    this.rows?.addEventListener("click", (e) => {
      if (this.selected.size <= 1)
        return;
      const btn = e.target.closest('.tag-card-actions .btn--icon[title="Edit"]');
      if (!btn)
        return;
      const card = btn.closest(this.cfg.cardSelector);
      const id = card?.dataset[this.cfg.idKey];
      if (!id || !this.selected.has(id))
        return;
      e.preventDefault();
      e.stopImmediatePropagation();
      this.openBulkEditDialog();
    }, true);
  }
  onRowsUpdated() {
    this.selected.clear();
    this.lastClickedIdx = -1;
    this.syncSelectionUi();
    this.applyView();
    applyOrgFilter(this.cfg.ns);
  }
  wireHtmxHooks() {
    this.rows?.addEventListener("htmx:afterSwap", () => this.onRowsUpdated());
    document.addEventListener("org:filter-applied", (e) => {
      if (e.detail.ns === this.cfg.ns)
        this.updateSelAllBtn();
    });
  }
  onCreate() {
    const f = document.getElementById(this.cfg.newForm?.dialogId ?? "");
    if (!f)
      return;
    if (this.cfg.newForm) {
      f.querySelector("form")?.reset();
      resetIconPicker(this.cfg.newForm.iconPickerId);
      resetColorPicker(this.cfg.newForm.colorPickerId, this.cfg.newForm.colorValueId);
      if (this.cfg.newForm.customPreviewId) {
        const preview = document.getElementById(this.cfg.newForm.customPreviewId);
        if (preview) {
          preview.src = "";
          preview.style.display = "none";
        }
      }
    } else {
      f.querySelector("form")?.reset();
    }
    if (!f.open)
      f.showModal();
  }
  async bulkDelete() {
    const n = this.selected.size;
    if (!n)
      return;
    const entity = n === 1 ? this.cfg.entitySingular.toLowerCase() : this.cfg.entityPluralLower;
    let message = `Delete ${n} ${entity}?`;
    if (this.cfg.deleteWarning)
      message += `
${this.cfg.deleteWarning}`;
    if (!await confirmAction({ title: `Delete ${this.cfg.entityPluralCap}`, message, confirmLabel: "Delete" }))
      return;
    const ids = Array.from(this.selected).map((id) => Number.parseInt(id, 10));
    try {
      const html = await this.postForHtml(this.cfg.endpoints.bulkDelete, { ids });
      this.replaceRows(html);
      this.onRowsUpdated();
      toast.success(n === 1 ? `1 ${this.cfg.entitySingular.toLowerCase()} deleted.` : `${n} ${this.cfg.entityPluralLower} deleted.`);
    } catch (err) {
      toast.error(`Delete failed: ${err.message}`);
    }
  }
  setConvertTarget(target) {
    const btns = document.querySelectorAll(`#${this.cfg.bulkEditDialog.dialogId} .kind-toggle-option`);
    if (this.convertTarget === target) {
      this.convertTarget = null;
      btns.forEach((b) => b.classList.remove("is-active"));
    } else {
      this.convertTarget = target;
      btns.forEach((b) => b.classList.remove("is-active"));
      document.getElementById(`${this.cfg.ns}-bulk-convert-to-${target}`)?.classList.add("is-active");
    }
    this.updateBulkState();
  }
  updateBulkState() {
    const converting = !!this.convertTarget;
    const hintId = this.cfg.bulkEditDialog.convertHintId;
    if (hintId) {
      const hint = document.getElementById(hintId);
      if (hint) {
        hint.hidden = !converting;
        if (converting) {
          const targetLabel = this.cfg.convertTargets.find((t) => t.kind === this.convertTarget)?.label ?? "";
          hint.textContent = `All pin memberships will be migrated. Selected parent links will be added after conversion. You will be redirected to the ${targetLabel.toLowerCase()} tab.`;
        }
      }
    }
    const btn = document.getElementById(this.cfg.bulkEditDialog.confirmId);
    if (btn && !btn.disabled) {
      const targetLabel = this.cfg.convertTargets.find((t) => t.kind === this.convertTarget)?.label ?? "";
      btn.innerHTML = converting ? `<i class="material-icons" style="font-size:1rem;vertical-align:middle">swap_horiz</i> Convert to ${targetLabel}` : '<i class="material-icons" style="font-size:1rem;vertical-align:middle">edit</i> Apply Changes';
    }
  }
  openBulkEditDialog() {
    const d = this.cfg.bulkEditDialog;
    const ids = Array.from(this.selected);
    const iconSet = new Set;
    const colorSet = new Set;
    const customIconSet = new Set;
    ids.forEach((id) => {
      const card = document.querySelector(`[data-${this.datasetAttr(this.cfg.idKey)}="${id}"]`);
      if (!card)
        return;
      iconSet.add(card.dataset[this.cfg.iconKey] ?? "");
      colorSet.add(card.dataset[this.cfg.colorKey] ?? "");
      if (this.cfg.customIconKey)
        customIconSet.add(card.dataset[this.cfg.customIconKey] ?? "");
    });
    const sharedIcon = iconSet.size === 1 ? Array.from(iconSet)[0] : null;
    const sharedColor = colorSet.size === 1 ? Array.from(colorSet)[0] : null;
    const sharedCustomIcon = this.cfg.customIconKey && customIconSet.size === 1 ? Array.from(customIconSet)[0] : null;
    const iconNochange = document.getElementById(d.iconNochangeId);
    const iconValue = document.getElementById(`icon-value-${d.iconPickerId}`);
    const iconCurrent = document.getElementById(`icon-current-${d.iconPickerId}`);
    const iconGrid = document.getElementById(`icon-grid-${d.iconPickerId}`);
    iconGrid?.querySelectorAll(".icon-picker-item").forEach((b) => b.classList.remove("selected"));
    if (sharedCustomIcon) {
      iconNochange.checked = true;
      if (iconValue)
        iconValue.value = "";
      if (iconCurrent)
        iconCurrent.innerHTML = `<img src="${sharedCustomIcon}" alt="" class="tag-icon-img"> <span class="icon-picker-none-label">Custom icon (kept unless you pick a new one)</span>`;
    } else if (sharedIcon !== null) {
      iconNochange.checked = false;
      if (iconValue)
        iconValue.value = sharedIcon;
      if (iconCurrent)
        iconCurrent.innerHTML = renderIconGlyphHtml(sharedIcon);
      if (sharedIcon && iconGrid)
        iconGrid.querySelector(`[data-icon="${sharedIcon}"]`)?.classList.add("selected");
      else
        iconGrid?.querySelector(".icon-picker-none")?.classList.add("selected");
    } else {
      iconNochange.checked = true;
      if (iconValue)
        iconValue.value = "";
      if (iconCurrent)
        iconCurrent.innerHTML = '<span class="icon-picker-none-label">No icon</span>';
    }
    const colorNochange = document.getElementById(d.colorNochangeId);
    const colorPickerEl = document.getElementById(d.colorPickerId);
    const colorValue = document.getElementById(d.colorValueId);
    colorPickerEl?.querySelectorAll(".color-swatch").forEach((b) => b.classList.remove("selected"));
    if (sharedColor !== null) {
      colorNochange.checked = false;
      if (colorValue)
        colorValue.value = sharedColor;
      if (sharedColor)
        colorPickerEl?.querySelector(`[data-color="${sharedColor}"]`)?.classList.add("selected");
    } else {
      colorNochange.checked = true;
      if (colorValue)
        colorValue.value = "";
    }
    if (d.orderValueId && d.orderNochangeId) {
      const orderNochange = document.getElementById(d.orderNochangeId);
      const orderValue = document.getElementById(d.orderValueId);
      orderNochange.checked = true;
      if (orderValue) {
        orderValue.value = "0";
        orderValue.dataset.bulkOriginal = "0";
      }
    }
    if (d.descValueId && d.descNochangeId) {
      const descNochange = document.getElementById(d.descNochangeId);
      const descValue = document.getElementById(d.descValueId);
      descNochange.checked = true;
      if (descValue) {
        descValue.value = "";
        descValue.dataset.bulkOriginal = "";
      }
    }
    LabelRelPicker.reset(`${this.cfg.ns}-bulk`);
    this.convertTarget = null;
    document.querySelectorAll(`#${d.dialogId} .kind-toggle-option`).forEach((b) => b.classList.remove("is-active"));
    const titleEl = document.getElementById(d.titleId);
    if (titleEl)
      titleEl.textContent = `Edit ${ids.length} ${ids.length === 1 ? this.cfg.entitySingular : this.cfg.entityPluralCap}`;
    const confirmBtn = document.getElementById(d.confirmId);
    confirmBtn.disabled = false;
    confirmBtn.innerHTML = '<i class="material-icons" style="font-size:1rem;vertical-align:middle">edit</i> Apply Changes';
    this.updateBulkState();
    document.getElementById(d.dialogId).showModal();
  }
  wireBulkEdit() {
    const d = this.cfg.bulkEditDialog;
    document.getElementById(d.iconNochangeId)?.addEventListener("change", (e) => {
      if (e.target.checked)
        resetIconPicker(d.iconPickerId);
      this.updateBulkState();
    });
    document.getElementById(d.colorNochangeId)?.addEventListener("change", (e) => {
      if (e.target.checked)
        resetColorPicker(d.colorPickerId, d.colorValueId);
      this.updateBulkState();
    });
    document.getElementById(`icon-grid-${d.iconPickerId}`)?.addEventListener("click", (e) => {
      if (e.target.closest(".icon-picker-item")) {
        document.getElementById(d.iconNochangeId).checked = false;
        this.updateBulkState();
      }
    });
    if (d.orderValueId && d.orderNochangeId) {
      document.getElementById(d.orderValueId)?.addEventListener("input", () => {
        document.getElementById(d.orderNochangeId).checked = false;
      });
      document.getElementById(d.orderNochangeId)?.addEventListener("change", (e) => {
        if (e.target.checked) {
          const el = document.getElementById(d.orderValueId);
          if (el)
            el.value = el.dataset.bulkOriginal ?? "0";
        }
      });
    }
    if (d.descValueId && d.descNochangeId) {
      document.getElementById(d.descValueId)?.addEventListener("input", () => {
        document.getElementById(d.descNochangeId).checked = false;
      });
      document.getElementById(d.descNochangeId)?.addEventListener("change", (e) => {
        if (e.target.checked) {
          const el = document.getElementById(d.descValueId);
          if (el)
            el.value = el.dataset.bulkOriginal ?? "";
        }
      });
    }
    document.getElementById(d.confirmId)?.addEventListener("click", async () => {
      const ids = Array.from(this.selected).map((id) => Number.parseInt(id, 10));
      const converting = !!this.convertTarget;
      const btn = document.getElementById(d.confirmId);
      const saved = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = `<span class="cat-merge-spinner"></span> ${converting ? "Converting…" : "Saving…"}`;
      const body = { ids };
      if (d.orderNochangeId && !document.getElementById(d.orderNochangeId).checked) {
        body.order = document.getElementById(d.orderValueId)?.value ?? "";
      }
      if (d.descNochangeId && !document.getElementById(d.descNochangeId).checked) {
        body.description = document.getElementById(d.descValueId)?.value ?? "";
      }
      if (!document.getElementById(d.iconNochangeId).checked) {
        body.icon = document.getElementById(`icon-value-${d.iconPickerId}`)?.value ?? "";
      }
      if (!document.getElementById(d.colorNochangeId).checked) {
        body.color = document.getElementById(d.colorValueId)?.value ?? "";
      }
      body.add_parent_ids = LabelRelPicker.getSelectedIds(`${this.cfg.ns}-bulk`, "parent");
      body.add_child_ids = LabelRelPicker.getSelectedIds(`${this.cfg.ns}-bulk`, "child");
      try {
        const target = converting ? this.cfg.convertTargets.find((t) => t.kind === this.convertTarget) : undefined;
        const url = converting ? target.endpoint : this.cfg.endpoints.bulkEdit;
        const html = await this.postForHtml(url, body);
        document.getElementById(d.dialogId).close();
        this.replaceRows(html);
        this.onRowsUpdated();
        if (converting) {
          toast.success(ids.length === 1 ? `1 ${this.cfg.entitySingular.toLowerCase()} converted.` : `${ids.length} ${this.cfg.entityPluralLower} converted.`);
          if (target?.rowsUrl && target.rowsTarget) {
            window.htmx?.ajax("GET", target.rowsUrl, { target: target.rowsTarget, swap: "innerHTML" });
          }
          if (target?.tabKey) {
            document.querySelector(`.organize-tab[data-tab="${target.tabKey}"]`)?.click();
          }
        } else {
          toast.success(`${this.cfg.entityPluralCap} updated.`);
        }
      } catch (err) {
        toast.error(`${converting ? "Convert" : "Edit"} failed: ${err.message}`);
        btn.disabled = false;
        btn.innerHTML = saved;
      }
    });
  }
  getCardData(id) {
    const card = document.querySelector(`[data-${this.datasetAttr(this.cfg.idKey)}="${id}"]`);
    if (!card)
      return { id, name: "?", color: "", icon: "", pinCount: "0" };
    const data = {
      id,
      name: card.dataset[this.cfg.nameKey] ?? "",
      color: card.dataset[this.cfg.colorKey] ?? "",
      icon: card.dataset[this.cfg.iconKey] ?? "",
      pinCount: card.dataset[this.cfg.pinCountKey] ?? "0"
    };
    if (this.cfg.customIconKey)
      data.customIcon = card.dataset[this.cfg.customIconKey] ?? "";
    if (this.cfg.locationCountKey)
      data.locationCount = card.dataset[this.cfg.locationCountKey] ?? "0";
    return data;
  }
  miniCardHtml(data, isTarget, hideSwap) {
    const colorStyle = data.color ? `background:${data.color}22;border-color:${data.color}44;` : "";
    const iconColorStyle = data.color ? `color:${data.color}` : "";
    let iconHtml;
    if (data.customIcon) {
      iconHtml = `<img src="${escHtml(data.customIcon)}" style="width:24px;height:24px;object-fit:cover;border-radius:4px;" alt="">`;
    } else if (data.icon) {
      iconHtml = MATERIAL_ICON_NAME2.test(data.icon) ? `<i class="material-icons" style="${iconColorStyle}">${escHtml(data.icon)}</i>` : `<span class="tag-icon-emoji">${escHtml(data.icon)}</span>`;
    } else {
      iconHtml = `<i class="material-icons tag-icon-empty">${this.cfg.emptyIcon}</i>`;
    }
    const swapBtn = isTarget || hideSwap ? "" : `<button type="button" class="cat-merge-swap-btn" data-swap-id="${data.id}" title="Make this the surviving ${this.cfg.entitySingular.toLowerCase()}"><i class="material-symbols-outlined">swap_vert</i></button>`;
    const meta = data.locationCount !== undefined ? `${data.pinCount} pins &middot; ${data.locationCount} locations` : `${data.pinCount} pins`;
    return `<div class="cat-merge-mini-card${isTarget ? " cat-merge-mini-card--target" : ""}" data-merge-id="${data.id}">` + `<div class="tag-card-icon cat-merge-mini-icon" style="${colorStyle}">${iconHtml}</div>` + `<div class="cat-merge-mini-info"><div class="cat-merge-mini-name">${escHtml(data.name)}</div>` + `<div class="cat-merge-mini-meta">${meta}</div></div>${swapBtn}</div>`;
  }
  setMergeColorPicker(color) {
    const picker = document.getElementById(`${this.cfg.ns}-merge-color-picker`);
    const input = document.getElementById(`${this.cfg.ns}-merge-edit-color`);
    if (!picker || !input)
      return;
    picker.querySelectorAll(".color-swatch").forEach((s) => s.classList.remove("selected"));
    input.value = color;
    if (color)
      picker.querySelector(`[data-color="${color}"]`)?.classList.add("selected");
    else
      picker.querySelector(".color-clear")?.classList.add("selected");
  }
  setMergeIconPicker(icon) {
    const pickerId = this.cfg.mergeDialog.editIconId ?? `${this.cfg.ns}-merge-edit`;
    const iconValue = document.getElementById(`icon-value-${pickerId}`);
    const iconCurrent = document.getElementById(`icon-current-${pickerId}`);
    const iconGrid = document.getElementById(`icon-grid-${pickerId}`);
    iconGrid?.querySelectorAll(".icon-picker-item").forEach((b) => b.classList.remove("selected"));
    if (iconValue)
      iconValue.value = icon;
    if (iconCurrent)
      iconCurrent.innerHTML = renderIconGlyphHtml(icon);
    if (icon && iconGrid)
      iconGrid.querySelector(`[data-icon="${icon}"]`)?.classList.add("selected");
    else
      iconGrid?.querySelector(".icon-picker-none")?.classList.add("selected");
  }
  renderMergeDialog() {
    const d = this.cfg.mergeDialog;
    const ids = Array.from(this.selected);
    const protectedId = this.cfg.isProtected ? ids.find((id) => this.cfg.isProtected(id)) : undefined;
    if (protectedId) {
      this.mergeTargetId = protectedId;
    } else if (!this.mergeTargetId || !this.selected.has(this.mergeTargetId)) {
      this.mergeTargetId = ids[0] ?? null;
    }
    const targetIsProtected = this.cfg.isProtected?.(this.mergeTargetId ?? "") ?? false;
    const sourceIds = ids.filter((id) => id !== this.mergeTargetId);
    const data = this.getCardData(this.mergeTargetId);
    const titleEl = document.getElementById(d.titleId);
    if (titleEl)
      titleEl.textContent = `Merge ${ids.length} ${this.cfg.entityPluralCap}`;
    const targetCard = document.getElementById(d.targetCardId);
    if (targetCard)
      targetCard.innerHTML = this.miniCardHtml(data, true, false);
    const sourcesList = document.getElementById(d.sourcesListId);
    if (sourcesList)
      sourcesList.innerHTML = sourceIds.map((id) => this.miniCardHtml(this.getCardData(id), false, targetIsProtected)).join("");
    if (this.cfg.supportsMergeEdit) {
      if (d.swapHintId) {
        const swapHint = document.getElementById(d.swapHintId);
        if (swapHint)
          swapHint.style.display = targetIsProtected ? "none" : "";
      }
      const nameEl = document.getElementById(d.editNameId ?? "");
      if (nameEl) {
        nameEl.value = data.name;
        nameEl.readOnly = targetIsProtected;
        nameEl.title = targetIsProtected ? "Protected status names cannot be changed" : "";
      }
      this.setMergeIconPicker(data.icon);
      this.setMergeColorPicker(data.color);
    }
    const confirmBtn = document.getElementById(d.confirmId);
    confirmBtn.innerHTML = `<i class="material-icons" style="font-size:1rem;vertical-align:middle">merge</i> Merge into ${escHtml(data.name)}`;
    confirmBtn.disabled = false;
  }
  wireMerge() {
    const d = this.cfg.mergeDialog;
    document.getElementById(d.sourcesListId)?.addEventListener("click", (e) => {
      const btn = e.target.closest(".cat-merge-swap-btn");
      if (!btn)
        return;
      this.mergeTargetId = btn.dataset.swapId ?? null;
      this.renderMergeDialog();
    });
    document.getElementById(d.confirmId)?.addEventListener("click", async () => {
      const ids = Array.from(this.selected);
      const sourceIds = ids.filter((id) => id !== this.mergeTargetId);
      const btn = document.getElementById(d.confirmId);
      const saved = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '<span class="cat-merge-spinner"></span> Merging…';
      const capturedId = this.mergeTargetId;
      const origData = this.getCardData(capturedId);
      let editName = "";
      let editIcon = "";
      let editColor = "";
      let hasEdits = false;
      if (this.cfg.supportsMergeEdit) {
        editName = (document.getElementById(d.editNameId ?? "")?.value ?? "").trim() || origData.name;
        const iconPickerId = d.editIconId ?? `${this.cfg.ns}-merge-edit`;
        editIcon = document.getElementById(`icon-value-${iconPickerId}`)?.value ?? "";
        editColor = document.getElementById(`${this.cfg.ns}-merge-edit-color`)?.value ?? "";
        hasEdits = editName !== origData.name || editIcon !== origData.icon || editColor !== origData.color;
      }
      try {
        const mergeHtml = await this.postForHtml(this.cfg.endpoints.multiMerge, {
          target_id: Number.parseInt(capturedId, 10),
          source_ids: sourceIds.map((id) => Number.parseInt(id, 10))
        });
        let html = mergeHtml;
        if (hasEdits && this.cfg.endpoints.mergeEditTemplate) {
          const fd = new FormData;
          fd.append("name", editName);
          fd.append("icon", editIcon);
          fd.append("color", editColor);
          const editUrl = this.cfg.endpoints.mergeEditTemplate.replace("99999", capturedId);
          const editResponse = await fetch(editUrl, { method: "POST", headers: { "X-CSRFToken": getCsrfToken() }, body: fd });
          if (!editResponse.ok)
            toast.warning("Merged, but could not save property changes.");
          else
            html = await editResponse.text();
        }
        document.getElementById(d.dialogId).close();
        this.replaceRows(html);
        this.mergeTargetId = null;
        this.onRowsUpdated();
        toast.success(`${this.cfg.entityPluralCap} merged successfully.`);
      } catch (err) {
        toast.error(`Merge failed: ${err.message}`);
        btn.disabled = false;
        btn.innerHTML = saved;
      }
    });
  }
  async postForHtml(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
      body: JSON.stringify(body)
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || response.statusText);
    }
    return response.text();
  }
  replaceRows(html) {
    const rows = this.rows;
    if (!rows)
      return;
    rows.innerHTML = html;
    window.htmx?.process(rows);
  }
  datasetAttr(camelKey) {
    return camelKey.replace(/([A-Z])/g, "-$1").toLowerCase();
  }
}

// src/urbanlens/dashboard/frontend/ts/shared/organize-priority.ts
function initOrganizePriority() {
  let prioritySortable = null;
  let priorityOrderEditing = null;
  let lastClickedIdx = -1;
  function priorityOrderBadge(item) {
    return item.querySelector(".priority-order-editor")?.querySelector(".priority-order-chip") ?? null;
  }
  function flashPriorityOrderSaved(item) {
    item.classList.remove("priority-item--order-saved");
    item.offsetWidth;
    item.classList.add("priority-item--order-saved");
    const badge = priorityOrderBadge(item);
    if (badge) {
      badge.classList.remove("priority-order-chip--flash");
      badge.offsetWidth;
      badge.classList.add("priority-order-chip--flash");
    }
    window.setTimeout(() => item.classList.remove("priority-item--order-saved"), 650);
  }
  function closeOrderEditor(restoreValue) {
    if (!priorityOrderEditing)
      return;
    const edit = priorityOrderEditing;
    priorityOrderEditing = null;
    edit.item.classList.remove("priority-item--editing-order");
    edit.editor.classList.remove("is-editing");
    edit.badge.textContent = String(restoreValue);
    edit.input.value = String(restoreValue);
    edit.input.setAttribute("aria-hidden", "true");
    edit.input.tabIndex = -1;
    edit.saveBtn.setAttribute("aria-hidden", "true");
    edit.saveBtn.tabIndex = -1;
  }
  async function savePriorityOrder(list, flashItem) {
    const items = Array.from(list.querySelectorAll(".priority-item[data-id]")).map((el, i) => {
      const badge = priorityOrderBadge(el);
      if (badge)
        badge.textContent = String(i + 1);
      return { id: Number.parseInt(el.dataset.id ?? "0", 10) };
    });
    try {
      const response = await fetch(list.dataset.saveUrl ?? "", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: JSON.stringify({ items })
      });
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || response.statusText);
      }
      if (flashItem)
        flashPriorityOrderSaved(flashItem);
      toast.success("Display order saved.");
    } catch (err) {
      toast.error(`Save failed: ${err.message}`);
    }
  }
  function commitOrderEditor() {
    if (!priorityOrderEditing)
      return;
    const edit = priorityOrderEditing;
    const list = edit.list;
    const total = list.querySelectorAll(".priority-item[data-id]").length;
    const newPos = Number.parseInt(edit.input.value, 10);
    if (Number.isNaN(newPos)) {
      closeOrderEditor(edit.originalValue);
      return;
    }
    const clampedPos = Math.max(1, Math.min(total, newPos));
    const items = Array.from(list.querySelectorAll(".priority-item[data-id]"));
    const currentIdx = items.indexOf(edit.item);
    const targetIdx = clampedPos - 1;
    closeOrderEditor(clampedPos);
    if (currentIdx === targetIdx)
      return;
    edit.item.remove();
    const remaining = Array.from(list.querySelectorAll(".priority-item[data-id]"));
    if (targetIdx >= remaining.length)
      list.appendChild(edit.item);
    else
      list.insertBefore(edit.item, remaining[targetIdx]);
    savePriorityOrder(list, edit.item);
  }
  function cancelOrderEditor() {
    if (priorityOrderEditing)
      closeOrderEditor(priorityOrderEditing.originalValue);
  }
  function beginPriorityOrderEdit(badge) {
    if (priorityOrderEditing) {
      if (priorityOrderEditing.badge === badge)
        return;
      cancelOrderEditor();
    }
    const editor = badge.closest(".priority-order-editor");
    const item = badge.closest(".priority-item");
    const list = document.getElementById("priority-list");
    if (!editor || !item || !list)
      return;
    const input = editor.querySelector(".priority-order-input");
    const saveBtn = editor.querySelector(".priority-order-save");
    if (!input || !saveBtn)
      return;
    const originalValue = Number.parseInt(badge.textContent ?? "0", 10);
    const total = list.querySelectorAll(".priority-item[data-id]").length;
    input.min = "1";
    input.max = String(total);
    input.value = String(originalValue);
    input.removeAttribute("aria-hidden");
    input.tabIndex = 0;
    saveBtn.removeAttribute("aria-hidden");
    saveBtn.tabIndex = 0;
    editor.classList.add("is-editing");
    item.classList.add("priority-item--editing-order");
    priorityOrderEditing = { item, editor, badge, input, saveBtn, originalValue, list, cancelled: false };
    window.requestAnimationFrame(() => {
      input.focus();
      input.select();
    });
    input.onkeydown = (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        commitOrderEditor();
      } else if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        if (priorityOrderEditing)
          priorityOrderEditing.cancelled = true;
        cancelOrderEditor();
      }
    };
    input.onblur = () => {
      window.setTimeout(() => {
        if (!priorityOrderEditing || priorityOrderEditing.input !== input)
          return;
        if (priorityOrderEditing.cancelled)
          return;
        const active = document.activeElement;
        if (active === saveBtn || saveBtn.contains(active))
          return;
        commitOrderEditor();
      }, 0);
    };
    saveBtn.onpointerdown = (e) => e.preventDefault();
    saveBtn.onclick = (e) => {
      e.preventDefault();
      commitOrderEditor();
    };
  }
  function priorityItems() {
    const list = document.getElementById("priority-list");
    return list ? Array.from(list.querySelectorAll(".priority-item[data-id]")) : [];
  }
  function setPrioritySelected(item, selected) {
    item.classList.toggle("priority-item--selected", selected);
  }
  function updatePrioritySelBar() {
    window._orgBulk.deselect = clearPrioritySelection;
    window._orgBulk.edit = () => {
      const items = document.querySelectorAll("#priority-list .priority-item--selected");
      if (!items.length)
        return;
      if (items.length === 1) {
        items[0].querySelector(".priority-edit-btn")?.click();
        return;
      }
      const kinds = new Set;
      const ids = [];
      items.forEach((item) => {
        if (item.dataset.kind)
          kinds.add(item.dataset.kind);
        if (item.dataset.id)
          ids.push(item.dataset.id);
      });
      if (kinds.size > 1) {
        toast.warning("Select only tags, only categories, or only statuses to bulk edit them together.");
        return;
      }
      const kind = Array.from(kinds)[0];
      const opener = kind ? window._orgBulkEditByIds[kind] : undefined;
      if (opener)
        opener(ids);
      else
        toast.error("Bulk edit is not available for this type.");
    };
    const n = document.querySelectorAll("#priority-list .priority-item--selected").length;
    window._orgBulkSync(n, { hasEdit: true, hasMerge: false, hasDel: false });
  }
  function clearPrioritySelection() {
    priorityItems().forEach((item) => setPrioritySelected(item, false));
    lastClickedIdx = -1;
    updatePrioritySelBar();
  }
  window._orgRegisterSelectionClearer(clearPrioritySelection);
  function initPrioritySortable() {
    const list = document.getElementById("priority-list");
    if (!list)
      return;
    prioritySortable?.destroy();
    prioritySortable = new sortable_esm_default(list, {
      animation: 150,
      handle: ".priority-drag-handle",
      ghostClass: "priority-item--ghost",
      fallbackTolerance: 3,
      onEnd: () => {
        savePriorityOrder(list, null);
      }
    });
  }
  document.getElementById("priority-list")?.addEventListener("click", (e) => {
    const target = e.target;
    const badge = target.closest(".priority-order-chip");
    if (badge) {
      e.preventDefault();
      beginPriorityOrderEdit(badge);
      return;
    }
    const jumpBtn = target.closest("[data-priority-jump]");
    if (jumpBtn) {
      const jumpItem = jumpBtn.closest(".priority-item");
      const list = document.getElementById("priority-list");
      if (!jumpItem || !list)
        return;
      if (jumpBtn.dataset.priorityJump === "top")
        list.insertBefore(jumpItem, list.firstElementChild);
      else
        list.appendChild(jumpItem);
      savePriorityOrder(list, jumpItem);
      return;
    }
    const item = target.closest(".priority-item");
    if (!item)
      return;
    if (target.closest(".priority-drag-handle,.priority-order-editor,a,button,input,select,textarea"))
      return;
    const items = priorityItems();
    const idx = items.indexOf(item);
    const isSelected = item.classList.contains("priority-item--selected");
    if (e.shiftKey && lastClickedIdx >= 0) {
      const lo = Math.min(idx, lastClickedIdx);
      const hi = Math.max(idx, lastClickedIdx);
      const targetState = !isSelected;
      for (let i = lo;i <= hi; i++) {
        const el = items[i];
        if (el)
          setPrioritySelected(el, targetState);
      }
    } else {
      setPrioritySelected(item, !isSelected);
      lastClickedIdx = idx;
    }
    updatePrioritySelBar();
  });
  window._initPrioritySortable = initPrioritySortable;
  document.getElementById("priority-list")?.addEventListener("htmx:afterSwap", () => {
    clearPrioritySelection();
    initPrioritySortable();
  });
  if (document.getElementById("panel-priority") && !document.getElementById("panel-priority").hidden) {
    initPrioritySortable();
  }
}

// src/urbanlens/dashboard/frontend/ts/shared/onboarding-tour.ts
function initOnboardingTour(config) {
  const sessionKey = `${config.prefix}_later`;
  function dismissed(id) {
    try {
      return localStorage.getItem(`${config.prefix}_${id}_dismissed`) === "1";
    } catch {
      return false;
    }
  }
  function dismiss(id) {
    try {
      localStorage.setItem(`${config.prefix}_${id}_dismissed`, "1");
    } catch {}
  }
  function later() {
    try {
      sessionStorage.setItem(sessionKey, "1");
    } catch {}
  }
  function laterSet() {
    try {
      return sessionStorage.getItem(sessionKey) === "1";
    } catch {
      return false;
    }
  }
  function clear() {
    document.querySelector(config.hostSelector)?.replaceChildren();
    document.querySelectorAll(".onboarding-focus").forEach((el) => el.classList.remove("onboarding-focus"));
  }
  function registerAutoDismiss(card) {
    if (dismissed(card.id) || !card.watchSelector)
      return;
    document.querySelectorAll(card.watchSelector).forEach((el) => {
      el.addEventListener(card.watchEvent ?? "click", () => dismiss(card.id), { once: true });
    });
  }
  function show(card) {
    const host = document.querySelector(config.hostSelector);
    if (!host)
      return;
    clear();
    document.querySelector(card.target)?.classList.add("onboarding-focus");
    const el = document.createElement("section");
    el.className = "page-onboarding-card";
    el.innerHTML = `<div class="page-onboarding-card__icon"><i class="material-icons">${card.icon}</i></div>` + `<div class="page-onboarding-card__body"><div class="page-onboarding-card__eyebrow">${card.eyebrow}</div>` + `<h2>${card.title}</h2><p>${card.body}</p><div class="page-onboarding-card__actions">` + `<button type="button" class="btn btn--primary js-onboarding-action">${card.button}</button>` + `<button type="button" class="btn btn--ghost js-onboarding-later">Later</button>` + `<button type="button" class="page-onboarding-dismiss js-onboarding-dismiss">Don't show again</button></div></div>` + `<button type="button" class="page-onboarding-x js-onboarding-later" aria-label="Close"><i class="material-symbols-outlined">close</i></button>`;
    host.appendChild(el);
    el.querySelector(".js-onboarding-action")?.addEventListener("click", () => {
      dismiss(card.id);
      clear();
      card.action();
    });
    el.querySelectorAll(".js-onboarding-later").forEach((btn) => btn.addEventListener("click", () => {
      later();
      clear();
    }));
    el.querySelector(".js-onboarding-dismiss")?.addEventListener("click", () => {
      dismiss(card.id);
      clear();
    });
  }
  function tryShow() {
    if (laterSet())
      return;
    const card = config.cards.find((c) => c.ready() && !dismissed(c.id));
    if (card)
      show(card);
  }
  config.cards.forEach(registerAutoDismiss);
  setTimeout(tryShow, config.initialDelayMs ?? 900);
  if (config.retryEvent) {
    document.addEventListener(config.retryEvent, () => {
      if (!document.querySelector(".page-onboarding-card"))
        setTimeout(tryShow, 250);
    });
  } else {
    document.body.addEventListener("htmx:afterSettle", () => {
      if (!document.querySelector(".page-onboarding-card"))
        setTimeout(tryShow, 250);
    });
  }
}

// src/urbanlens/dashboard/frontend/ts/entries/organize.ts
installGlobalOrganizeIconPicker();
installGlobalColorPicker();
installGlobalLabelRelPicker();
function showLabelCustomPreview(input, previewId) {
  const file = input.files?.[0];
  if (!file)
    return;
  const preview = document.getElementById(previewId);
  if (!preview)
    return;
  const reader = new FileReader;
  reader.onload = (e) => {
    preview.src = e.target?.result;
    preview.style.display = "block";
  };
  reader.readAsDataURL(file);
}
function showTagCustomPreview(input) {
  showLabelCustomPreview(input, "new-tag-custom-preview");
}
window.showLabelCustomPreview = showLabelCustomPreview;
window.showTagCustomPreview = showTagCustomPreview;
var KIND_ROWS_TARGET = { tag: "#tag-rows", category: "#category-rows", status: "#status-rows" };
var KIND_TAB_KEY = { tag: "tags", category: "categories", status: "status" };
function buildTabConfig(rows, overrides) {
  const page = document.querySelector(".organize-page");
  const rowsUrls = { tag: page?.dataset.rowsUrlTag, category: page?.dataset.rowsUrlCategory, status: page?.dataset.rowsUrlStatus };
  const convertTargets = [];
  if (rows.dataset.convertCategoryUrl)
    convertTargets.push({ kind: "category", label: "Categories", endpoint: rows.dataset.convertCategoryUrl, rowsUrl: rowsUrls.category, rowsTarget: KIND_ROWS_TARGET.category, tabKey: KIND_TAB_KEY.category });
  if (rows.dataset.convertTagUrl)
    convertTargets.push({ kind: "tag", label: "Tags", endpoint: rows.dataset.convertTagUrl, rowsUrl: rowsUrls.tag, rowsTarget: KIND_ROWS_TARGET.tag, tabKey: KIND_TAB_KEY.tag });
  if (rows.dataset.convertStatusUrl)
    convertTargets.push({ kind: "status", label: "Statuses", endpoint: rows.dataset.convertStatusUrl, rowsUrl: rowsUrls.status, rowsTarget: KIND_ROWS_TARGET.status, tabKey: KIND_TAB_KEY.status });
  const base = {
    ns: overrides.ns,
    nsCapitalized: overrides.nsCapitalized,
    rowsId: rows.id,
    cardSelector: `.tag-card[data-${overrides.ns}-id]`,
    idKey: `${overrides.ns}Id`,
    nameKey: `${overrides.ns}Name`,
    iconKey: `${overrides.ns}Icon`,
    colorKey: `${overrides.ns}Color`,
    parentsKey: `${overrides.ns}Parents`,
    pinCountKey: `${overrides.ns}PinCount`,
    checkboxSelector: `.${overrides.ns}-select-cb`,
    entitySingular: "",
    entityPluralLower: "",
    entityPluralCap: "",
    emptyIcon: "label",
    endpoints: {
      bulkDelete: rows.dataset.bulkDeleteUrl ?? "",
      bulkEdit: rows.dataset.bulkEditUrl ?? "",
      multiMerge: rows.dataset.mergeUrl ?? "",
      mergeEditTemplate: rows.dataset.mergeEditUrlTemplate
    },
    supportsMergeEdit: !!rows.dataset.mergeEditUrlTemplate,
    convertTargets,
    newForm: null,
    bulkEditDialog: {
      dialogId: `${overrides.ns}-bulk-edit-dialog`,
      titleId: `${overrides.ns}-bulk-edit-title`,
      confirmId: `${overrides.ns}-bulk-edit-confirm`,
      iconPickerId: `${overrides.ns}-bulk-edit`,
      iconNochangeId: `${overrides.ns}-bulk-icon-nochange`,
      colorPickerId: `${overrides.ns}-bulk-color-picker`,
      colorValueId: `${overrides.ns}-bulk-color-value`,
      colorNochangeId: `${overrides.ns}-bulk-color-nochange`,
      orderValueId: `${overrides.ns}-bulk-order-value`,
      orderNochangeId: `${overrides.ns}-bulk-order-nochange`,
      descValueId: `${overrides.ns}-bulk-description-value`,
      descNochangeId: `${overrides.ns}-bulk-description-nochange`,
      convertHintId: `${overrides.ns}-bulk-convert-hint`
    },
    mergeDialog: {
      dialogId: `${overrides.ns}-merge-dialog`,
      titleId: `${overrides.ns}-merge-dialog-title`,
      targetCardId: `${overrides.ns}-merge-target-card`,
      sourcesListId: `${overrides.ns}-merge-sources-list`,
      confirmId: `${overrides.ns}-merge-confirm-btn`,
      editNameId: `${overrides.ns}-merge-edit-name`,
      editIconId: `${overrides.ns}-merge-edit`,
      swapHintId: `${overrides.ns}-merge-swap-hint`
    }
  };
  return { ...base, ...overrides };
}
function initTabs() {
  const tagRows = document.getElementById("tag-rows");
  if (tagRows) {
    new OrgTabManager(buildTabConfig(tagRows, {
      ns: "tag",
      nsCapitalized: "Tag",
      entitySingular: "Tag",
      entityPluralLower: "tags",
      entityPluralCap: "Tags",
      emptyIcon: "label",
      customIconKey: "tagCustomIcon",
      deleteWarning: "Pins will NOT be deleted.",
      newForm: { dialogId: "new-tag-form", iconPickerId: "new-tag", colorPickerId: "new-tag-color-picker", colorValueId: "new-tag-color-value", customPreviewId: "new-tag-custom-preview" }
    })).init();
  }
  const catRows = document.getElementById("category-rows");
  if (catRows) {
    new OrgTabManager(buildTabConfig(catRows, {
      ns: "cat",
      nsCapitalized: "Cat",
      cardSelector: ".tag-card[data-category-id]",
      idKey: "categoryId",
      nameKey: "categoryName",
      iconKey: "categoryIcon",
      colorKey: "categoryColor",
      parentsKey: "categoryParents",
      pinCountKey: "categoryPinCount",
      locationCountKey: "categoryLocationCount",
      entitySingular: "Category",
      entityPluralLower: "categories",
      entityPluralCap: "Categories",
      emptyIcon: "category",
      deleteWarning: "Pins and locations will NOT be deleted.",
      newForm: { dialogId: "new-category-form", iconPickerId: "new-cat", colorPickerId: "new-cat-color-picker", colorValueId: "new-cat-color-value", customPreviewId: "new-cat-custom-preview" }
    })).init();
  }
  const statusRows = document.getElementById("status-rows");
  if (statusRows) {
    new OrgTabManager(buildTabConfig(statusRows, {
      ns: "status",
      nsCapitalized: "Status",
      entitySingular: "Status",
      entityPluralLower: "statuses",
      entityPluralCap: "Statuses",
      emptyIcon: "flag",
      isProtected: (id) => {
        const card = document.querySelector(`[data-status-id="${id}"]`);
        return card?.dataset.statusProtected === "true" || card?.dataset.statusProtected === "1";
      },
      newForm: { dialogId: "new-status-form", iconPickerId: "new-status", colorPickerId: "new-status-color-picker", colorValueId: "new-status-color-value", customPreviewId: "new-status-custom-preview" }
    })).init();
  }
  const peopleRows = document.getElementById("people-label-rows");
  if (peopleRows) {
    new OrgTabManager(buildTabConfig(peopleRows, {
      ns: "people",
      nsCapitalized: "People",
      cardSelector: ".tag-card[data-people-id]",
      idKey: "peopleId",
      nameKey: "peopleName",
      iconKey: "peopleIcon",
      colorKey: "peopleColor",
      parentsKey: "peopleParents",
      pinCountKey: "peoplePinCount",
      checkboxSelector: ".people-sel-cb",
      entitySingular: "Label",
      entityPluralLower: "labels",
      entityPluralCap: "Labels",
      emptyIcon: "person",
      bulkEditDialog: {
        dialogId: "people-bulk-edit-dialog",
        titleId: "people-bulk-edit-title",
        confirmId: "people-bulk-edit-confirm",
        iconPickerId: "people-bulk-edit",
        iconNochangeId: "people-bulk-icon-nochange",
        colorPickerId: "people-bulk-color-picker",
        colorValueId: "people-bulk-color-value",
        colorNochangeId: "people-bulk-color-nochange",
        descValueId: "people-bulk-description-value",
        descNochangeId: "people-bulk-description-nochange"
      },
      newForm: { dialogId: "new-people-form", iconPickerId: "new-people", colorPickerId: "new-people-color-picker", colorValueId: "new-people-color-value" }
    })).init();
  }
}
function initOnboarding() {
  const host = document.getElementById("organize-onboarding");
  if (!host)
    return;
  if (host.dataset.standaloneMode)
    return;
  if (!host.dataset.showOnboardingTips)
    return;
  initOnboardingTour({
    prefix: "ul_onboarding_v1_organize",
    hostSelector: "#organize-onboarding",
    retryEvent: "org:tab-changed",
    cards: [
      {
        id: "priority-order",
        icon: "low_priority",
        target: "#priority-explainer",
        eyebrow: "Map display",
        title: "Display order decides which label wins on the map",
        body: "If a pin has multiple tags, categories, or statuses, the highest item in this list provides the icon/color that appears on the map.",
        button: "Open display order",
        watchSelector: '[data-tab="priority"]',
        action: () => {
          document.querySelector('[data-tab="priority"]')?.click();
          document.getElementById("priority-explainer")?.scrollIntoView({ behavior: "smooth", block: "center" });
        },
        ready: () => !!document.getElementById("priority-explainer")
      },
      {
        id: "drag-priority",
        icon: "drag_indicator",
        target: "#priority-list .priority-drag-handle, #priority-list",
        eyebrow: "Reorder visually",
        title: "Drag important labels upward",
        body: "Put more specific labels near the top so the map shows the most meaningful icon when a pin has multiple tags.",
        button: "Go to display order",
        watchSelector: ".priority-drag-handle",
        watchEvent: "pointerdown",
        action: () => {
          document.querySelector('[data-tab="priority"]')?.click();
          document.getElementById("priority-list")?.scrollIntoView({ behavior: "smooth", block: "center" });
        },
        ready: () => !!document.getElementById("priority-list")
      },
      {
        id: "bulk-actions",
        icon: "checklist",
        target: "#org-header-sel-all",
        eyebrow: "Cleanup tools",
        title: "Select multiple labels to merge, edit, or delete in batches",
        body: "Bulk selection is useful when consolidating duplicate tags or applying the same icon/color to a group.",
        button: "Try bulk select",
        watchSelector: "#org-header-sel-all",
        action: () => {
          const btn = document.getElementById("org-header-sel-all");
          btn?.click();
          btn?.focus();
        },
        ready: () => !!document.getElementById("org-header-sel-all")
      }
    ]
  });
}
function initKindChangedListener() {
  const page = document.querySelector(".organize-page");
  const rowUrls = {
    tag: page?.dataset.rowsUrlTag,
    category: page?.dataset.rowsUrlCategory,
    status: page?.dataset.rowsUrlStatus
  };
  document.body.addEventListener("htmx:afterRequest", (e) => {
    const detail = e.detail;
    if (!detail.xhr || !detail.successful)
      return;
    const kindChanged = detail.xhr.getResponseHeader("X-Kind-Changed");
    if (!kindChanged)
      return;
    const url = rowUrls[kindChanged];
    const target = KIND_ROWS_TARGET[kindChanged];
    if (url && target)
      window.htmx?.ajax("GET", url, { target, swap: "innerHTML" });
    const tabKey = KIND_TAB_KEY[kindChanged];
    if (tabKey)
      document.querySelector(`.organize-tab[data-tab="${tabKey}"]`)?.click();
  });
}
function initPinCacheInvalidation() {
  document.body.addEventListener("htmx:afterRequest", (e) => {
    const detail = e.detail;
    if (!detail.xhr || !detail.successful)
      return;
    if (detail.requestConfig?.verb?.toLowerCase() === "get")
      return;
    try {
      localStorage.setItem("ul_pins_dirty", "1");
    } catch {}
    document.body.dispatchEvent(new Event("refreshPriority"));
  });
}
function initConsolidatedDialogOpener() {
  document.body.addEventListener("htmx:afterSwap", (e) => {
    const detail = e.detail;
    const id = detail.target?.id;
    if (!id)
      return;
    if (id === "label-edit-dialog-body") {
      const body = detail.target;
      const titleEl = document.getElementById("label-edit-dialog-title");
      if (titleEl) {
        if (body.querySelector(".organize-label-merge-form")) {
          const mergeName = body.querySelector(".tag-merge-source-name");
          titleEl.textContent = mergeName ? `Merge ${mergeName.textContent?.trim()}` : "Merge";
        } else if (body.querySelector(".organize-label-customize-form")) {
          titleEl.textContent = "Customize Display";
        } else if (body.querySelector(".tag-global-edit-form")) {
          titleEl.textContent = "Edit Global Tag";
        } else {
          const kindInput = body.querySelector('input[name="kind"]:checked');
          const titles = { tag: "Tag", category: "Category", status: "Status" };
          titleEl.textContent = `Edit ${titles[kindInput?.value ?? ""] ?? "Label"}`;
        }
      }
      const dialog = document.getElementById("label-edit-dialog");
      if (dialog && !dialog.open)
        dialog.showModal();
    } else if (id === "people-label-edit-dialog-body") {
      const dialog = document.getElementById("people-label-edit-dialog");
      if (dialog && !dialog.open)
        dialog.showModal();
    }
  });
}
function init() {
  const page = document.querySelector(".organize-page");
  if (!page)
    return;
  installOrgFilterEngine();
  installOrgBulkToolbar();
  createOrganizeHeader(page.dataset.activeTab ?? "tags");
  installOrgTabSwitching();
  installOrgSectionSwitching();
  initConsolidatedDialogOpener();
  initKindChangedListener();
  initPinCacheInvalidation();
  initOnboarding();
  initTabs();
  initOrganizePriority();
  orgHeader.init();
}
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
