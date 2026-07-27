/**
 * extractor.js — Quanta Full-Document DOM Harvester
 *
 * PHILOSOPHY:
 *   We don't scroll to find elements. We read the entire document at once
 *   and record the absolute position of every interactive element so the
 *   execution engine can scroll to any of them precisely when needed.
 *
 * WHAT THIS DOES:
 *   1. Walks the FULL DOM (not just the visible viewport).
 *   2. Pierces Shadow DOM roots (Web Components, custom elements).
 *   3. Traverses accessible iFrame documents (same-origin only).
 *   4. Deduplicates elements by a structural fingerprint.
 *   5. Collapses repeating sibling groups (nav menus, lists) to avoid token bloat.
 *   6. Returns a clean, deduplicated array of element descriptors.
 *
 * OUTPUT SCHEMA per element:
 *   {
 *     qId:           "q-1",           // Injected Quanta tracking ID
 *     tag:           "button",        // Lowercase HTML tag
 *     type:          "submit",        // input[type], null otherwise
 *     role:          "button",        // ARIA role
 *     text:          "Sign In",       // Visible innerText (truncated to 120 chars)
 *     ariaLabel:     "Sign in",       // aria-label attribute
 *     ariaLabelledBy:"heading-1",     // aria-labelledby ref value
 *     placeholder:   "Email address", // Input placeholder
 *     name:          "email",         // name attribute (stable for selectors)
 *     id:            "login-btn",     // Element id (stable selector anchor)
 *     dataTestId:    "submit-btn",    // data-testid (developer-intended)
 *     href:          "/login",        // Anchor href
 *     value:         "Submit",        // Current element value
 *     title:         "Login now",     // title attribute
 *     classes:       ["btn","btn-primary"], // CSS class list
 *     isVisible:     true,            // Not hidden (computed style check)
 *     inViewport:    false,           // Is currently on screen
 *     scrollY:       1450,            // Absolute Y offset from document top
 *     scrollX:       0,               // Absolute X offset from document left
 *     width:         120,             // Element width in px
 *     height:        40,              // Element height in px
 *     inShadowDom:   false,           // Part of a Shadow DOM subtree
 *     inIframe:      false,           // Part of a child iFrame
 *     iframeIndex:   null,            // Which frame it belongs to
 *     fingerprint:   "button|submit|Sign In", // For math-based matching
 *   }
 */

