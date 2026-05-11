/**
 * Form validation enhancer.
 *
 * Hooks every <form> in the app to upgrade native HTML5 validation
 * into inline-error UX:
 *   - First invalid field is scrolled into view + focused
 *   - A styled .field-error message appears directly under the field
 *   - The browser's default validation tooltip is suppressed
 *   - Errors clear automatically when the user starts editing
 *   - aria-invalid + aria-describedby wire screen-reader output
 *
 * Opt-out per form: <form data-no-validate-enhancer>
 * Opt-out per field: <input data-skip-validation>
 *
 * No per-form markup changes required. Forms keep their native
 * `required` / `pattern` / `type="email"` etc. attributes; this
 * script just dresses up how those validation errors are shown.
 */
(function () {
    "use strict";

    if (typeof document === "undefined") return;

    var ERROR_CLASS = "field-error";
    var ID_PREFIX = "field-error-";
    var counter = 0;

    function nextId() {
        counter += 1;
        return ID_PREFIX + counter;
    }

    function friendlyMessage(field) {
        var v = field.validity;
        if (!v) return field.validationMessage || "Please check this field.";
        if (v.valueMissing) {
            return field.dataset.requiredMessage || "This field is required.";
        }
        if (v.typeMismatch) {
            if (field.type === "email") return "Enter a valid email address.";
            if (field.type === "url") return "Enter a valid URL (include https://).";
            return "Enter a valid value for this field.";
        }
        if (v.tooShort) {
            return "Make this at least " + field.minLength + " characters.";
        }
        if (v.tooLong) {
            return "Keep this under " + field.maxLength + " characters.";
        }
        if (v.rangeUnderflow) return "Value must be at least " + field.min + ".";
        if (v.rangeOverflow) return "Value must be at most " + field.max + ".";
        if (v.patternMismatch) {
            return field.dataset.patternMessage ||
                   field.title ||
                   "This field doesn't match the expected format.";
        }
        if (v.stepMismatch) return "Pick a valid step value.";
        return field.validationMessage || "Please check this field.";
    }

    function ensureErrorEl(field) {
        var existing = field._fieldErrorEl;
        if (existing && existing.parentNode) return existing;

        var el = document.createElement("div");
        el.className = ERROR_CLASS;
        el.id = nextId();
        el.setAttribute("role", "alert");

        // Insert after the field. If the field is wrapped in a label
        // (common pattern), insert after the label instead so the
        // error sits below the entire field+label group.
        var anchor = field;
        var parent = field.parentNode;
        if (parent && parent.tagName === "LABEL") {
            anchor = parent;
            parent = parent.parentNode;
        }
        if (parent) parent.insertBefore(el, anchor.nextSibling);

        field._fieldErrorEl = el;

        // Wire ARIA so screen readers announce the error.
        var existingDescribedBy = field.getAttribute("aria-describedby") || "";
        if (existingDescribedBy.indexOf(el.id) === -1) {
            field.setAttribute(
                "aria-describedby",
                (existingDescribedBy + " " + el.id).trim()
            );
        }

        return el;
    }

    function showError(field, message) {
        var el = ensureErrorEl(field);
        el.textContent = message;
        el.classList.add("is-visible");
        field.classList.add("is-invalid");
        field.setAttribute("aria-invalid", "true");
    }

    function clearError(field) {
        if (field._fieldErrorEl) {
            field._fieldErrorEl.classList.remove("is-visible");
            field._fieldErrorEl.textContent = "";
        }
        field.classList.remove("is-invalid");
        field.removeAttribute("aria-invalid");
    }

    function attachClearOnEdit(field) {
        if (field._validationClearWired) return;
        field._validationClearWired = true;
        ["input", "change"].forEach(function (event) {
            field.addEventListener(event, function () {
                if (field.checkValidity()) clearError(field);
            });
        });
    }

    function handleInvalid(e) {
        var field = e.target;
        if (!field || !field.form) return;
        if (field.form.dataset.noValidateEnhancer !== undefined) return;
        if (field.dataset.skipValidation !== undefined) return;
        if (field.type === "submit" || field.type === "button" || field.type === "hidden") return;

        // Block the browser's default validation tooltip.
        e.preventDefault();

        var message = friendlyMessage(field);
        showError(field, message);
        attachClearOnEdit(field);

        // Focus + scroll the first invalid field. Subsequent invalid
        // events fire for other fields in DOM order; we only scroll
        // for the first one to avoid jumping around.
        if (!field.form._scrolledToInvalid) {
            field.form._scrolledToInvalid = true;
            try {
                field.focus({ preventScroll: true });
            } catch (_) {
                field.focus();
            }
            var rect = field.getBoundingClientRect();
            var topOffset = window.scrollY + rect.top - 120;
            window.scrollTo({ top: Math.max(0, topOffset), behavior: "smooth" });
        }
    }

    function handleSubmit(e) {
        var form = e.target;
        if (!form || form.tagName !== "FORM") return;
        // Reset the scroll-once flag so the next attempt re-scrolls.
        form._scrolledToInvalid = false;
    }

    // 'invalid' doesn't bubble — must use capture phase to delegate.
    document.addEventListener("invalid", handleInvalid, true);
    document.addEventListener("submit", handleSubmit, true);
})();
