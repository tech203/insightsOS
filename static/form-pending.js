/**
 * Pending submit state.
 *
 * When any <form> is submitted, immediately:
 *   - Disable its submit button(s)
 *   - Set aria-busy="true" so screen readers announce
 *   - Add .is-pending so CSS can show a spinner / dim the button
 *   - Replace the button label with a friendly "..." form
 *     (preserves original label in data-original-label)
 *
 * Restores the button if:
 *   - The browser navigates back to this page (pageshow with persisted)
 *   - 30 seconds pass without navigation (failsafe — a server error
 *     might have rendered an in-place response without scrolling
 *     so the user knows nothing's stuck)
 *
 * Does NOT fire if:
 *   - The form is invalid (HTML5 validation blocks submit; the
 *     form-validation.js enhancer handles that flow separately)
 *   - The form sets data-no-pending-state
 *   - The submit event is canceled (preventDefault)
 *
 * Pairs cleanly with form-validation.js — that one runs on
 * 'invalid', this one runs on 'submit'. Both are global; no
 * per-form changes required.
 */
(function () {
    "use strict";

    if (typeof document === "undefined") return;

    var FAILSAFE_MS = 30000;

    function pendingLabel(original) {
        if (!original) return "Working...";
        // Match common verbs and replace with progressive form
        var verbMap = {
            "Save": "Saving...",
            "Save Changes": "Saving...",
            "Submit": "Submitting...",
            "Update": "Updating...",
            "Create": "Creating...",
            "Add": "Adding...",
            "Send": "Sending...",
            "Generate": "Generating...",
            "Run Audit": "Running audit...",
            "Run": "Running...",
            "Publish": "Publishing...",
            "Approve": "Approving...",
            "Delete": "Deleting...",
            "Remove": "Removing...",
            "Sign In": "Signing in...",
            "Log In": "Signing in...",
            "Log In to Dashboard": "Signing in...",
            "Create Account": "Creating account...",
            "Get Started": "Setting up...",
        };
        var trimmed = original.trim();
        if (verbMap[trimmed]) return verbMap[trimmed];
        // Generic fallback: append ellipsis if not already there
        if (trimmed.endsWith("...")) return trimmed;
        return trimmed + "...";
    }

    function setPending(button, isPending) {
        if (!button) return;
        if (isPending) {
            if (button.dataset.originalLabel === undefined) {
                button.dataset.originalLabel = button.innerHTML;
            }
            button.disabled = true;
            button.setAttribute("aria-busy", "true");
            button.classList.add("is-pending");
            // Replace text content while keeping any inner spinner.
            // Use innerHTML so the spinner CSS pseudo-element lays out
            // around the new text.
            var label = button.textContent || button.value || "";
            button.innerHTML = '<span class="btn-pending-label">' +
                escapeHtml(pendingLabel(label)) +
                '</span>';
        } else {
            button.disabled = false;
            button.removeAttribute("aria-busy");
            button.classList.remove("is-pending");
            if (button.dataset.originalLabel !== undefined) {
                button.innerHTML = button.dataset.originalLabel;
                delete button.dataset.originalLabel;
            }
        }
    }

    function escapeHtml(s) {
        return String(s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function findSubmitButtons(form) {
        // Both <button type=submit> inside the form AND any external
        // button with [form="form-id"]. Skip [formnovalidate] and
        // explicit type=button.
        var inForm = form.querySelectorAll(
            'button[type="submit"], button:not([type]), input[type="submit"]'
        );
        var external = form.id
            ? document.querySelectorAll('button[form="' + form.id + '"]')
            : [];
        var all = [];
        inForm.forEach(function (b) { all.push(b); });
        external.forEach(function (b) {
            if (b.type === "submit" || !b.type) all.push(b);
        });
        return all;
    }

    function handleSubmit(e) {
        var form = e.target;
        if (!form || form.tagName !== "FORM") return;
        if (e.defaultPrevented) return;
        if (form.dataset.noPendingState !== undefined) return;

        var buttons = findSubmitButtons(form);
        buttons.forEach(function (b) { setPending(b, true); });
        form._pendingButtons = buttons;
        form._pendingTimer = setTimeout(function () {
            buttons.forEach(function (b) { setPending(b, false); });
        }, FAILSAFE_MS);
    }

    // Page-show fires on back/forward nav (bfcache); restore
    // any buttons that were left in a pending state.
    function handlePageShow() {
        document.querySelectorAll("button.is-pending, input.is-pending").forEach(function (b) {
            setPending(b, false);
        });
    }

    document.addEventListener("submit", handleSubmit, true);
    window.addEventListener("pageshow", handlePageShow);
})();
