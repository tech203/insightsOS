/**
 * Task-completion tab notifier.
 *
 * When a long-running operation finishes (audit / brief / draft /
 * presentation generation), the user often has the app in a
 * background tab. By the time they tab back, they've forgotten what
 * was running. This script prepends a "(✓) " to the page title so
 * the browser tab and the OS task switcher show that something
 * completed while they were away.
 *
 * Opt-in per page via:
 *   <body data-task-complete-notify="Audit complete">
 *     ↑ value is a short label that shows briefly in the title
 *
 * Behaviour:
 *   1. On page load, if document is hidden, prepend "(✓) " to title
 *   2. On visibilitychange to visible, restore the original title
 *   3. If document was visible at load time, do nothing — the user
 *      saw the result land naturally, no need to flicker the tab
 *   4. If the Notifications API permission is granted, ALSO fire a
 *      desktop notification with the label
 *
 * Permission for desktop notifications is never requested
 * automatically. Pages can request it explicitly via a
 * "Notify me when long tasks finish" preference toggle (future
 * enhancement). Without permission, only the title-blink fires.
 */
(function () {
    "use strict";

    if (typeof document === "undefined") return;

    var notifyLabel = document.body && document.body.dataset.taskCompleteNotify;
    if (!notifyLabel) return;

    var originalTitle = document.title;
    var pendingTitle = "(✓) " + originalTitle;
    var wasHidden = document.visibilityState === "hidden";

    function applyPending() {
        if (document.visibilityState === "hidden") {
            document.title = pendingTitle;
        }
    }

    function restore() {
        document.title = originalTitle;
    }

    if (wasHidden) {
        applyPending();

        // Also try a desktop notification if permission was previously
        // granted. We never ask for permission here — that needs an
        // explicit user action (a preferences toggle). Just consume
        // the existing grant if it's there.
        try {
            if (typeof Notification !== "undefined" &&
                Notification.permission === "granted") {
                var n = new Notification("DarInsights", {
                    body: notifyLabel,
                    silent: false,
                    tag: "darinsights-task-complete",
                });
                n.onclick = function () {
                    window.focus();
                    n.close();
                };
            }
        } catch (_) { /* notifications unavailable / blocked */ }
    }

    document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "visible") restore();
    });

    // Also restore if the user clicks anywhere — handles edge cases
    // where visibilitychange doesn't fire (some embedded webviews).
    window.addEventListener("focus", restore, { once: true });
})();
