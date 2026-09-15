(function () {
    "use strict";

    const REQUEST_KEY = "__singlefile_bridge_to_extension";
    const RESPONSE_KEY = "__singlefile_bridge_from_extension";

    function sendResponse(requestId, response) {
        window.postMessage(
            {
                [RESPONSE_KEY]: true,
                requestId: requestId,
                resp: response,
            },
            "*"
        );
    }

    function getRuntime() {
        if (
            typeof chrome !== "undefined" &&
            chrome.runtime &&
            typeof chrome.runtime.sendMessage === "function"
        ) {
            return chrome.runtime;
        }

        if (
            typeof browser !== "undefined" &&
            browser.runtime &&
            typeof browser.runtime.sendMessage === "function"
        ) {
            return browser.runtime;
        }

        return null;
    }

    window.addEventListener("message", function (event) {
        if (event.source !== window) {
            return;
        }

        const data = event.data;

        if (!data || data[REQUEST_KEY] !== true) {
            return;
        }

        const requestId = data.requestId;
        const extId = data.extId;
        const message = data.message;

        if (!requestId || !message) {
            return;
        }

        const runtime = getRuntime();

        if (!runtime) {
            sendResponse(requestId, {
                __sf_err: "no_extension_runtime"
            });
            return;
        }

        let finished = false;

        const respond = function (response) {
            if (finished) {
                return;
            }

            finished = true;

            sendResponse(
                requestId,
                response
            );
        };

        try {
            // First send internal message (without extId) so it routes to runtime.onMessage
            // where sender.tab is automatically attached and trustedCaller is honored.
            runtime.sendMessage(
                message,
                function (response) {
                    if (runtime.lastError) {
                        // If internal message failed and extId is provided, try external route
                        if (extId) {
                            try {
                                runtime.sendMessage(extId, message, function(resp2) {
                                    if (runtime.lastError) {
                                        respond({
                                            __sf_err: "sendMessage:" + (runtime.lastError.message || "runtime.lastError")
                                        });
                                    } else {
                                        respond(resp2 || { __sf_err: "empty_extension_response" });
                                    }
                                });
                                return;
                            } catch(e2) {}
                        }
                        respond({
                            __sf_err: "sendMessage:" + (runtime.lastError.message || "runtime.lastError")
                        });
                        return;
                    }

                    respond(
                        response || {
                            __sf_err: "empty_extension_response"
                        }
                    );
                }
            );
        } catch (err) {
            respond({
                __sf_err:
                    "sendMessage_exception:" +
                    (
                        err &&
                        err.message
                            ? err.message
                            : String(err)
                    )
            });
        }
    });
})();