() => {
  // ─── CONFIG ───────────────────────────────────────────────────────────────
  const MAX_ELEMENTS = 600; // Hard cap — prevents memory issues on huge pages
  const MAX_TEXT_LENGTH = 120; // Truncate long innerText
  const MAX_SIBLINGS = 5; // Max same-type siblings in a group before collapsing

  // ─── STATE ────────────────────────────────────────────────────────────────
  let qIdCounter = 0;
  const elements = [];
  const seenFingerprints = new Set(); // For deduplication

  // ─── HELPERS ──────────────────────────────────────────────────────────────

  /**
   * Determines if an element is semantically interactive.
   * Handles native HTML, ARIA roles, framework-style handlers.
   */
  function isInteractive(el) {
    const tag = el.tagName.toLowerCase();

    // AXTree Pruning: Strip bare structural wrappers (div/span) to prevent div-soup
    const text = el.innerText ? el.innerText.trim() : "";
    const ariaLabel = el.getAttribute("aria-label") || "";
    const title = el.getAttribute("title") || "";
    const hasSemantics =
      text.length > 0 || ariaLabel.length > 0 || title.length > 0;

    // ARIA role overrides
    const role = (el.getAttribute("role") || "").toLowerCase();

    // If it's a generic layout tag (div, span) with zero structural meaning and no role, ignore it to prevent DOM hallucinations.
    if (["div", "span"].includes(tag) && !hasSemantics && !role) {
      return false;
    }

    // Native interactive HTML tags
    if (
      [
        "button",
        "a",
        "input",
        "textarea",
        "select",
        "details",
        "summary",
        "span",
        "p",
        "h1",
        "h2",
        "h3",
        "article",
        "section", // Content-heavy (for SCRAPE)
      ].includes(tag)
    )
      return true;

    if (
      [
        "button",
        "link",
        "checkbox",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "tab",
        "switch",
        "combobox",
        "searchbox",
        "spinbutton",
        "slider",
      ].includes(role)
    )
      return true;

    // Framework event handlers (React onClick, Vue v-on:click, Angular (click))
    if (
      el.hasAttribute("onclick") ||
      el.hasAttribute("ng-click") ||
      el.hasAttribute("@click") ||
      el.hasAttribute("v-on:click") ||
      el.hasAttribute("data-action") ||
      el.hasAttribute("data-click")
    )
      return true;

    // Elements with explicit tabIndex are meant to be keyboard-reachable
    const tabIndex = el.getAttribute("tabindex");
    if (tabIndex !== null && tabIndex !== "-1") return true;

    // Content-editable divs (rich text editors like Notion, Quill, Slate)
    if (el.getAttribute("contenteditable") === "true") return true;

    return false;
  }

  /**
   * Checks if the element is truly visible (computed style — not bounded to viewport).
   * This catches display:none, visibility:hidden, opacity:0, and zero-size elements.
   */
  function isVisible(el, style) {
    if (!style) style = window.getComputedStyle(el);
    if (style.display === "none") return false;
    if (style.visibility === "hidden") return false;
    if (parseFloat(style.opacity) === 0) return false;
    // aria-hidden="true" means the element is intentionally hidden from AT and users
    if (el.getAttribute("aria-hidden") === "true") return false;

    const rect = el.getBoundingClientRect();
    // Allow input/button/select even if they have 0 dimensions (some frameworks do this)
    const tag = el.tagName.toLowerCase();
    if (
      rect.width === 0 &&
      rect.height === 0 &&
      !["input", "button", "select"].includes(tag)
    )
      return false;

    return true;
  }

  /**
   * Returns whether the element's bounding box intersects the current viewport.
   */
  function isInViewport(rect) {
    return (
      rect.top < window.innerHeight &&
      rect.bottom > 0 &&
      rect.left < window.innerWidth &&
      rect.right > 0
    );
  }

  /**
   * Returns the absolute Y position from the top of the document —
   * not just relative to the viewport. This lets us scroll to any element
   * later without needing to re-scan the DOM.
   */
  function getAbsolutePosition(el) {
    const rect = el.getBoundingClientRect();
    const scrollTop = window.pageYOffset || document.documentElement.scrollTop;
    const scrollLeft =
      window.pageXOffset || document.documentElement.scrollLeft;
    return {
      scrollY: Math.round(rect.top + scrollTop),
      scrollX: Math.round(rect.left + scrollLeft),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
      rect,
    };
  }

  /**
   * Builds a minimal unique XPath for an element.
   * Used as a stable structural fallback when data-quanta-id is wiped by SPA re-renders.
   * Prefers id-based shortcuts (//*[@id="..."]) for brevity and speed.
   */
  function getXPath(el) {
    if (!el || el.nodeType !== Node.ELEMENT_NODE) return "";
    // Shortcut: if the element has a unique id, this is the best selector
    if (el.id) {
      // Verify uniqueness — some frameworks duplicate IDs
      try {
        if (document.querySelectorAll(`#${CSS.escape(el.id)}`).length === 1) {
          return `//*[@id="${el.id}"]`;
        }
      } catch (_) {}
    }

    const parts = [];
    let node = el;
    while (node && node.nodeType === Node.ELEMENT_NODE) {
      const tag = node.tagName.toLowerCase();
      let index = 1;
      let sibling = node.previousElementSibling;
      while (sibling) {
        if (sibling.tagName.toLowerCase() === tag) index++;
        sibling = sibling.previousElementSibling;
      }
      parts.unshift(index > 1 ? `${tag}[${index}]` : tag);
      node = node.parentElement;
      // Stop at body — we don't need the full /html/body/... prefix
      if (node && node.tagName && node.tagName.toLowerCase() === "body") {
        parts.unshift("body");
        break;
      }
    }
    return parts.length ? "//" + parts.join("/") : "";
  }

  /**
   * Extracts all semantic text signals from an element.
   * Returns normalized, truncated strings.
   */
  function getSemantics(el) {
    const rawText = (el.innerText || el.textContent || "")
      .replace(/\s+/g, " ")
      .trim();
    return {
      text: rawText.slice(0, MAX_TEXT_LENGTH),
      ariaLabel: el.getAttribute("aria-label") || null,
      ariaLabelledBy: el.getAttribute("aria-labelledby") || null,
      placeholder: el.placeholder || el.getAttribute("placeholder") || null,
      name: el.name || el.getAttribute("name") || null,
      id: el.id || null,
      dataTestId:
        el.getAttribute("data-testid") ||
        el.getAttribute("data-cy") ||
        el.getAttribute("data-qa") ||
        null,
      href: el.href || el.getAttribute("href") || null,
      value: el.value || el.getAttribute("value") || null,
      itemprop: el.getAttribute("itemprop") || null,
      title: el.getAttribute("title") || null,
      type: el.type || el.getAttribute("type") || null,
      role: el.getAttribute("role") || null,
      classes: Array.from(el.classList).filter((c) => c.length > 0),
    };
  }

  /**
   * A structural fingerprint for deduplication.
   * Identical buttons in a list (like 10 "Add to cart" buttons) collapse to one entry.
   * The fingerprint does NOT include position, so siblings are caught.
   */
  function buildFingerprint(tag, type, sem) {
    const key = [
      tag,
      type || "",
      sem.role || "",
      sem.ariaLabel || sem.placeholder || sem.text.slice(0, 30),
    ]
      .join("|")
      .toLowerCase();
    return key;
  }

  /**
   * Counts how many immediately preceding siblings have the same tag.
   * Used to detect and collapse repeating lists.
   */
  function countSameSiblings(el) {
    const tag = el.tagName;
    let count = 0;
    let prev = el.previousElementSibling;
    while (prev && prev.tagName === tag) {
      count++;
      prev = prev.previousElementSibling;
    }
    return count;
  }

  // ─── CORE WALKER ──────────────────────────────────────────────────────────

  /**
   * Recursively walks a DOM subtree.
   * @param {Node}    node          - Starting node
   * @param {boolean} inShadowDom   - True if we're inside a Shadow Root
   * @param {boolean} inIframe      - True if we're inside a child frame
   * @param {number|null} iframeIdx - Frame index for iFrame elements
   */
  function walkNode(
    node,
    inShadowDom = false,
    inIframe = false,
    iframeIdx = null,
  ) {
    if (!node || elements.length >= MAX_ELEMENTS) return;

    // Allow element nodes (1) and document fragments (11 - ShadowRoots)
    if (node.nodeType !== Node.ELEMENT_NODE && node.nodeType !== 11) return;

    // For document fragments, tag name is undefined, default to empty string
    const tag = (node.tagName || "").toLowerCase();

    // Ignore non-visual elements
    if (["script", "style", "noscript", "meta", "title"].includes(tag)) return;

    if (isInteractive(node)) {
      const style = window.getComputedStyle(node);
      if (isVisible(node, style)) {
        const pos = getAbsolutePosition(node);
        const sem = getSemantics(node);
        const fp = buildFingerprint(tag, sem.type, sem);

        // ── Deduplication: skip exact semantic + structural duplicates ──────
        if (!seenFingerprints.has(fp)) {
          seenFingerprints.add(fp);

          // ── Sibling collapse: skip > MAX_SIBLINGS repeated siblings ────────
          // e.g., a nav with 50 list items — we only keep the first MAX_SIBLINGS
          const sameCount = countSameSiblings(node);
          if (sameCount < MAX_SIBLINGS) {
            qIdCounter++;
            const qId = `q-${qIdCounter}`;

            // Brand the physical DOM element with our tracking ID
            // This allows execute_action_activity to click [data-quanta-id='q-12']
            node.setAttribute("data-quanta-id", qId);

            // Build the descriptor — only include non-null/non-empty fields
            const descriptor = {
              qId,
              tag,
              fingerprint: fp,
              isVisible: true,
              inViewport: isInViewport(pos.rect),
              scrollY: pos.scrollY,
              scrollX: pos.scrollX,
              width: pos.width,
              height: pos.height,
              inShadowDom,
              inIframe,
              iframeIndex: iframeIdx,
              xpath: getXPath(node),
            };

            // Merge semantic fields — omit null/empty to keep payload lean
            const semanticKeys = [
              "text",
              "ariaLabel",
              "ariaLabelledBy",
              "placeholder",
              "name",
              "id",
              "dataTestId",
              "href",
              "value",
              "title",
              "type",
              "role",
              "classes",
            ];
            for (const key of semanticKeys) {
              const v = sem[key];
              if (
                v !== null &&
                v !== undefined &&
                v !== "" &&
                !(Array.isArray(v) && v.length === 0)
              ) {
                descriptor[key] = v;
              }
            }

            elements.push(descriptor);
          }
        }
      }
    }

    // ── Pierce Shadow DOM ─────────────────────────────────────────────────
    // Web Components (Material UI, Lit, Polymer, Ionic) hide internals here.
    if (node.shadowRoot) {
      walkNode(node.shadowRoot, true, inIframe, iframeIdx);
    }

    // ── Recurse into children ─────────────────────────────────────────────
    for (const child of node.children) {
      walkNode(child, inShadowDom, inIframe, iframeIdx);
    }
  }

  // ─── IFRAME TRAVERSAL ─────────────────────────────────────────────────────
  /**
   * Attempts to access same-origin iFrame documents.
   * Cross-origin frames will throw a SecurityError — we catch and skip them.
   */
  function walkIframes() {
    const iframes = document.querySelectorAll("iframe");
    iframes.forEach((iframe, idx) => {
      try {
        const doc = iframe.contentDocument || iframe.contentWindow?.document;
        if (doc && doc.body) {
          walkNode(doc.body, false, true, idx);
        }
      } catch (e) {
        // Cross-origin iframe — we cannot access it. That is expected.
      }
    });
  }

  // ─── ENTRY POINT ──────────────────────────────────────────────────────────

  // Walk the main document
  walkNode(document.body);

  // Walk any accessible iFrames (same-origin)
  walkIframes();

  return elements;
};
