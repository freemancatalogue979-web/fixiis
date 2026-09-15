(function () {
    "use strict";

    const REQUEST_KEY = "__singlefile_bridge_request";
    const RESPONSE_KEY = "__singlefile_bridge_response";

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

    window.__singlefile_bridge_installed = true;

    window.addEventListener("message", function (event) {
        if (event.source !== window) {
            return;
        }

        const data = event.data;

        if (!data || data[REQUEST_KEY] !== true) {
            return;
        }

        const requestId = data.requestId;

        if (!requestId) {
            return;
        }

        // The MAIN-world bridge does NOT attempt to access
        // chrome.runtime.
        //
        // It simply forwards the request to the isolated
        // SingleFile bridge.
        window.postMessage(
            {
                __singlefile_bridge_to_extension: true,
                requestId: requestId,
                extId: data.extId,
                message: data.message,
            },
            "*"
        );
    });

    // Response coming back from the isolated extension bridge.
    window.addEventListener("message", function (event) {
        if (event.source !== window) {
            return;
        }

        const data = event.data;

        if (
            !data ||
            data.__singlefile_bridge_from_extension !== true
        ) {
            return;
        }

        sendResponse(
            data.requestId,
            data.resp
        );
    });
})();
